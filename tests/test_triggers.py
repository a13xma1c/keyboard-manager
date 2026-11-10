"""Hotkey sequences (multi-chord shortcuts) and event triggers."""

import pytest

from keyboardmanager.core import actions as catalog
from keyboardmanager.core import events as events_mod
from keyboardmanager.core.events import EventWatcher, validate_event
from keyboardmanager.core.hotkeys import SEQUENCE_TIMEOUT_S, SequenceMatcher
from keyboardmanager.core.models import ActiveWindow, Macro
from keyboardmanager.core.service import MacroService
from tests_support import FakeHotkeys, wait_until


# ------------------------------------------------------------ sequence matcher
@pytest.fixture
def matcher():
    now = [0.0]
    m = SequenceMatcher(clock=lambda: now[0])
    m.now = now
    return m


def test_single_chord_hotkeys_fire_immediately(matcher):
    assert matcher.configure(["ctrl+1", "ctrl+k ctrl+s"]) == {"ctrl+1", "ctrl+k", "ctrl+s"}
    assert matcher.press("ctrl+1") == "ctrl+1"
    assert matcher.press("ctrl+s") is None  # only a continuation, nothing on its own


def test_two_chord_sequence(matcher):
    matcher.configure(["ctrl+k ctrl+s", "ctrl+k ctrl+a"])
    assert matcher.press("ctrl+k") is None  # waiting for the second chord
    assert matcher.press("ctrl+s") == "ctrl+k ctrl+s"
    assert matcher.press("ctrl+s") is None  # the prefix was consumed
    assert matcher.press("ctrl+k") is None
    assert matcher.press("ctrl+a") == "ctrl+k ctrl+a"


def test_sequence_times_out_between_chords(matcher):
    matcher.configure(["ctrl+k ctrl+s"])
    matcher.press("ctrl+k")
    matcher.now[0] += SEQUENCE_TIMEOUT_S + 0.1
    assert matcher.press("ctrl+s") is None
    matcher.press("ctrl+k")
    matcher.now[0] += SEQUENCE_TIMEOUT_S - 0.1
    assert matcher.press("ctrl+s") == "ctrl+k ctrl+s"


def test_wrong_continuation_can_start_a_new_sequence(matcher):
    matcher.configure(["ctrl+k ctrl+s", "ctrl+j x"])
    matcher.press("ctrl+k")
    assert matcher.press("ctrl+j") is None  # not a continuation, but a fresh start
    assert matcher.press("x") == "ctrl+j x"
    matcher.press("ctrl+k")
    assert matcher.press("ctrl+1") is None  # matches nothing at all: state cleared
    assert matcher.press("ctrl+s") is None


def test_three_chord_sequence_and_exact_match_priority(matcher):
    matcher.configure(["ctrl+k ctrl+s x", "ctrl+k"])  # conflicting, as from an old file
    assert matcher.press("ctrl+k") == "ctrl+k"  # the exact hotkey wins immediately


# ------------------------------------------------------- sequences in services
def seq_macro(db, name, hotkey, text=None):
    m = Macro(name, [catalog.new_action("type_text")], hotkey=hotkey)
    m.actions[0].params["text"] = text or name
    return db.save_macro(m)


@pytest.fixture
def svc(db, backend):
    now = [1000.0]
    service = MacroService(db, backend, FakeHotkeys(), clock=lambda: now[0], scheduler_thread=False)
    service.now = now
    yield service
    service.shutdown()


def press(svc, *chords):
    for chord in chords:
        svc.hotkeys.mapping[chord]()


def ran(svc, backend):
    assert wait_until(lambda: svc.running_macro is None and backend.calls)
    calls, backend.calls = list(backend.calls), []
    return [text for kind, text in calls if kind == "type"]


def test_sequence_hotkey_runs_macro(svc, db, backend):
    seq_macro(db, "save all", "ctrl+k ctrl+s")
    seq_macro(db, "quick", "ctrl+1")
    svc.reload()
    # the backend registers single chords only: they work on every platform backend
    assert {"ctrl+k", "ctrl+s", "ctrl+1"} <= set(svc.hotkeys.mapping)
    press(svc, "ctrl+k", "ctrl+s")
    assert ran(svc, backend) == ["save all"]
    press(svc, "ctrl+1")
    assert ran(svc, backend) == ["quick"]
    assert db.list_history()[1].trigger == "hotkey ctrl+k ctrl+s"


def test_sequence_respects_the_timeout(svc, db, backend):
    seq_macro(db, "slowpoke", "ctrl+k ctrl+s")
    svc.reload()
    press(svc, "ctrl+k")
    svc.now[0] += SEQUENCE_TIMEOUT_S + 1
    press(svc, "ctrl+s")
    assert backend.calls == [] and db.list_history() == []


def test_app_specific_and_global_sequence_share_a_hotkey(svc, db, backend):
    seq_macro(db, "everywhere", "ctrl+k ctrl+s")
    m = seq_macro(db, "只 chrome", "ctrl+k ctrl+s")
    m.app_filter = "chrome"
    db.save_macro(m)
    svc.reload()
    backend.window = ActiveWindow("Google Chrome", "")
    press(svc, "ctrl+k", "ctrl+s")
    assert ran(svc, backend) == ["只 chrome"]


def test_reserved_chord_inside_a_macro_sequence_is_reported(svc, db, backend):
    seq_macro(db, "mine", "ctrl+k ctrl+alt+f12")  # second chord = the stop hotkey
    failures = svc.reload()
    assert "ctrl+alt+f12" in failures and '"mine"' in failures["ctrl+alt+f12"]
    press(svc, "ctrl+k", "ctrl+alt+f12")  # the macro keeps working...
    assert ran(svc, backend) == ["mine"]
    assert "ctrl+alt+f11" in svc.hotkeys.mapping  # ...pause is unaffected


def test_failed_chord_is_reported_against_its_sequences(db, backend):
    service = MacroService(db, backend, FakeHotkeys(failures={"ctrl+k": "grabbed elsewhere"}),
                           scheduler_thread=False)
    seq_macro(db, "a", "ctrl+k ctrl+s")
    assert service.reload() == {"ctrl+k ctrl+s": "grabbed elsewhere"}
    service.shutdown()


# ------------------------------------------------------------------- events
def w(app, title=""):
    return ActiveWindow(app, title)


def test_validate_event():
    validate_event("", Macro("m"))
    validate_event("app_start", Macro("m"))
    validate_event("focus_gained", Macro("m", app_filter="chrome"))
    with pytest.raises(ValueError, match="unknown"):
        validate_event("on_tuesdays", Macro("m"))
    with pytest.raises(ValueError, match="filter"):
        validate_event("focus_lost", Macro("m"))  # would match every window


def test_watcher_fires_on_focus_transitions():
    windows = [w("Google Chrome", "Start")]  # the watched app is already focused
    runs = []
    watcher = EventWatcher(active_window=lambda: windows[0], run=lambda m, t: runs.append((m.name, t)))
    gained = Macro("hello", app_filter="chrome", event="focus_gained")
    lost = Macro("bye", app_filter="chrome", event="focus_lost")
    watcher.update([gained, lost, Macro("not an event macro")], thread=False)
    watcher.check()  # the first look must not count as gaining focus
    watcher.check()
    assert runs == []
    windows[0] = w("Terminal")
    watcher.check()
    assert runs == [("bye", "event: When its application loses focus")]
    runs.clear()
    windows[0] = w("Google Chrome", "Inbox")
    watcher.check()
    assert runs == [("hello", "event: When its application gains focus")]
    windows[0] = w("Google Chrome", "Other tab")  # same app: no transition
    watcher.check()
    assert runs[1:] == []


def test_watcher_thread_only_runs_while_needed():
    watcher = EventWatcher(active_window=lambda: w("x"), run=lambda m, t: True, interval=0.01)
    watcher.update([Macro("m", app_filter="a", event="focus_gained")])
    assert watcher._thread is not None and watcher._thread.is_alive()
    watcher.update([Macro("m", event="app_start")])  # app_start needs no polling
    assert watcher._thread is None
    watcher.stop()


def test_service_runs_focus_event_macros(svc, db, backend):
    m = Macro("greet chrome", [catalog.new_action("type_text")], app_filter="chrome", event="focus_gained")
    m.actions[0].params["text"] = "hi"
    db.save_macro(m)
    svc.reload()
    svc.events.check()
    backend.window = ActiveWindow("Google Chrome", "")
    svc.events.check()
    assert ran(svc, backend) == ["hi"]
    entry = db.list_history()[0]
    assert (entry.macro_name, entry.trigger) == ("greet chrome", "event: When its application gains focus")
    svc.set_autorun_enabled(False)  # the toolbar toggle also covers events
    backend.window = ActiveWindow("Terminal", "")
    svc.events.check()
    backend.window = ActiveWindow("Google Chrome", "")
    svc.events.check()
    assert backend.calls == []


def test_startup_event(svc, db, backend):
    m = Macro("hello", [catalog.new_action("type_text")], event="app_start")
    m.actions[0].params["text"] = "good morning"
    db.save_macro(m)
    db.save_macro(Macro("scoped", event="app_start", app_filter="nothing-focused"))
    db.save_macro(Macro("off", event="app_start", enabled=False))
    svc.fire_startup_event()
    assert ran(svc, backend) == ["good morning"]
    assert [e.macro_name for e in db.list_history()] == ["hello"]
    svc.set_autorun_enabled(False)
    svc.fire_startup_event()
    assert backend.calls == []


def test_disabled_and_eventless_macros_are_not_watched(svc, db):
    db.save_macro(Macro("m", app_filter="x", event="focus_gained", enabled=False))
    db.save_macro(Macro("n", app_filter="x"))
    svc.reload()
    assert svc.events._macros == []


def test_event_round_trip_and_validation(db):
    import json

    from keyboardmanager.core import io as macro_io

    m = db.save_macro(Macro("m", app_filter="mail", event="focus_lost"))
    assert db.get_macro(m.id).event == "focus_lost"
    doc = macro_io.export_to_dict([m])
    assert doc["macros"][0]["event"] == "focus_lost"
    doc["macros"].append({"name": "bad", "actions": [], "event": "focus_gained"})  # no filter
    doc["macros"].append({"name": "worse", "actions": [], "event": "explosion"})
    result = macro_io.import_from_text(json.dumps(doc), db)
    assert [x.name for x in result.imported] == ["m (2)"] and result.scheduled == 1
    assert len(result.skipped) == 2 and "filter" in result.skipped[0] and "unknown" in result.skipped[1]


def test_database_tolerates_events_from_other_versions(db):
    m = db.save_macro(Macro("m", app_filter="mail", event="focus_gained"))
    db._conn.execute("UPDATE macros SET event='hyperspace' WHERE id=?", (m.id,))
    assert db.get_macro(m.id).event == ""


def test_event_labels_are_user_friendly():
    assert events_mod.describe_event("") == "Not on an event"
    for label in events_mod.EVENTS.values():
        assert label and label[0].isupper()
