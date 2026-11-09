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
