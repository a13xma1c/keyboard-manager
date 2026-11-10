import json
import sqlite3
import time
from datetime import datetime

import pytest

from keyboardmanager.core import actions as catalog
from keyboardmanager.core import io as macro_io
from keyboardmanager.core.database import Database
from keyboardmanager.core.models import Macro
from keyboardmanager.core.schedule import Schedule, format_when
from keyboardmanager.core.scheduler import GRACE_S, Scheduler


def ts(*args):
    return datetime(*args).timestamp()


# --------------------------------------------------------------------- Schedule
def test_validate_normalises_and_rejects():
    assert Schedule("weekly", time="9:5", days=(4, 0, 0)).validate() == Schedule("weekly", time="09:05", days=(0, 4))
    assert Schedule("once", at="2030-01-02 3:04").validate().at == "2030-01-02 03:04"
    for bad in (
        Schedule("weekly", time="25:00"), Schedule("weekly", time="09:00", days=()),
        Schedule("weekly", time="09:00", days=(7,)), Schedule("once", at="tomorrow"),
        Schedule("interval", minutes=0), Schedule("interval", minutes=True), Schedule("hourly"),
    ):
        with pytest.raises(ValueError):
            bad.validate()


def test_dict_round_trip_and_describe():
    for s, text in (
        (Schedule("once", at="2030-01-02 03:04"), "Once on 2030-01-02 03:04"),
        (Schedule("weekly", time="09:00"), "Daily at 09:00"),
        (Schedule("weekly", time="09:00", days=(0, 1, 2, 3, 4)), "Weekdays at 09:00"),
        (Schedule("weekly", time="18:30", days=(0, 2)), "Mon, Wed at 18:30"),
        (Schedule("interval", minutes=15), "Every 15 min"),
        (Schedule("interval", minutes=120), "Every 2 hours"),
    ):
        assert Schedule.from_dict(json.loads(json.dumps(s.to_dict()))) == s
        assert s.describe() == text
    assert Schedule.from_dict(None) == Schedule() == Schedule.from_dict({"kind": "none"})
    with pytest.raises(ValueError):
        Schedule.from_dict({"kind": "weekly", "time": "09:00", "days": "mon"})


def test_next_after_weekly_and_once():
    wed_noon = ts(2030, 1, 2, 12, 0)  # a Wednesday
    daily9 = Schedule("weekly", time="09:00")
    assert daily9.next_after(wed_noon) == ts(2030, 1, 3, 9, 0)
    assert daily9.next_after(ts(2030, 1, 2, 8, 0)) == ts(2030, 1, 2, 9, 0)
    assert daily9.next_after(ts(2030, 1, 2, 9, 0)) == ts(2030, 1, 3, 9, 0)  # strictly after
    mondays = Schedule("weekly", time="09:00", days=(0,))
    assert mondays.next_after(wed_noon) == ts(2030, 1, 7, 9, 0)
    once = Schedule("once", at="2030-01-02 13:00")
    assert once.next_after(wed_noon) == ts(2030, 1, 2, 13, 0)
    assert once.next_after(ts(2030, 1, 2, 13, 0)) is None


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs time.tzset (POSIX)")
def test_daylight_saving_transitions(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        # 2030-03-10 02:30 does not exist (clocks jump 02:00 -> 03:00): still runs, at 03:30
        daily = Schedule("weekly", time="02:30")
        due = daily.next_after(ts(2030, 3, 10, 0, 0))
        assert datetime.fromtimestamp(due).strftime("%H:%M") == "03:30"
        # the day after, back to the normal 02:30
        nxt = daily.next_after(due)
        assert datetime.fromtimestamp(nxt).strftime("%d %H:%M") == "11 02:30"
        # 2030-11-03 01:30 happens twice (clocks go back): runs only once that day
        daily = Schedule("weekly", time="01:30")
        first = daily.next_after(ts(2030, 11, 3, 0, 0))
        again = daily.next_after(first)
        assert datetime.fromtimestamp(again).day == 4
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_format_when():
    now = ts(2030, 1, 2, 12, 0)  # Wednesday
    assert format_when(ts(2030, 1, 2, 15, 0), now) == "today 15:00"
    assert format_when(ts(2030, 1, 3, 9, 0), now) == "tomorrow 09:00"
    assert format_when(ts(2030, 1, 6, 9, 0), now) == "Sun 09:00"
    assert format_when(ts(2030, 2, 6, 9, 0), now) == "2030-02-06 09:00"


# -------------------------------------------------------------------- Scheduler
class Harness:
    def __init__(self, now):
        self.now = now
        self.runs: list[tuple[str, str]] = []
        self.skipped: list[tuple[str, str]] = []
        self.busy = False
        self.match = True
        self.changes = 0
        self.sched = Scheduler(
            run=self._run, matches=lambda m: self.match,
            record_skipped=lambda m, trig, msg: self.skipped.append((m.name, msg)),
            on_change=self._changed, clock=lambda: self.now,
        )

    def _run(self, macro, trigger):
        if self.busy:
            return False
        self.runs.append((macro.name, trigger))
        return True

    def _changed(self):
        self.changes += 1

    def at(self, t):
        self.now = t
        self.sched.tick()


def macro(name, schedule, id_=1):
    return Macro(name, schedule=schedule, id=id_)


def test_weekly_runs_at_its_time_once_per_day():
    h = Harness(ts(2030, 1, 2, 8, 0))
    h.sched.update([macro("m", Schedule("weekly", time="09:00"))])
    h.at(ts(2030, 1, 2, 8, 59, 59))
    assert h.runs == []
    h.at(ts(2030, 1, 2, 9, 0, 0))
    h.at(ts(2030, 1, 2, 9, 0, 1))
    assert h.runs == [("m", "schedule: Daily at 09:00")] and h.changes == 1
    assert h.sched.next_run(1) == ts(2030, 1, 3, 9, 0)


def test_busy_retries_within_grace_then_skips():
    h = Harness(ts(2030, 1, 2, 8, 0))
    h.sched.update([macro("m", Schedule("weekly", time="09:00"))])
    h.busy = True
    h.at(ts(2030, 1, 2, 9, 0, 0))
    h.at(ts(2030, 1, 2, 9, 0, 30))
    h.busy = False
    h.at(ts(2030, 1, 2, 9, 0, 31))  # the other macro ended: runs late, once
    assert len(h.runs) == 1 and h.skipped == []

    h.busy = True
    h.at(ts(2030, 1, 3, 9, 0, 0))
    h.at(ts(2030, 1, 3, 9, 0) + GRACE_S + 1)
    assert len(h.runs) == 1
    assert h.skipped == [("m", "Skipped: another macro was still running")]
    assert h.sched.next_run(1) == ts(2030, 1, 4, 9, 0)


def test_missed_while_asleep_is_not_caught_up():
    h = Harness(ts(2030, 1, 2, 8, 0))
    h.sched.update([macro("m", Schedule("weekly", time="09:00"))])
    h.at(ts(2030, 1, 2, 11, 0))  # woke up two hours late
    assert h.runs == [] and len(h.skipped) == 1 and "missed" in h.skipped[0][1]
    assert h.sched.next_run(1) == ts(2030, 1, 3, 9, 0)


def test_interval_keeps_phase_and_collapses_missed_runs():
    start = ts(2030, 1, 2, 8, 0)
    h = Harness(start)
    h.sched.update([macro("m", Schedule("interval", minutes=10))])
    assert h.sched.next_run(1) == start + 600  # first run one interval after activation
    h.at(start + 600)
    h.at(start + 1200 + 5)
    assert len(h.runs) == 2
    assert h.sched.next_run(1) == start + 1800  # phase kept although run 5 s late
    h.at(start + 5 * 3600 + 7)  # asleep for hours: one skipped record, not thirty
    assert len(h.skipped) == 1 and len(h.runs) == 2
    assert h.sched.next_run(1) == start + 5 * 3600 + 600


def test_filter_mismatch_passes_silently():
    h = Harness(ts(2030, 1, 2, 8, 0))
    h.sched.update([macro("m", Schedule("weekly", time="09:00"))])
    h.match = False
    h.at(ts(2030, 1, 2, 9, 0))
    assert h.runs == [] and h.skipped == [] and h.sched.next_run(1) == ts(2030, 1, 3, 9, 0)


def test_once_runs_once_and_past_once_never():
    h = Harness(ts(2030, 1, 2, 8, 0))
    h.sched.update([macro("a", Schedule("once", at="2030-01-02 09:00"), 1),
                    macro("past", Schedule("once", at="2030-01-01 09:00"), 2)])
    assert h.sched.next_run(2) is None
    h.at(ts(2030, 1, 2, 9, 0))
    h.at(ts(2030, 1, 3, 9, 0))
    assert h.runs == [("a", "schedule: Once on 2030-01-02 09:00")]
    assert h.sched.next_run(1) is None and h.sched.upcoming() == []


def test_update_keeps_unchanged_and_resets_edited():
    start = ts(2030, 1, 2, 8, 0)
    h = Harness(start)
    h.sched.update([macro("m", Schedule("interval", minutes=10))])
    h.now = start + 300
    h.sched.update([macro("m renamed", Schedule("interval", minutes=10))])  # e.g. another save
    assert h.sched.next_run(1) == start + 600
    h.at(start + 600)
    assert h.runs[0][0] == "m renamed"  # edited macro picked up
    h.sched.update([macro("m", Schedule("interval", minutes=5))])
    assert h.sched.next_run(1) == start + 600 + 300
    h.sched.update([])
    assert h.sched.next_run(1) is None


# ------------------------------------------------------------ service / storage
def test_service_runs_scheduled_macros(db, backend):
    from keyboardmanager.core.service import MacroService

    now = [ts(2030, 1, 2, 8, 0)]
    svc = MacroService(db, backend, None, clock=lambda: now[0], scheduler_thread=False)
    m = Macro("typer", [catalog.new_action("type_text")], schedule=Schedule("weekly", time="09:00"))
    m.actions[0].params["text"] = "hi"
    db.save_macro(m)
    off = db.save_macro(Macro("off", schedule=Schedule("weekly", time="09:00"), enabled=False))
    scoped = db.save_macro(Macro("scoped", schedule=Schedule("weekly", time="09:00"), app_filter="chrome"))
    svc.reload()
    assert svc.next_run(m.id) == ts(2030, 1, 2, 9, 0)
    assert svc.next_run(off.id) is None  # disabled macros are not scheduled
    now[0] = ts(2030, 1, 2, 9, 0)
    svc.scheduler.tick()
    for _ in range(200):
        if svc.running_macro is None and db.list_history():
            break
        time.sleep(0.01)
    entries = db.list_history()
    assert [(e.macro_name, e.status, e.trigger) for e in entries] == [
        ("typer", "success", "schedule: Daily at 09:00")]
    assert ("type", "hi") in backend.calls
    assert svc.next_run(scoped.id) == ts(2030, 1, 3, 9, 0)  # passed over: filter did not match

    svc.set_autorun_enabled(False)
    assert svc.next_run(m.id) is None and svc.scheduler.upcoming() == []
    svc.shutdown()


def test_service_records_skipped_run(db, backend):
    from keyboardmanager.core.service import MacroService

    now = [ts(2030, 1, 2, 8, 0)]
    events = []
    svc = MacroService(db, backend, None, listener=lambda e, p: events.append(e),
                       clock=lambda: now[0], scheduler_thread=False)
    db.save_macro(Macro("m", schedule=Schedule("weekly", time="09:00")))
    svc.reload()
    now[0] = ts(2030, 1, 2, 10, 0)
    svc.scheduler.tick()
    e = db.list_history()[0]
    assert (e.status, e.trigger) == ("skipped", "schedule: Daily at 09:00") and "missed" in e.message
    assert "skipped" in events and "schedule" in events


def test_scheduler_thread_starts_and_stops(db, backend):
    from keyboardmanager.core.service import MacroService

    svc = MacroService(db, backend, None)
    svc.reload()
    assert svc.scheduler._thread is not None and svc.scheduler._thread.is_alive()
    svc.shutdown()
    assert svc.scheduler._thread is None


def test_schedule_persists(db):
    m = db.save_macro(Macro("m", schedule=Schedule("weekly", time="07:15", days=(5, 6))))
    assert db.get_macro(m.id).schedule == Schedule("weekly", time="07:15", days=(5, 6))
    m.schedule = Schedule()
    db.save_macro(m)
    assert db.get_macro(m.id).schedule == Schedule()


def test_database_from_older_version_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE macros (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', hotkey TEXT NOT NULL DEFAULT '',
            app_filter TEXT NOT NULL DEFAULT '', window_filter TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1, actions TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        INSERT INTO macros (name, created_at, updated_at) VALUES ('legacy', 'x', 'x');
    """)
    conn.commit()
    conn.close()
    db = Database(path)
    legacy = db.list_macros()[0]
    assert legacy.name == "legacy" and legacy.schedule == Schedule() and legacy.event == ""
    legacy.event = "app_start"
    legacy.schedule = Schedule("interval", minutes=5)
    db.save_macro(legacy)
    db.close()
    reopened = Database(path).list_macros()[0]
    assert reopened.schedule.minutes == 5 and reopened.event == "app_start"


def test_export_import_schedule(db):
    plain = Macro("plain")
    timed = Macro("timed", schedule=Schedule("weekly", time="09:00", days=(0,)))
    doc = macro_io.export_to_dict([plain, timed])
    assert "schedule" not in doc["macros"][0]  # unchanged format for unscheduled macros
    assert doc["macros"][1]["schedule"] == {"kind": "weekly", "time": "09:00", "days": [0]}
    doc["macros"].append({"name": "broken", "actions": [], "schedule": {"kind": "weekly", "time": "99:00"}})
    result = macro_io.import_from_text(json.dumps(doc), db)
    assert [m.name for m in result.imported] == ["plain", "timed"] and result.scheduled == 1
    assert len(result.skipped) == 1 and "broken" in result.skipped[0]
    # a malformed schedule skips that macro instead of aborting the whole import
    bad = {"format": "keyboardmanager-macros", "version": 1, "macros": [
        {"name": "odd days", "actions": [], "schedule": {"kind": "weekly", "time": "09:00", "days": [0, "mon"]}},
        {"name": "fine", "actions": []}]}
    result = macro_io.import_from_text(json.dumps(bad), db)
    assert [m.name for m in result.imported] == ["fine"] and "odd days" in result.skipped[0]
    assert {m.name: m for m in db.list_macros()}["timed"].schedule.days == (0,)


@pytest.mark.parametrize("data,problem", [
    ([], "object"), ({"kind": 5}, "object"), ({"kind": "once", "at": 20300101}, "text"),
    ({"kind": "weekly", "time": "09:00", "days": [0, "mon"]}, "Monday"),
    ({"kind": "interval", "minutes": 10_081}, "1 to"), ({"kind": "weekly", "time": "9"}, "HH:MM"),
    ({"kind": "once", "at": "2030-13-01 00:00"}, "YYYY-MM-DD"),
])
def test_invalid_schedule_documents(data, problem):
    with pytest.raises(ValueError, match=problem):
        Schedule.from_dict(data)


def test_describe_more_cases():
    assert Schedule("weekly", time="10:00", days=(5, 6)).describe() == "Weekends at 10:00"
    assert Schedule("interval", minutes=1440).describe() == "Every 1 day"
    assert Schedule("interval", minutes=60).describe() == "Every 1 hour"
    assert Schedule().describe() == "Not scheduled" and Schedule().to_dict() == {"kind": "none"}
    assert Schedule("interval", minutes=5).next_after(0) is None  # computed by the scheduler instead


def test_abbreviation_persists_and_migrates(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE macros (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', hotkey TEXT NOT NULL DEFAULT '',
            app_filter TEXT NOT NULL DEFAULT '', window_filter TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1, actions TEXT NOT NULL DEFAULT '[]',
            schedule TEXT NOT NULL DEFAULT '', event TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        INSERT INTO macros (name, created_at, updated_at) VALUES ('before snippets', 'x', 'x');
    """)
    conn.commit()
    conn.close()
    db = Database(path)
    m = db.list_macros()[0]
    assert m.abbreviation == ""
    m.abbreviation = ";hi"
    db.save_macro(m)
    assert Database(path).list_macros()[0].abbreviation == ";hi"
    doc = macro_io.export_to_dict([m])
    assert doc["macros"][0]["abbreviation"] == ";hi"
    assert Macro.from_dict(doc["macros"][0]).abbreviation == ";hi"
    with pytest.raises(ValueError):
        Macro.from_dict({"name": "x", "abbreviation": "has space"})
