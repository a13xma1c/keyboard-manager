"""Action catalog: validation rules and human-readable descriptions, table-driven."""

import pytest

from keyboardmanager.core import actions as catalog
from keyboardmanager.core.models import Action


def A(t, children=(), else_children=(), **params):
    a = catalog.new_action(t)
    a.params.update(params)
    a.children, a.else_children = list(children), list(else_children)
    return a


@pytest.mark.parametrize("action,problem", [
    (A("key_press", keys=""), "empty"),
    (A("key_down", key="ctrl+a"), "unknown key"),
    (A("delay", ms=10**9), "between"),
    (A("delay", ms=True), ""),
    (A("mouse_move", relative="yes"), "true or false"),
    (A("mouse_click", state="hover"), "one of"),
    (A("type_text", text=5), "text"),
    (A("if", kind="title", value="  "), "condition value is empty"),
    (A("if", kind="variable", variable=""), "variable name is empty"),
    (A("if", kind="time", value="9-5"), "09:00-17:30"),
    (A("set_variable", name=" "), "variable name is empty"),
    (A("delay", children=[A("stop")]), "cannot contain"),
    (A("repeat", else_children=[A("stop")]), "no else branch"),
    (A("if", kind="app", value="x", else_children=[A("key_press", keys="bad+")]), "else #1"),
])
def test_invalid_actions(action, problem):
    errors = catalog.validate_action(action)
    assert errors and problem in " ".join(errors)


@pytest.mark.parametrize("action", [
    A("key_press", keys="ctrl+shift+f5"), A("key_down", key="Shift"), A("type_text", text="x"),
    A("if", kind="time", value="22:00-06:00"), A("if", kind="variable", variable="n", op="contains"),
    A("repeat", count=0, children=[A("delay", ms=0)]), A("set_variable", name="n", mode="add", value="1"),
])
def test_valid_actions(action):
    assert catalog.validate_action(action) == []


def test_params_missing_from_a_file_fall_back_to_defaults():
    action = Action("delay", {})
    assert catalog.get_param(action, "ms") == catalog.new_action("delay").params["ms"]
    with pytest.raises(KeyError):
        catalog.get_param(action, "nonexistent")


@pytest.mark.parametrize("text,minutes", [("09:00-17:30", (540, 1050)), (" 7:05 - 8:00 ", (425, 480))])
def test_parse_time_range(text, minutes):
    assert catalog.parse_time_range(text) == minutes


@pytest.mark.parametrize("action,text", [
    (A("key_press", keys="ctrl+c"), "Press ctrl+c"),
    (A("mouse_move", x=1, y=2, relative=True), "Move mouse by (1, 2)"),
    (A("mouse_click", button="left", count=2, move_to=True, x=3, y=4), "Left click x2 at (3, 4)"),
    (A("mouse_click", button="right", state="press"), "Right button press"),
    (A("mouse_scroll", dx=0, dy=-3), "Scroll (dx=0, dy=-3)"),
    (A("delay", ms=100, jitter_ms=50), "Wait 100 ms + up to 50 ms"),
    (A("repeat", count=0), "Repeat until stopped"),
    (A("repeat", count=1), "Repeat 1 time"),
    (A("if", kind="title", value="Inbox", negate=True), 'If NOT (window title contains "Inbox")'),
    (A("if", kind="time", value="09:00-17:00"), "If time is between 09:00-17:00"),
    (A("if", kind="variable", variable="n", op=">", compare_to="3"), 'If n > "3"'),
    (A("set_variable", name="n", mode="add", value="2"), 'Set n += "2"'),
    (A("stop"), "Stop macro"),
])
def test_descriptions(action, text):
    assert catalog.describe(action).startswith(text)


def test_count_actions_includes_nested_steps():
    tree = [A("repeat", children=[A("if", kind="app", value="x", children=[A("stop")],
                                    else_children=[A("stop"), A("stop")])])]
    assert catalog.count_actions(tree) == 5
