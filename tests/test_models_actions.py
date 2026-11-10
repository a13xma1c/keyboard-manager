from keyboardmanager.core import actions as catalog
from keyboardmanager.core.models import Action, ActiveWindow, Macro


def test_macro_roundtrip_with_nesting():
    cond = catalog.new_action("if")
    cond.children = [catalog.new_action("stop")]
    cond.else_children = [catalog.new_action("delay")]
    rep = catalog.new_action("repeat")
    rep.children = [cond]
    macro = Macro(name="m", actions=[rep], hotkey="ctrl+k")
    assert Macro.from_dict(macro.to_dict()).to_dict() == macro.to_dict()


def test_from_dict_rejects_garbage():
    for bad in (None, [], {"name": 1}, {"name": "x", "actions": [{"params": {}}]}, {"name": "x", "actions": {}}):
        try:
            Macro.from_dict(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_app_matching():
    w = ActiveWindow("Google Chrome", "Inbox - Mail")
    assert Macro("a").matches(w) and Macro("a").matches(None)
    assert Macro("a", app_filter="chrome, firefox").matches(w)
    assert not Macro("a", app_filter="firefox").matches(w)
    assert not Macro("a", app_filter="chrome", window_filter="calendar").matches(w)
    assert Macro("a", window_filter="MAIL").matches(w)
    assert not Macro("a", app_filter="chrome").matches(None)


def test_validation_reports_nested_problems():
    bad_key = Action("key_press", {"keys": "ctrl+bogus"})
    rep = Action("repeat", {"count": 2}, children=[bad_key])
    errors = catalog.validate_actions([rep])
    assert len(errors) == 1 and "bogus" in errors[0]


def test_validation_rules():
    assert catalog.validate_action(catalog.new_action("key_press")) == []
    assert catalog.validate_action(Action("delay", {"ms": -5}))
    assert catalog.validate_action(Action("delay", {"ms": "fast"}))
    assert catalog.validate_action(Action("nope"))
    assert catalog.validate_action(Action("type_text", {"text": ""}))
    assert catalog.validate_action(Action("if", {"kind": "time", "value": "25:00-26:00"}))
    assert catalog.validate_action(Action("if", {"kind": "app", "value": "x"})) == []
    assert catalog.validate_action(Action("mouse_click", {"button": "side"}))
    assert catalog.validate_action(Action("stop", children=[Action("stop")]))


def test_every_spec_has_valid_defaults_and_description():
    for t in catalog.SPECS:
        a = catalog.new_action(t)
        needs_input = ("type_text", "if", "while", "paste_text", "run_macro", "wait_for_window", "launch",
                       "wait_for_image", "click_image", "focus_window")
        if t not in needs_input:  # these need a value from the user
            assert catalog.validate_action(a, in_loop=True) == [], t  # parameters only
        assert catalog.describe(a)
