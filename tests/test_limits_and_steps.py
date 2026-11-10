"""Per-macro limits and the per-step log in the history."""

import threading
import time
from datetime import datetime

import pytest

from keyboardmanager.core import actions as catalog
from keyboardmanager.core import executor as executor_mod
from keyboardmanager.core.executor import MacroExecutor
from keyboardmanager.core.limits import Limits
from keyboardmanager.core.models import Macro
from keyboardmanager.core.preflight import CheckContext, check_macro
from keyboardmanager.core.schedule import Schedule
from keyboardmanager.core.service import MacroService
from tests_support import FakeHotkeys, wait_until


def A(t, children=(), **params):
    a = catalog.new_action(t)
    a.params.update(params)
    a.children = list(children)
    return a


# -------------------------------------------------------------------- model
def test_limits_model():
    limits = Limits(max_seconds=30, cooldown_s=60)
    assert limits.active and Limits.from_dict(limits.to_dict()) == limits and not Limits().active
    assert limits.describe() == "stops after 30 s, at most once every 60 s"
    for bad in ({"max_seconds": -1}, {"cooldown_s": 1.5}, {"max_steps": True}, {"sometimes": 1}, []):
        with pytest.raises(ValueError):
            Limits.from_dict(bad)
    m = Macro("m", limits=limits)
    assert m.to_dict()["limits"] == {"max_seconds": 30, "cooldown_s": 60}
    assert Macro.from_dict(m.to_dict()).limits == limits and "limits" not in Macro("x").to_dict()


def test_limits_persist(db):
    m = db.save_macro(Macro("m", limits=Limits(max_steps=5, max_runs_per_day=3)))
    assert db.get_macro(m.id).limits == Limits(max_steps=5, max_runs_per_day=3)
    db._conn.execute("UPDATE macros SET limits='{bad' WHERE id=?", (m.id,))
    assert db.get_macro(m.id).limits == Limits()


# ---------------------------------------------------------- run-time limits
def test_time_limit_stops_a_run(backend):
    macro = Macro("m", [A("delay", ms=10_000), A("key_press", keys="a")], limits=Limits(max_seconds=1))
    t = time.monotonic()
    result = MacroExecutor(backend).run(macro)
    assert result.status == "stopped" and result.message == "Stopped: time limit of 1 s reached"
    assert 0.9 <= time.monotonic() - t < 2 and backend.calls == []


def test_paused_time_does_not_count(backend):
    ex = MacroExecutor(backend)
    done = {}
    macro = Macro("m", [A("delay", ms=600), A("key_press", keys="a")], limits=Limits(max_seconds=1))
    th = threading.Thread(target=lambda: done.update(r=ex.run(macro)))
    th.start()
    time.sleep(0.1)
    ex.pause()
    time.sleep(1.2)  # longer than the limit, but paused
    ex.resume()
    th.join(3)
    assert done["r"].status == "success" and ("down", "a") in backend.calls


def test_step_limit(backend):
    macro = Macro("m", [A("repeat", [A("key_press", keys="a")], count=0)], limits=Limits(max_steps=10))
    result = MacroExecutor(backend).run(macro)
    assert result.status == "stopped" and "step limit of 10" in result.message
    assert len([c for c in backend.calls if c[0] == "down"]) == 9  # the repeat itself is a step


# ----------------------------------------------------------- trigger limits
@pytest.fixture
def svc(db, backend):
    now = [datetime(2030, 1, 2, 9, 0).timestamp()]
    service = MacroService(db, backend, FakeHotkeys(), clock=lambda: now[0], scheduler_thread=False)
    service.now = now
    yield service
    service.shutdown()


def run_and_wait(svc, macro, trigger):
    started = svc.run(macro, trigger=trigger)
    wait_until(lambda: svc.running_macro is None)
    return started


def test_cooldown_applies_to_triggers_only(svc, db):
    m = db.save_macro(Macro("m", [A("comment", text="x")], hotkey="ctrl+1", limits=Limits(cooldown_s=60)))
    run_and_wait(svc, m, "hotkey ctrl+1")
    svc.now[0] += 30
    assert run_and_wait(svc, m, "hotkey ctrl+1")  # handled: skipped, not "busy"
    entry = db.list_history()[0]
    assert entry.status == "skipped" and entry.message == "Skipped: cooldown: it may run again in 30 s"
    run_and_wait(svc, m, "manual")  # on purpose: never limited
    assert db.list_history()[0].status == "success"
    svc.now[0] += 61
    run_and_wait(svc, m, "abbreviation ;m")
    assert db.list_history()[0].status == "success"


def test_daily_limit_counts_today_only(svc, db):
    m = db.save_macro(Macro("m", [A("comment", text="x")], limits=Limits(max_runs_per_day=2)))
    for _ in range(3):
        run_and_wait(svc, m, "event: x")
    assert [e.status for e in db.list_history()] == ["skipped", "success", "success"]
    assert "already ran 2 time(s) today (limit 2)" in db.list_history()[0].message
    svc.now[0] += 86_400  # the next day
    run_and_wait(svc, m, "event: x")
    assert db.list_history()[0].status == "success"


def test_a_schedule_moves_on_after_a_limited_occurrence(svc, db):
    m = db.save_macro(Macro("m", [A("comment", text="x")], schedule=Schedule("interval", minutes=1), limits=Limits(cooldown_s=3600)))
    svc.reload()
    run_and_wait(svc, m, "manual")
    svc.now[0] += 60
    svc.scheduler.tick()
    svc.scheduler.tick()  # a refused occurrence is not retried every second
    assert [e.status for e in db.list_history()] == ["skipped", "success"]
    assert svc.next_run(m.id) > svc.now[0]


def test_preflight_mentions_limits():
    m = Macro("m", [A("delay", ms=10_000)], limits=Limits(max_seconds=5, cooldown_s=60), id=1)
    messages = [i.message for i in check_macro(m, CheckContext(macros=[m]))]
    assert "limits: stops after 5 s, at most once every 60 s" in messages
    assert any("time limit (5 s) is shorter than it takes (10.0 s)" in x for x in messages)


# ------------------------------------------------------------------ step log
def test_step_log_records_every_step_with_details(backend):
    macro = Macro("m", [
        A("set_variable", name="n", value="0"),
        A("repeat", [A("set_variable", name="n", value="1", mode="add"),
                     A("if", [A("break")], kind="variable", variable="n", op="==", compare_to="2")], count=5),
        A("type_text", text="secret password"),
    ])
    result = MacroExecutor(backend).run(macro)
    log = [(s.depth, s.action_type, s.status, s.detail) for s in result.steps]
    assert log == [
        (0, "set_variable", "ok", "n = '0'"),
        (0, "repeat", "ok", "2 rounds; left by Break in round 2"),
        (1, "set_variable", "ok", "n = '1'"),
        (1, "if", "ok", "condition false: else"),
        (1, "set_variable", "ok", "n = '2'"),
        (1, "if", "ok", "condition true: then"),
        (2, "break", "ok", ""),
        (0, "type_text", "ok", "15 characters"),
    ]
    assert [s.seq for s in result.steps] == list(range(1, 9))
    assert not any("secret" in s.detail for s in result.steps)  # typed text is never logged


def test_step_log_marks_the_failing_and_the_stopped_step(backend):
    result = MacroExecutor(backend).run(Macro("m", [A("run_macro", name="nobody")]))
    assert result.steps[0].status == "error" and "no macro named" in result.steps[0].detail
    result = MacroExecutor(backend).run(Macro("m", [A("delay", ms=5000)], limits=Limits(max_seconds=1)))
    assert result.steps[0].status == "stopped" and "time limit" in result.steps[0].detail


def test_step_log_is_capped(backend, monkeypatch):
    monkeypatch.setattr(executor_mod, "MAX_LOGGED_STEPS", 5)
    result = MacroExecutor(backend).run(Macro("m", [A("repeat", [A("key_press", keys="a")], count=10)]))
    assert len(result.steps) == 5 and result.steps_not_logged == 6


def test_step_log_can_be_switched_off(backend):
    result = MacroExecutor(backend, log_steps=False).run(Macro("m", [A("stop")]))
    assert result.steps == []


def test_service_stores_the_step_log(svc, db, monkeypatch):
    monkeypatch.setattr(executor_mod, "MAX_LOGGED_STEPS", 2)
    m = db.save_macro(Macro("m", [A("key_press", keys="a")] * 3))
    run_and_wait(svc, m, "manual")
    steps = db.history_steps(db.list_history()[0].id)
    assert [s["action_type"] for s in steps] == ["key_press", "key_press", ""]
    assert steps[-1]["description"] == "... 1 more steps were run but not logged"
    svc.set_log_steps(False)
    run_and_wait(svc, m, "manual")
    assert db.history_steps(db.list_history()[0].id) == [] and not svc.log_steps
    db.clear_history()
    assert db.history_steps(1) == []


def test_history_tab_shows_the_steps_of_a_run(qapp, db, backend):
    from keyboardmanager.gui.history import HistoryView

    service = MacroService(db, backend, FakeHotkeys(), scheduler_thread=False)
    m = db.save_macro(Macro("m", [A("repeat", [A("key_press", keys="a")], count=2), A("stop")]))
    service.run(m)
    assert wait_until(lambda: service.running_macro is None)
    toggled = []
    view = HistoryView(db, log_steps=(lambda: True, toggled.append))
    assert view.steps_label.text() == "Select a run to see its steps."
    view.table.selectRow(0)
    top = [view.steps.topLevelItem(i) for i in range(view.steps.topLevelItemCount())]
    assert [t.text(1) for t in top] == ["Repeat 2 times", "Stop macro"]
    assert [top[0].child(i).text(1) for i in range(top[0].childCount())] == ["Press a", "Press a"]
    assert top[0].text(3) == "ok - 2 rounds" and top[1].text(3) == "stopped"  # Stop ends the run
    view.log_check.setChecked(False)
    assert toggled == [False]
    service.shutdown()


def test_editor_limits_fields(qapp, db, backend):
    from keyboardmanager.gui.mainwindow import MainWindow

    service = MacroService(db, backend, FakeHotkeys(), scheduler_thread=False)
    window = MainWindow(db, service)
    window._new_macro()
    ed = window.editor
    assert ed.limit_spins["max_seconds"].text() == "none"  # 0 reads as "none"
    ed.limit_spins["max_seconds"].setValue(45)
    ed.limit_spins["cooldown_s"].setValue(10)
    assert ed.dirty and window.save_current()
    assert db.list_macros()[0].limits == Limits(max_seconds=45, cooldown_s=10)
    ed.load(Macro("other", limits=Limits(max_runs_per_day=7)))
    assert ed.limit_spins["max_runs_per_day"].value() == 7 and ed.limit_spins["max_seconds"].value() == 0
    window.editor.dirty = False
    window._quitting = True
    window.close()
    service.shutdown()


def test_the_test_run_countdown_is_not_part_of_the_time_limit(backend):
    macro = Macro("m", [A("delay", ms=300), A("key_press", keys="a")], limits=Limits(max_seconds=1))
    result = MacroExecutor(backend).run(macro, start_delay=1.2)  # longer than the limit
    assert result.status == "success" and ("down", "a") in backend.calls


def test_refused_runs_do_not_count_toward_the_limits(svc, db):
    m = db.save_macro(Macro("m", [A("comment", text="x")], limits=Limits(cooldown_s=60, max_runs_per_day=2)))
    run_and_wait(svc, m, "hotkey ctrl+1")      # runs (1 of 2 today)
    svc.now[0] += 40
    run_and_wait(svc, m, "hotkey ctrl+1")      # refused: cooldown...
    svc.now[0] += 30                           # ...70 s after the run, 30 s after the refusal
    run_and_wait(svc, m, "hotkey ctrl+1")      # the refusal neither restarted the cooldown...
    assert [e.status for e in db.list_history()] == ["success", "skipped", "success"]
    svc.now[0] += 61
    run_and_wait(svc, m, "hotkey ctrl+1")      # ...nor used up one of today's 2 runs: now the limit
    assert db.list_history()[0].status == "skipped" and "2 time(s) today" in db.list_history()[0].message
