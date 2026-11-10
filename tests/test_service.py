import threading

from keyboardmanager.core import actions as catalog
from keyboardmanager.core.backend import pynput_key_to_name
from keyboardmanager.core.models import ActiveWindow, Macro
from keyboardmanager.core.service import MacroService
from tests_support import FakeHotkeys


def make(db, backend, **kw):
    hk = FakeHotkeys()
    events = []
    svc = MacroService(db, backend, hk, listener=lambda e, p: events.append(e))
    return svc, hk, events


def save(db, name, hotkey="", keys_="a", **kw):
    m = Macro(name, [catalog.new_action("key_press")], hotkey=hotkey, **kw)
    m.actions[0].params["keys"] = keys_
    return db.save_macro(m)


def run_blocking(svc, macro):
    assert svc.run(macro, blocking=True)


def test_reload_registers_only_enabled_hotkeys_plus_stop_and_pause(db, backend):
    save(db, "a", "ctrl+1")
    save(db, "b", "ctrl+2", enabled=False)
    save(db, "c")
    svc, hk, _ = make(db, backend)
    svc.reload()
    assert set(hk.mapping) == {"ctrl+1", "ctrl+alt+f12", "ctrl+alt+f11"}
    svc.set_hotkeys_enabled(False)
    assert set(hk.mapping) == {"ctrl+alt+f12", "ctrl+alt+f11"}


def test_hotkey_runs_macro_and_records_history(db, backend):
    save(db, "a", "ctrl+1", keys_="x")
    svc, hk, events = make(db, backend)
    svc.reload()
    hk.mapping["ctrl+1"]()
    for _ in range(100):
        if svc.running_macro is None and "finished" in events:
            break
        threading.Event().wait(0.02)
    assert ("down", "x") in backend.calls
    entry = db.list_history()[0]
    assert (entry.macro_name, entry.status, entry.trigger) == ("a", "success", "hotkey ctrl+1")
    assert entry.actions_run == 1


def test_app_specific_macro_wins_and_filters(db, backend):
    save(db, "global", "ctrl+1", keys_="g")
    save(db, "chrome-only", "ctrl+1", keys_="c", app_filter="chrome")
    svc, hk, _ = make(db, backend)
    svc.reload()
    backend.window = ActiveWindow("Google Chrome", "")
    svc.on_hotkey("ctrl+1")
    while svc.running_macro:
        threading.Event().wait(0.01)
    backend.window = ActiveWindow("Terminal", "")
    svc.on_hotkey("ctrl+1")
    while svc.running_macro:
        threading.Event().wait(0.01)
    assert [c[1] for c in backend.calls if c[0] == "down"] == ["c", "g"]


def test_app_specific_only_does_nothing_elsewhere(db, backend):
    save(db, "scoped", "ctrl+1", app_filter="chrome")
    svc, _, _ = make(db, backend)
    svc.reload()
    svc.on_hotkey("ctrl+1")
    assert backend.calls == [] and db.list_history() == []


def test_only_one_macro_at_a_time_and_stop(db, backend):
    slow = Macro("slow", [catalog.new_action("delay")])
    slow.actions[0].params["ms"] = 10_000
    slow = db.save_macro(slow)
    svc, _, _ = make(db, backend)
    assert svc.run(slow)
    assert not svc.run(slow)
    threading.Event().wait(0.1)
    svc.stop()
    for _ in range(100):
        if svc.running_macro is None:
            break
        threading.Event().wait(0.02)
    assert svc.running_macro is None
    assert db.list_history()[0].status == "stopped"


def test_pressing_hotkey_again_stops_running_macro(db, backend):
    slow = Macro("slow", [catalog.new_action("delay")], hotkey="ctrl+1")
    slow.actions[0].params["ms"] = 10_000
    slow = db.save_macro(slow)
    svc, _, _ = make(db, backend)
    svc.reload()
    svc.on_hotkey("ctrl+1")
    threading.Event().wait(0.1)
    svc.on_hotkey("ctrl+1")
    for _ in range(100):
        if svc.running_macro is None:
            break
        threading.Event().wait(0.02)
    assert db.list_history()[0].status == "stopped"


def test_error_in_macro_is_logged(db, backend):
    backend.key_down = lambda k: (_ for _ in ()).throw(RuntimeError("no permission"))
    m = save(db, "a")
    svc, _, _ = make(db, backend)
    run_blocking(svc, m)
    e = db.list_history()[0]
    assert e.status == "error" and "no permission" in e.message


def test_start_delay_can_be_cancelled(db, backend):
    m = save(db, "a")
    svc, _, _ = make(db, backend)
    svc.run(m, start_delay=30)
    threading.Event().wait(0.1)
    svc.stop()
    for _ in range(100):
        if svc.running_macro is None:
            break
        threading.Event().wait(0.02)
    assert backend.calls == [] and db.list_history()[0].status == "stopped"


def test_stop_hotkey_setting(db, backend):
    svc, hk, _ = make(db, backend)
    svc.set_stop_hotkey("Ctrl+Shift+F11")
    assert "ctrl+shift+f11" in hk.mapping


def test_stale_running_history_is_flagged_on_startup(db, backend):
    m = save(db, "a")
    db.start_history(m, "manual")
    make(db, backend)
    assert db.list_history()[0].status == "error"


def test_pynput_key_translation():
    from pynput.keyboard import Key, KeyCode

    assert pynput_key_to_name(Key.ctrl_l) == "ctrl"
    assert pynput_key_to_name(Key.cmd_r) == "cmd"
    assert pynput_key_to_name(Key.page_up) == "page_up"
    assert pynput_key_to_name(Key.f5) == "f5"
    assert pynput_key_to_name(KeyCode.from_char("A")) == "a"
    assert pynput_key_to_name(KeyCode.from_char(" ")) == "space"
    assert pynput_key_to_name(KeyCode.from_char("\x03")) == "c"
    assert pynput_key_to_name(KeyCode.from_vk(200)) == "vk:200"


def test_hotkey_backend_selection(monkeypatch):
    import sys

    from keyboardmanager.core import hotkeys

    monkeypatch.setattr(sys, "platform", "darwin")
    # `keyboard` aborts the interpreter on import under macOS, so it must never be chosen
    assert isinstance(hotkeys.create_hotkey_backend("keyboard"), hotkeys.PynputHotkeys)
    assert isinstance(hotkeys.create_hotkey_backend("auto"), hotkeys.PynputHotkeys)
    monkeypatch.setattr(sys, "platform", "win32")
    assert isinstance(hotkeys.create_hotkey_backend("auto"), hotkeys.FallbackHotkeys)
    assert isinstance(hotkeys.create_hotkey_backend("keyboard"), hotkeys.KeyboardHotkeys)


# ------------------------------------------------------------------ pause / resume
def wait_until(pred, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if pred():
            return True
        threading.Event().wait(0.01)
    return pred()


def slow_macro(db, hotkey="", ms=150):
    m = Macro("slow", [catalog.new_action("delay"), catalog.new_action("key_press")], hotkey=hotkey)
    m.actions[0].params["ms"] = ms
    m.actions[1].params["keys"] = "z"
    return db.save_macro(m)


def test_pause_and_resume_running_macro(db, backend):
    m = slow_macro(db)
    svc, _, events = make(db, backend)
    assert not svc.pause() and not svc.resume()  # nothing running
    assert svc.run(m)
    assert svc.pause() and svc.paused
    assert not svc.pause()  # already paused
    threading.Event().wait(0.3)
    assert svc.running_macro is not None and backend.calls == []
    assert svc.resume() and not svc.paused
    assert wait_until(lambda: "finished" in events)
    assert ("down", "z") in backend.calls
    assert [e for e in events if e != "hotkeys"] == ["started", "paused", "resumed", "finished"]
    assert db.list_history()[0].status == "success"


def test_pause_hotkey_toggles(db, backend):
    m = slow_macro(db)
    svc, hk, _ = make(db, backend)
    svc.reload()
    svc.run(m)
    hk.mapping["ctrl+alt+f11"]()
    assert svc.paused
    hk.mapping["ctrl+alt+f11"]()
    assert not svc.paused
    assert wait_until(lambda: svc.running_macro is None)
    hk.mapping["ctrl+alt+f11"]()  # nothing running: harmless
    assert not svc.paused


def test_stop_while_paused_records_stopped(db, backend):
    m = slow_macro(db, ms=10_000)
    svc, _, _ = make(db, backend)
    svc.run(m)
    svc.pause()
    threading.Event().wait(0.05)
    svc.stop()
    assert wait_until(lambda: svc.running_macro is None)
    assert not svc.paused
    assert db.list_history()[0].status == "stopped" and backend.calls == []


def test_pause_during_start_countdown(db, backend):
    m = save(db, "a", keys_="q")
    svc, _, _ = make(db, backend)
    svc.run(m, start_delay=0.1)
    svc.pause()
    threading.Event().wait(0.25)
    assert backend.calls == [] and svc.running_macro is not None
    svc.resume()
    assert wait_until(lambda: svc.running_macro is None)
    assert ("down", "q") in backend.calls


def test_pause_hotkey_setting_and_validation(db, backend):
    svc, hk, _ = make(db, backend)
    svc.set_pause_hotkey("Ctrl+Shift+P")
    assert "ctrl+shift+p" in hk.mapping and "ctrl+alt+f11" not in hk.mapping
    assert svc.reserved_hotkeys() == {"ctrl+alt+f12": "stop", "ctrl+shift+p": "pause"}
    try:
        svc.set_pause_hotkey("ctrl+alt+f12")
        raise AssertionError("pause hotkey equal to stop hotkey was accepted")
    except ValueError:
        pass
    try:
        svc.set_stop_hotkey("ctrl+shift+p")
        raise AssertionError("stop hotkey equal to pause hotkey was accepted")
    except ValueError:
        pass
    svc.set_pause_hotkey("")  # disabled
    assert set(hk.mapping) == {"ctrl+alt+f12"} and svc.reserved_hotkeys() == {"ctrl+alt+f12": "stop"}


def test_existing_macro_keeps_hotkey_that_clashes_with_pause_hotkey(db, backend):
    # e.g. a macro created before the pause hotkey existed
    save(db, "mine", "ctrl+alt+f11", keys_="m")
    svc, hk, _ = make(db, backend)
    failures = svc.reload()
    assert "ctrl+alt+f11" in failures and '"mine"' in failures["ctrl+alt+f11"]
    hk.mapping["ctrl+alt+f11"]()
    assert wait_until(lambda: ("down", "m") in backend.calls)


def test_history_is_complete_once_the_macro_is_no_longer_running(db, backend):
    """Callers (GUI, tests) may read the history as soon as running_macro is None."""
    m = save(db, "quick")
    svc, _, _ = make(db, backend)
    for _ in range(30):
        assert svc.run(m)
        assert wait_until(lambda: svc.running_macro is None)
        assert db.list_history()[0].status == "success"


def test_database_failure_does_not_leave_the_service_busy(db, backend, monkeypatch):
    m = save(db, "a")
    svc, _, _ = make(db, backend)

    def broken(*args):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "finish_history", broken)
    monkeypatch.setattr(threading, "excepthook", lambda args: None)  # the worker's error is expected
    svc.run(m)
    assert wait_until(lambda: svc.running_macro is None)
    assert svc.run(m)  # not stuck
    assert wait_until(lambda: svc.running_macro is None)  # ends before the database is closed


def test_shutdown_waits_until_held_keys_are_released(db, backend):
    """On macOS the process can exit right after shutdown (AppKit calls exit()), so a
    running macro must have released what it holds by the time shutdown returns."""
    m = Macro("holding", [catalog.new_action("key_down"), catalog.new_action("delay")])
    m.actions[0].params["key"] = "shift"
    m.actions[1].params["ms"] = 10_000
    svc, hk, _ = make(db, backend)
    svc.run(db.save_macro(m))
    assert wait_until(lambda: ("down", "shift") in backend.calls)
    svc.shutdown()
    assert ("up", "shift") in backend.calls and svc.running_macro is None
    assert db.list_history()[0].status == "stopped" and hk.stopped
    svc.shutdown()  # idempotent: quitting can reach it from several places
