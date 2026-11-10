"""Queued runs, the trigger engine (trigger rules), debug runs and the live state."""

import threading
import time

import pytest

from keyboardmanager.core import actions as catalog
from keyboardmanager.core import service as service_mod
from keyboardmanager.core import triggers as triggers_mod
from keyboardmanager.core.debugger import Debugger
from keyboardmanager.core.limits import Limits
from keyboardmanager.core.models import Macro
from keyboardmanager.core.preflight import CheckContext, ERROR, INFO, WARNING, check_all, check_triggers
from keyboardmanager.core.service import MacroService
from keyboardmanager.core.triggers import MAX_CHAIN, TriggerEngine, TriggerRule, validate_rule
from tests_support import FakeHotkeys, wait_until


def A(t, **params):
    a = catalog.new_action(t)
    a.params.update(params)
    return a


def typing(name, text=None, delay_ms=0, **kw):
    steps = ([A("delay", ms=delay_ms)] if delay_ms else []) + [A("type_text", text=text or name)]
    return Macro(name, steps, **kw)


@pytest.fixture
def svc(db, backend):
    events = []
    service = MacroService(db, backend, FakeHotkeys(), listener=lambda e, p: events.append((e, p)),
                           scheduler_thread=False)
    service.events_seen = events
    yield service
    service.shutdown()


def typed(backend):
    return [c[1] for c in backend.calls if c[0] == "type"]


def idle(svc, timeout=3.0):
    return wait_until(lambda: svc.running_macro is None and not svc.queue_snapshot(), timeout)


# ------------------------------------------------------------------- queue
def test_queued_runs_start_in_order_after_the_current_one(svc, backend):
    assert svc.run(typing("first", delay_ms=200))
    assert svc.run(typing("second"), queue=True)
    assert svc.run(typing("third"), queue=True)
    assert svc.queue_snapshot() == [("second", "manual"), ("third", "manual")]
    assert ("queue", [("second", "manual"), ("third", "manual")]) in svc.events_seen
    assert idle(svc)
    assert typed(backend) == ["first", "second", "third"]
    assert [h.macro_name for h in reversed(svc.db.list_history())] == ["first", "second", "third"]


def test_without_queueing_a_busy_run_is_refused(svc, backend):
    svc.run(typing("first", delay_ms=200))
    assert svc.run(typing("second")) is False
    assert svc.run(typing("third"), queue=False) is False
    assert svc.queue_snapshot() == []
    assert idle(svc) and typed(backend) == ["first"]


def test_when_busy_queue_setting_is_the_default_for_triggers(svc, backend):
    svc.run(typing("first", delay_ms=200))
    assert svc.run(typing("patient", when_busy="queue"), trigger="hotkey ctrl+1")
    assert svc.run(typing("impatient"), trigger="hotkey ctrl+2") is False
    assert svc.run(typing("refuses", when_busy="queue"), queue=False) is False  # explicit wins
    assert idle(svc) and typed(backend) == ["first", "patient"]


def test_queue_is_limited(svc, backend, db):
    svc.run(typing("first", delay_ms=300))
    for i in range(service_mod.MAX_QUEUE):
        assert svc.run(typing(f"q{i}"), queue=True)
    extra = db.save_macro(typing("extra"))
    assert svc.run(extra, queue=True)  # handled: recorded as skipped
    assert len(svc.queue_snapshot()) == service_mod.MAX_QUEUE
    assert db.list_history()[0].status == "skipped" and "queue is full" in db.list_history()[0].message
    svc.clear_queue()


def test_remove_and_clear_queued_runs(svc, backend):
    svc.run(typing("first", delay_ms=300))
    for name in ("a", "b", "c"):
        svc.run(typing(name), queue=True)
    svc.remove_queued(1)
    svc.remove_queued(7)  # out of range: ignored
    assert [n for n, _ in svc.queue_snapshot()] == ["a", "c"]
    svc.clear_queue()
    assert svc.queue_snapshot() == [] and svc.events_seen[-1] == ("queue", [])
    assert idle(svc) and typed(backend) == ["first"]


def test_stop_empties_the_queue(svc, backend):
    svc.run(typing("first", delay_ms=5000))
    svc.run(typing("second"), queue=True)
    assert wait_until(lambda: svc.live_state() is not None)
    svc.stop()
    assert idle(svc)
    time.sleep(0.1)
    assert typed(backend) == [] and svc.db.list_history()[0].status == "stopped"


def test_queued_run_keeps_its_trigger_and_limits_still_apply(svc, backend, db):
    limited = db.save_macro(typing("limited", limits=Limits(cooldown_s=3600)))
    svc.run(limited, trigger="hotkey ctrl+1", blocking=True)
    svc.run(typing("first", delay_ms=100))
    assert svc.run(limited, trigger="hotkey ctrl+1", queue=True)  # cooldown: skipped, not queued
    assert svc.queue_snapshot() == []
    assert db.list_history()[0].status == "skipped"
    svc.run(typing("later"), trigger="schedule", queue=True)
    assert idle(svc)
    assert db.list_history()[0].trigger == "schedule"


def test_nothing_starts_after_shutdown(db, backend):
    service = MacroService(db, backend, FakeHotkeys(), scheduler_thread=False)
    service.run(typing("first", delay_ms=5000))
    service.run(typing("second"), queue=True)
    service.shutdown()
    assert service.run(typing("late")) is False
    time.sleep(0.1)
    assert typed(backend) == []


# ---------------------------------------------------------- trigger engine
class Clock:
    def __init__(self):
        self.clipboard = ""
        self.values = {}
        self.files = {}


@pytest.fixture
def engine():
    world = Clock()
    fired = []

    def evaluate(expression):
        from keyboardmanager.core.script import evaluate as ev

        return ev(expression, lambda name: world.values[name])

    eng = TriggerEngine(fire=lambda rule, text, chain: fired.append((rule.name, text, chain)),
                        evaluate=evaluate, clipboard=lambda: world.clipboard,
                        file_state=lambda path: world.files.get(path))
    eng.world, eng.fired = world, fired
    yield eng
    eng.stop()


def rule(name, kind, macro="target", params=None, **kw):
    return TriggerRule(name, kind, macro, params or {}, id=hash(name) % 10_000, **kw)


def test_macro_finished_rules(engine):
    engine.update([rule("any", "macro_finished", params={"macro": "Build"}),
                   rule("ok", "macro_finished", params={"macro": "build", "result": "success"}),
                   rule("bad", "macro_finished", params={"macro": "Build", "result": "error"}),
                   rule("other", "macro_finished", params={"macro": "Deploy"})], thread=False)
    engine.macro_finished("Build", "success")
    assert [f[0] for f in engine.fired] == ["any", "ok"]
    assert engine.fired[0][1] == 'trigger "any": "Build" finished' and engine.fired[0][2] == 1
    engine.fired.clear()
    engine.macro_finished("Build", "error", chain=3)
    assert [(f[0], f[2]) for f in engine.fired] == [("any", 4), ("bad", 4)]


def test_macro_finished_chain_is_limited(engine):
    r = rule("loop", "macro_finished", params={"macro": "A"})
    engine.update([r], thread=False)
    engine.macro_finished("A", "success", chain=MAX_CHAIN - 1)
    assert len(engine.fired) == 1
    engine.macro_finished("A", "success", chain=MAX_CHAIN)
    assert len(engine.fired) == 1 and "in a row" in engine.status[r.id]


def test_clipboard_rule_fires_on_change_not_on_the_first_look(engine):
    r = rule("clip", "clipboard")
    engine.update([r], thread=False)
    engine.world.clipboard = "already there"
    engine.tick()
    assert engine.fired == []  # the baseline
    engine.tick()
    assert engine.fired == []  # unchanged
    engine.world.clipboard = "new"
    engine.tick()
    assert len(engine.fired) == 1 and engine.fired[0][1] == 'trigger "clip": the clipboard changes'
    assert engine.status[r.id].endswith("started the macro")


def test_clipboard_rule_with_contains(engine):
    engine.update([rule("url", "clipboard", params={"contains": "HTTP"})], thread=False)
    engine.tick()
    for text in ("plain", "http://x", "https://y", "https://y"):
        engine.world.clipboard = text
        engine.tick()
    assert len(engine.fired) == 2  # case-insensitive, and only on a change


def test_file_rule(engine, tmp_path):
    path = str(tmp_path / "report.csv")
    engine.update([rule("file", "file", params={"path": path})], thread=False)
    engine.tick()
    engine.world.files[path] = (1, 10)  # created
    engine.tick()
    engine.tick()
    engine.world.files[path] = (2, 10)  # changed
    engine.tick()
    del engine.world.files[path]  # deleted
    engine.tick()
    assert len(engine.fired) == 3


def test_real_file_state(tmp_path):
    path = tmp_path / "f.txt"
    assert triggers_mod._mtime(str(path)) is None
    path.write_text("a")
    first = triggers_mod._mtime(str(path))
    path.write_text("abc")  # the size changes even when the time does not
    assert triggers_mod._mtime(str(path)) != first


def test_becomes_true_fires_on_the_edge_only(engine):
    r = rule("edge", "becomes_true", params={"expression": "n >= 3"})
    engine.update([r], thread=False)
    engine.world.values["n"] = "5"
    engine.tick()  # already true at the start: the baseline, no fire
    assert engine.fired == []
    for n in ("4", "1", "2", "3", "7", "0", "9"):
        engine.world.values["n"] = n
        engine.tick()
    assert len(engine.fired) == 2  # 2 -> 3 and 0 -> 9


def test_expression_errors_are_reported_not_raised(engine):
    r = rule("broken", "becomes_true", params={"expression": "missing > 1"})
    engine.update([r], thread=False)
    engine.tick()
    assert "error: unknown variable missing" in engine.status[r.id] and engine.fired == []


def test_condition_is_checked_when_firing(engine):
    r = rule("cond", "clipboard", condition="mode == \"on\"")
    engine.update([r], thread=False)
    engine.tick()
    engine.world.values["mode"] = "off"
    engine.world.clipboard = "x"
    engine.tick()
    assert engine.fired == [] and "condition was false" in engine.status[r.id]
    engine.world.values["mode"] = "on"
    engine.world.clipboard = "y"
    engine.tick()
    assert len(engine.fired) == 1
    del engine.world.values["mode"]
    engine.world.clipboard = "z"
    engine.tick()
    assert len(engine.fired) == 1 and "condition error" in engine.status[r.id]


def test_disabled_rules_and_updates(engine):
    on, off = rule("on", "clipboard"), rule("off", "clipboard", enabled=False)
    engine.update([on, off], thread=False)
    engine.tick()
    engine.world.clipboard = "x"
    engine.tick()
    assert [f[0] for f in engine.fired] == ["on"] and engine.active_count() == 1
    engine.update([], thread=False)
    engine.world.clipboard = "y"
    engine.tick()
    assert len(engine.fired) == 1 and engine.active_count() == 0


def test_polling_thread_runs_only_for_polling_rules(engine, monkeypatch):
    monkeypatch.setattr(triggers_mod, "POLL_S", 0.02)
    engine.update([rule("done", "macro_finished", params={"macro": "x"})])
    assert engine._thread is None  # nothing to poll
    engine.update([rule("clip", "clipboard")])
    assert engine._thread is not None
    engine.world.clipboard = "a"
    time.sleep(0.1)
    engine.world.clipboard = "b"
    assert wait_until(lambda: engine.fired)
    engine.update([])
    assert engine._thread is None


def test_restarting_the_polling_leaves_one_thread(engine, monkeypatch):
    monkeypatch.setattr(triggers_mod, "POLL_S", 0.02)
    slow = threading.Event()
    engine._clipboard = lambda: slow.wait(1.0) and "" or ""  # e.g. waiting for a busy GUI thread
    engine.update([rule("clip", "clipboard")])
    first = engine._thread
    time.sleep(0.05)  # the first poll is under way
    engine.update([])  # gives up waiting for it after a moment
    engine.update([rule("clip", "clipboard")])  # started again right away
    second = engine._thread
    first.join(1)
    assert not first.is_alive() and second.is_alive() and second is not first


@pytest.mark.parametrize("bad, message", [
    (TriggerRule("", "clipboard", "m"), "name"),
    (TriggerRule("n", "weird", "m"), "unknown kind"),
    (TriggerRule("n", "clipboard", " "), "choose the macro"),
    (TriggerRule("n", "macro_finished", "m", {}), "whose end"),
    (TriggerRule("n", "macro_finished", "m", {"macro": "x", "result": "maybe"}), "result"),
    (TriggerRule("n", "file", "m", {"path": ""}), "file"),
    (TriggerRule("n", "becomes_true", "m", {"expression": "x.y"}), "not supported"),
    (TriggerRule("n", "becomes_true", "m", {"expression": ""}), "syntax"),
    (TriggerRule("n", "clipboard", "m", condition="1 +"), "syntax"),
])
def test_rule_validation(bad, message):
    with pytest.raises(ValueError, match=message):
        validate_rule(bad)


def test_rule_descriptions():
    assert TriggerRule("n", "macro_finished", "m", {"macro": "A", "result": "error"}).describe() == \
        '"A" finished with error'
    assert TriggerRule("n", "clipboard", "m", {"contains": "http"}).describe() == \
        'the clipboard changes to contain "http"'
    assert TriggerRule("n", "file", "m", {"path": "~/x"}).describe() == "~/x changes"
    assert TriggerRule("n", "becomes_true", "m", {"expression": "a > 1"}).describe() == "a > 1 becomes true"


# ----------------------------------------------------- rules in the service
def test_rules_are_stored(db):
    saved = db.save_trigger(TriggerRule("r", "clipboard", "M", {"contains": "x"}, condition="1", queue=True))
    assert saved.id is not None
    saved.enabled = False
    db.save_trigger(saved)
    [loaded] = db.list_triggers()
    assert loaded.to_dict() == {"name": "r", "kind": "clipboard", "macro": "M", "params": {"contains": "x"},
                                "condition": "1", "queue": True, "enabled": False}
    db.delete_trigger(saved.id)
    assert db.list_triggers() == []


def test_macro_finished_trigger_starts_the_next_macro(svc, backend, db):
    db.save_macro(typing("Build"))
    db.save_macro(typing("Notify"))
    rule_ = svc.save_trigger(TriggerRule("after build", "macro_finished", "notify",
                                         {"macro": "Build", "result": "success"}))
    svc.run(svc.find_macro("Build"))
    assert wait_until(lambda: len(typed(backend)) == 2) and idle(svc)
    assert typed(backend) == ["Build", "Notify"]
    assert db.list_history()[0].trigger == 'trigger "after build": "Build" finished with success'
    assert svc.triggers.status[rule_.id].endswith("started the macro")


def test_trigger_chains_stop_after_the_limit(svc, backend, db):
    db.save_macro(typing("Ping"))
    svc.save_trigger(TriggerRule("again", "macro_finished", "Ping", {"macro": "Ping"}))
    svc.run(svc.find_macro("Ping"))
    assert wait_until(lambda: len(typed(backend)) == MAX_CHAIN + 1, 5) and idle(svc)
    time.sleep(0.1)
    assert len(typed(backend)) == MAX_CHAIN + 1  # the first run plus MAX_CHAIN started by the rule


def test_trigger_reports_missing_and_disabled_macros(svc, db):
    db.save_macro(typing("Build"))
    db.save_macro(typing("Off", enabled=False))
    missing = svc.save_trigger(TriggerRule("m", "macro_finished", "Nope", {"macro": "Build"}))
    disabled = svc.save_trigger(TriggerRule("d", "macro_finished", "Off", {"macro": "Build"}))
    svc.run(svc.find_macro("Build"), blocking=True)
    assert wait_until(lambda: "no macro named" in svc.triggers.status.get(missing.id, ""))
    assert "is disabled" in svc.triggers.status[disabled.id]


def test_trigger_queues_or_skips_when_busy(svc, backend, db):
    db.save_macro(typing("Clip"))
    queued = svc.save_trigger(TriggerRule("q", "clipboard", "Clip", queue=True))
    svc.run(typing("long", delay_ms=300))
    svc._fire_rule(queued, "trigger", 0)
    assert svc.triggers.status[queued.id].endswith("queued: another macro was running")
    skipped = TriggerRule("s", "clipboard", "Clip", id=999)
    svc._fire_rule(skipped, "trigger", 0)
    assert svc.triggers.status[999].endswith("not started: another macro was running")
    assert idle(svc) and typed(backend) == ["long", "Clip"]


def test_trigger_respects_the_macro_limits(svc, db):
    macro = db.save_macro(typing("Limited", limits=Limits(max_runs_per_day=1)))
    svc.run(macro, trigger="hotkey", blocking=True)
    r = TriggerRule("lim", "clipboard", "Limited", id=5)
    svc._fire_rule(r, "trigger", 0)
    assert "not started: it already ran 1 time(s) today" in svc.triggers.status[5]


def test_clipboard_trigger_through_the_service(svc, backend, db):
    db.save_macro(typing("OnCopy"))
    svc.save_trigger(TriggerRule("copy", "clipboard", "OnCopy", {"contains": "@"}))
    svc.ui.clipboard = "start"
    svc.triggers.tick()
    svc.ui.clipboard = "someone@example.com"
    svc.triggers.tick()
    assert wait_until(lambda: typed(backend) == ["OnCopy"])


def test_becomes_true_with_saved_variables(svc, backend, db):
    db.save_macro(typing("Ten"))
    svc.save_trigger(TriggerRule("ten", "becomes_true", "Ten", {"expression": "count >= 10"}))
    svc.saved_variables.set("count", "9")
    svc.triggers.tick()
    svc.saved_variables.set("count", "10")
    svc.triggers.tick()
    assert wait_until(lambda: typed(backend) == ["Ten"])


def test_file_trigger_with_a_real_file(svc, backend, db, tmp_path):
    db.save_macro(typing("Import"))
    path = tmp_path / "drop.csv"
    svc.save_trigger(TriggerRule("drop", "file", "Import", {"path": str(path)}))
    svc.triggers.tick()
    path.write_text("data")
    svc.triggers.tick()
    assert wait_until(lambda: typed(backend) == ["Import"])


def test_autorun_switch_turns_rules_off(svc, backend, db):
    db.save_macro(typing("Clip"))
    svc.save_trigger(TriggerRule("c", "clipboard", "Clip"))
    assert svc.triggers.active_count() == 1
    svc.set_autorun_enabled(False)
    assert svc.triggers.active_count() == 0
    svc.set_autorun_enabled(True)
    assert svc.triggers.active_count() == 1


def test_delete_and_rename(svc, db):
    r = svc.save_trigger(TriggerRule("c", "macro_finished", "Old", {"macro": "Old"}))
    other = svc.save_trigger(TriggerRule("d", "clipboard", "Other"))
    assert svc.rename_in_triggers("old", "New") == 1
    [renamed] = [x for x in db.list_triggers() if x.id == r.id]
    assert renamed.macro == "New" and renamed.params["macro"] == "New"
    svc.delete_trigger(other.id)
    assert [x.name for x in db.list_triggers()] == ["c"]
    with pytest.raises(ValueError):
        svc.save_trigger(TriggerRule("", "clipboard", "x"))


def test_debug_runs_do_not_fire_triggers(svc, backend, db):
    db.save_macro(typing("Main"))
    db.save_macro(typing("Next"))
    svc.save_trigger(TriggerRule("n", "macro_finished", "Next", {"macro": "Main"}))
    svc.run(svc.find_macro("Main"), trigger="debug", debugger=Debugger(), blocking=True)
    time.sleep(0.1)
    assert idle(svc) and typed(backend) == ["Main"]


def test_service_evaluate(svc):
    svc.saved_variables.set("n", "4")
    svc.ui.clipboard = "copied"
    assert svc.evaluate("n * 2") == 8
    assert svc.evaluate("upper(clipboard)") == "COPIED"
    assert svc.evaluate("app") == "Terminal"
    with pytest.raises(ValueError, match="unknown variable nope"):
        svc.evaluate("nope")


# ------------------------------------------------------- debug via service
def test_debug_run_with_simulated_input(svc, backend):
    debugger = Debugger({(1,)})
    macro = Macro("Dbg", [A("type_text", text="a"), A("type_text", text="b")])
    assert svc.run(macro, trigger="debug", debugger=debugger, simulate=True)
    assert wait_until(lambda: svc.paused)
    state = svc.live_state()
    assert state["debugging"] and state["stopped_at"] == (1,) and state["macro"] == "Dbg"
    assert state["simulated"] == [(pytest.approx(state["simulated"][0][0]), "type 'a'")]
    assert ("paused", macro) in svc.events_seen
    svc.debug_continue()
    assert idle(svc)
    assert backend.calls == []  # simulated: nothing was typed for real
    assert svc.db.list_history()[0].trigger == "debug"


def test_debug_step_through_the_service(svc, backend):
    debugger = Debugger(stop_at_start=True)
    svc.run(Macro("Dbg", [A("type_text", text="a"), A("type_text", text="b")]), trigger="debug",
            debugger=debugger)
    assert wait_until(lambda: svc.paused and debugger.stopped_at == (0,))
    svc.debug_step()
    assert wait_until(lambda: svc.paused and debugger.stopped_at == (1,))
    assert typed(backend) == ["a"]  # not simulated: real input
    svc.debug_step()
    assert idle(svc) and typed(backend) == ["a", "b"]


def test_live_state(svc, backend):
    assert svc.live_state() is None
    svc.run(Macro("Live", [A("set_variable", name="v", value="1"), A("delay", ms=5000)]), trigger="manual")
    assert wait_until(lambda: (svc.live_state() or {}).get("step") == (1,))
    state = svc.live_state()
    assert state["macro"] == "Live" and state["trigger"] == "manual" and not state["paused"]
    assert not state["debugging"] and state["stopped_at"] is None and state["simulated"] is None
    assert state["variables"] == {"v": "1"}
    svc.pause()
    assert svc.live_state()["paused"]
    svc.stop()
    assert idle(svc) and svc.live_state() is None


def test_debug_continue_without_a_debugger_just_resumes(svc, backend):
    svc.run(typing("plain", delay_ms=200))
    svc.pause()
    svc.debug_continue()
    assert not svc.paused
    svc.debug_step()  # nothing to step: harmless
    assert idle(svc) and typed(backend) == ["plain"]


# ------------------------------------------------------------- pre-flight
def test_preflight_of_trigger_rules():
    macros = [Macro("Build", []), Macro("Off", [], enabled=False)]
    rules = [
        TriggerRule("missing", "clipboard", "Nope"),
        TriggerRule("off", "clipboard", "Off"),
        TriggerRule("watch missing", "macro_finished", "Build", {"macro": "Ghost"}),
        TriggerRule("self", "macro_finished", "Build", {"macro": "build"}),
        TriggerRule("broken", "becomes_true", "Build", {"expression": "1 +"}),
        TriggerRule("disabled", "clipboard", "Nope", enabled=False),
        TriggerRule("fine", "clipboard", "Build"),
    ]
    issues = {(i.macro_name, i.severity): i.message for i in check_triggers(rules, macros)}
    assert issues == {
        ("trigger: missing", ERROR): 'there is no macro named "Nope" to run',
        ("trigger: off", WARNING): '"Off" is disabled, so this trigger does not run it',
        ("trigger: watch missing", WARNING): 'there is no macro named "Ghost", so this trigger never fires',
        ("trigger: self", INFO): f'"Build" starts itself again when it finishes (at most {MAX_CHAIN} times in a row)',
        ("trigger: broken", ERROR): "syntax error: invalid syntax",
    }
    everything = check_all(macros, CheckContext(macros=macros, triggers=rules))
    assert any(i.macro_name == "trigger: missing" for i in everything)


def test_preflight_of_scripts_and_expressions():
    from keyboardmanager.core.preflight import check_macro

    s = A("script", code="total = price * 2\ntype(total + tax)")
    cond = catalog.new_action("if")
    cond.params.update(kind="expression", expression="total > limit")
    typed_ = A("type_text", text="{= total + 1} {= 1 +}")
    calc = A("set_variable", name="x", value="undefined_thing + 1", mode="expression")
    macro = Macro("M", [A("set_variable", name="price", value="2"), s, cond, typed_, calc])
    issues = [(i.severity, i.message) for i in check_macro(macro, CheckContext(macros=[macro]))]
    assert (WARNING, '"tax" is never set: the script stops the macro with an error when it reads it') in issues
    assert (WARNING, '"limit" is never set: the expression stops the macro with an error when it reads it') in issues
    assert (WARNING, '"undefined_thing" is never set: the expression stops the macro with an error '
                     "when it reads it") in issues
    assert any(sev == ERROR and msg.startswith("{= 1 +}: syntax error") for sev, msg in issues)
    # variables the script assigns count as set for the later steps
    assert not any('"total"' in msg or '"price"' in msg for _, msg in issues)


def test_each_queued_run_is_announced(db, backend):
    calls = []
    service = MacroService(db, backend, FakeHotkeys(), listener=lambda e, p: calls.append(e),
                           scheduler_thread=False)
    service.run(typing("a", delay_ms=50))
    service.run(typing("b"), queue=True)
    assert wait_until(lambda: service.running_macro is None and not service.queue_snapshot())
    assert wait_until(lambda: calls.count("finished") == 2)
    assert calls.count("started") == 2
    # the queue shrinks when the queued run starts
    assert calls.index("queue", calls.index("finished")) < len(calls)
    service.shutdown()
