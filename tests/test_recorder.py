from keyboardmanager.core.recorder import RecordOptions, build_actions


def kd(t, k): return (t, "key_down", k)
def ku(t, k): return (t, "key_up", k)


def types(actions):
    return [(a.type, a.params) for a in actions]


def test_typed_text_is_coalesced_with_shift():
    ev = [kd(0, "shift"), kd(0.01, "h"), ku(0.02, "h"), ku(0.03, "shift"),
          kd(0.1, "i"), ku(0.11, "i"), kd(0.2, "space"), ku(0.21, "space"), kd(0.3, "!"), ku(0.31, "!")]
    out = build_actions(ev)
    assert len(out) == 1 and out[0].type == "type_text" and out[0].params["text"] == "hi !"


def test_shortcut_with_modifiers():
    ev = [kd(0, "ctrl"), kd(0.01, "shift"), kd(0.02, "t"), ku(0.03, "t"), ku(0.04, "shift"), ku(0.05, "ctrl")]
    out = build_actions(ev)
    assert [(a.type, a.params["keys"]) for a in out] == [("key_press", "ctrl+shift+t")]


def test_lone_modifier_and_special_keys():
    ev = [kd(0, "shift"), ku(0.02, "shift"), kd(0.1, "enter"), ku(0.11, "enter")]
    out = build_actions(ev, RecordOptions(record_delays=False))
    assert [a.params["keys"] for a in out] == ["shift", "enter"]


def test_auto_repeat_ignored():
    ev = [kd(0, "a"), kd(0.03, "a"), kd(0.06, "a"), ku(0.07, "a")]
    assert build_actions(ev)[0].params["text"] == "a"


def test_pauses_become_delays_but_short_ones_do_not():
    ev = [kd(0, "enter"), ku(0.01, "enter"), kd(0.03, "tab"), ku(0.04, "tab"), kd(1.5, "esc"), ku(1.51, "esc")]
    out = build_actions(ev)
    kinds = [a.type for a in out]
    assert kinds == ["key_press", "key_press", "delay", "key_press"]
    assert 1400 <= out[2].params["ms"] <= 1500
    assert "delay" not in [a.type for a in build_actions(ev, RecordOptions(record_delays=False))]


def test_clicks_and_double_click():
    ev = [(0, "click", (10, 20, "left", True)), (0.05, "click", (10, 20, "left", False)),
          (0.2, "click", (10, 20, "left", True)), (0.25, "click", (10, 20, "left", False))]
    out = build_actions(ev)
    assert len(out) == 1
    p = out[0].params
    assert (out[0].type, p["button"], p["count"], p["x"], p["y"], p["move_to"]) == ("mouse_click", "left", 2, 10, 20, True)


def test_drag_becomes_press_and_release():
    ev = [(0, "click", (0, 0, "left", True)), (0.5, "click", (100, 50, "left", False))]
    out = build_actions(ev, RecordOptions(record_delays=False))
    assert [(a.params["state"], a.params["x"], a.params["y"]) for a in out] == [("press", 0, 0), ("release", 100, 50)]


def test_scroll_events_merge():
    ev = [(0, "scroll", (0, 0, 0, -1)), (0.1, "scroll", (0, 0, 0, -1)), (0.15, "scroll", (0, 0, 0, -2))]
    out = build_actions(ev)
    assert len(out) == 1 and out[0].params["dy"] == -4


def test_moves_only_when_enabled():
    ev = [(0, "move", (1, 1)), (0.2, "move", (5, 5))]
    assert build_actions(ev) == []
    out = build_actions(ev, RecordOptions(record_moves=True, record_delays=False))
    assert [(a.params["x"], a.params["y"]) for a in out] == [(1, 1), (5, 5)]


def test_shift_click_holds_modifier_explicitly():
    ev = [kd(0, "shift"), (0.1, "click", (3, 3, "left", True)), (0.15, "click", (3, 3, "left", False)), ku(0.3, "shift")]
    out = build_actions(ev, RecordOptions(record_delays=False))
    assert [a.type for a in out] == ["key_down", "mouse_click", "key_up"]


def test_recorded_actions_are_valid_and_replayable(backend):
    from keyboardmanager.core import actions as catalog
    from keyboardmanager.core.executor import MacroExecutor
    from keyboardmanager.core.models import Macro

    ev = [kd(0, "ctrl"), kd(0.01, "c"), ku(0.02, "c"), ku(0.03, "ctrl"),
          (0.5, "click", (4, 4, "left", True)), (0.55, "click", (4, 4, "left", False))]
    out = build_actions(ev, RecordOptions(min_delay_ms=1))
    assert catalog.validate_actions(out) == []
    assert MacroExecutor(backend).run(Macro("r", out)).status == "success"
    assert ("click", "left", 1) in backend.calls


# --------------------------------------------------------- live Recorder class
import pytest  # noqa: E402
from pynput.keyboard import Key, KeyCode  # noqa: E402
from pynput.mouse import Button  # noqa: E402

from keyboardmanager.core.recorder import Recorder  # noqa: E402


class _FakeListener:
    instances: list = []

    def __init__(self, **callbacks):
        self.callbacks, self.started, self.stopped = callbacks, False, False
        _FakeListener.instances.append(self)

    def start(self): self.started = True
    def stop(self): self.stopped = True


@pytest.fixture
def live(monkeypatch):
    """A started Recorder whose pynput listeners are fakes we can drive."""
    from pynput import keyboard, mouse

    _FakeListener.instances = []
    monkeypatch.setattr(keyboard, "Listener", _FakeListener)
    monkeypatch.setattr(mouse, "Listener", _FakeListener)
    stops, counts = [], []
    rec = Recorder(RecordOptions(record_delays=False, stop_key="f9"),
                   on_stopped=lambda: stops.append(1), on_event=counts.append)
    rec.start()
    kb, ms = _FakeListener.instances
    return rec, kb.callbacks, ms.callbacks, stops, counts


def _tap(kb, *keys_):
    for k in keys_:
        kb["on_press"](k)
    for k in reversed(keys_):
        kb["on_release"](k)


def test_recorder_start_stop_lifecycle(live):
    rec, kb, ms, stops, counts = live
    assert rec.active and all(l.started for l in _FakeListener.instances)
    rec.start()  # already running: no second set of listeners
    assert len(_FakeListener.instances) == 2
    _tap(kb, KeyCode.from_char("a"))
    assert counts == [1, 2]
    assert [a.params["text"] for a in rec.stop()] == ["a"]
    assert not rec.active and all(l.stopped for l in _FakeListener.instances)
    kb["on_press"](KeyCode.from_char("b"))  # late events after stop are ignored
    assert rec.stop()[0].params["text"] == "a"


def test_recorder_keeps_capitals_and_symbols(live):
    rec, kb, *_ = live
    kb["on_press"](Key.shift)
    _tap(kb, KeyCode.from_char("H"))
    kb["on_release"](Key.shift)
    _tap(kb, KeyCode.from_char("i"), )
    kb["on_press"](Key.shift)
    _tap(kb, KeyCode.from_char("!"))
    kb["on_release"](Key.shift)
    assert [a.params["text"] for a in rec.stop()] == ["Hi!"]


def test_recorder_stop_key_is_not_recorded(live):
    rec, kb, _, stops, _ = live
    _tap(kb, KeyCode.from_char("x"))
    kb["on_press"](Key.f9)
    kb["on_press"](Key.f9)  # auto-repeat: the callback fires once
    kb["on_release"](Key.f9)
    assert stops == [1]
    assert [a.params["text"] for a in rec.stop()] == ["x"]


def test_recorder_shortcuts_and_windows_control_characters(live):
    rec, kb, *_ = live
    # Windows: ctrl auto-repeats while held and ctrl+c arrives as "\x03"
    kb["on_press"](Key.ctrl_l)
    kb["on_press"](Key.ctrl_l)
    kb["on_press"](KeyCode.from_char("\x03"))
    kb["on_release"](KeyCode.from_char("\x03"))
    kb["on_release"](Key.ctrl_l)
    _tap(kb, KeyCode.from_char("v"))  # ctrl is fully released: plain typing again
    out = rec.stop()
    assert [(a.type, a.params.get("keys") or a.params.get("text")) for a in out] == [
        ("key_press", "ctrl+c"), ("type_text", "v")]


def test_recorder_mouse_events(live):
    rec, _, ms, *_ = live
    ms["on_click"](10.7, 20.2, Button.left, True)
    ms["on_click"](10.7, 20.2, Button.left, False)
    ms["on_scroll"](0, 0, 0, -2)
    ms["on_move"](1, 1)  # moves are off by default
    out = rec.stop()
    assert [(a.type, a.params.get("x"), a.params.get("dy")) for a in out] == [
        ("mouse_click", 10, None), ("mouse_scroll", None, -2)]


def test_recorder_unknown_keys_are_skipped(live):
    rec, kb, *_ = live
    kb["on_press"](None)
    kb["on_release"](None)
    assert rec.stop() == []


def test_build_actions_uses_typed_character_when_given():
    ev = [(0, "key_down", ("a", "A")), (0.01, "key_up", "a"), (0.02, "key_down", ("space", " ")),
          (0.03, "key_up", "space")]
    assert build_actions(ev)[0].params["text"] == "A "


def test_modifiers_still_held_at_the_end_are_released():
    ev = [(0, "key_down", "shift"), (0.1, "click", (1, 1, "left", True)), (0.2, "click", (1, 1, "left", False))]
    out = build_actions(ev, RecordOptions(record_delays=False))
    assert [(a.type, a.params.get("key")) for a in out][-1] == ("key_up", "shift")


def test_other_mouse_buttons_and_mismatched_release_are_ignored():
    ev = [(0, "click", (0, 0, "x1", True)), (0.1, "click", (0, 0, "x1", False)),
          (0.2, "click", (0, 0, "left", False))]
    assert build_actions(ev) == []
