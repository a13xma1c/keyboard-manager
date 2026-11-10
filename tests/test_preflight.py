"""Pre-flight check: every kind of issue, run-time estimates and the run guard flag."""

import pytest

from keyboardmanager.core import actions as catalog
from keyboardmanager.core.models import Macro
from keyboardmanager.core.preflight import (
    ERROR,
    INFO,
    WARNING,
    CheckContext,
    check_all,
    check_macro,
    estimate_duration,
)
from keyboardmanager.core.schedule import Schedule


def A(t, children=(), else_children=(), **params):
    a = catalog.new_action(t)
    a.params.update(params)
    a.children, a.else_children = list(children), list(else_children)
    return a


def issues(macro, *others, **context):
    ctx = CheckContext(macros=[macro, *others], **context)
    return [(i.severity, i.step, i.message) for i in check_macro(macro, ctx)]


def messages(macro, *others, severity=None, **context):
    return [m for s, _, m in issues(macro, *others, **context) if severity in (None, s)]


def test_a_good_macro_has_only_its_run_time():
    m = Macro("ok", [A("type_text", text="hi"), A("delay", ms=2000)], id=1)
    assert issues(m) == [(INFO, (), "takes about 2.0 s")]


def test_invalid_steps_are_errors_that_block_running():
    m = Macro("bad", [A("key_press", keys="ctrl+nope"), A("repeat", children=[A("break"), A("set_variable", name="has space")])], id=1)
    found = check_macro(m, CheckContext(macros=[m]))
    errors = [(i.step, i.blocks_run) for i in found if i.severity == ERROR]
    assert errors == [((0,), True), ((1, "children", 1), True)]  # Break inside the loop is fine
    assert all(i.step_text for i in found if i.step)


def test_break_outside_a_loop_is_reported_at_its_step():
    m = Macro("m", [A("if", kind="app", value="x", children=[A("break")])], id=1)
    assert (ERROR, (0, "children", 0), "Break only works inside a Repeat or While loop") in issues(m)


def test_missing_and_recursive_macro_references():
    a = Macro("a", [A("run_macro", name="b")], id=1)
    b = Macro("b", [A("run_macro", name="A")], id=2)  # names match ignoring case
    assert any("runs \"a\" again" in m for m in messages(a, b, severity=WARNING))
    ghost = Macro("g", [A("run_macro", name="nobody"), A("run_macro", name="{who}")], id=3)
    found = messages(ghost, severity=ERROR)
    assert found == ['there is no macro named "nobody"']  # a name from a variable is not judged


def test_variables_that_are_never_set():
    m = Macro("m", [A("type_text", text="{greeting} {name} {date} {{literal}}"),
                    A("set_variable", name="name", value="Ada"),
                    A("if", kind="variable", variable="mood", op="==", compare_to="{mode}",
                      children=[A("type_text", text="{iteration}")])], id=1)
    found = messages(m, severity=WARNING, saved_variables={"mode"})
    assert found == ["{greeting} is never set, so it is typed as it is",
                     'variable "mood" is never set: it is always empty here',
                     "{iteration} is never set, so it is typed as it is"]


def test_variables_set_by_loops_asks_waits_and_other_macros_count():
    helper = Macro("helper", [A("set_variable", name="from_helper", value="1")], id=2)
    m = Macro("m", [A("repeat", count=2, children=[A("type_text", text="{iteration}")]),
                    A("ask", variable="answer"), A("wait_for_window", value="x", on_timeout="continue"),
                    A("run_macro", name="helper"),
                    A("type_text", text="{answer} {timed_out} {from_helper}")], id=1)
    assert messages(m, helper, severity=WARNING) == []


def test_keys_this_system_does_not_have():
    m = Macro("m", [A("key_press", keys="ctrl+insert"), A("key_down", key="insert")], id=1)
    found = messages(m, severity=WARNING, key_supported=lambda k: k != "insert")
    assert found == ['the key "insert" does not exist on this system, so it cannot be sent'] * 2


def test_runaway_repeat():
    fast = Macro("m", [A("repeat", count=0, children=[A("key_press", keys="a")])], id=1)
    assert "repeats as fast as possible" in messages(fast, severity=WARNING)[0]
    for calm in (A("delay"), A("wait_for_window", value="x"), A("if", kind="app", value="x", children=[A("break")])):
        ok = Macro("m", [A("repeat", count=0, children=[A("key_press", keys="a"), calm])], id=1)
        assert messages(ok, severity=WARNING) == [], calm.type


def test_triggers_events_and_schedules():
    clash = Macro("first", [A("stop")], hotkey="ctrl+k", id=1)
    m = Macro("m", [A("ask")], hotkey="ctrl+k ctrl+s", abbreviation=";x", id=2,
              schedule=Schedule("once", at="2001-01-01 09:00"))
    found = issues(m, clash, Macro("other", abbreviation=";xy", id=3))
    texts = [t for _, _, t in found]
    assert any(t.startswith("hotkey:") and "overlaps" in t for t in texts)
    assert any(t.startswith("abbreviation:") for t in texts)
    assert "its scheduled time has passed: the schedule will not run again" in texts
    assert any("waits for an answer" in t for t in texts)
    bad_event = Macro("e", [A("stop")], event="focus_gained", id=4)
    assert any("filter" in t for t in messages(bad_event, severity=ERROR))
    # trigger problems are errors, but they do not stop the macro being run by hand
    assert not any(i.blocks_run for i in check_macro(m, CheckContext(macros=[m, clash])))


def test_disabled_and_empty_macros():
    assert "has no steps: running it does nothing" in messages(Macro("empty", id=1))
    off = Macro("off", [A("stop")], hotkey="ctrl+1", enabled=False, id=1)
    assert "disabled: its hotkey will not start it" in messages(off, severity=INFO)


@pytest.mark.parametrize("steps,seconds,exact", [
    ([A("delay", ms=1500)], 1.5, True),
    ([A("delay", ms=1000, jitter_ms=1000)], 1.5, False),          # jitter: on average half
    ([A("type_text", text="abcd", interval_ms=250)], 1.0, True),
    ([A("repeat", count=3, children=[A("delay", ms=500)])], 1.5, True),
    ([A("repeat", count=0, children=[A("delay")])], None, False),
    ([A("if", kind="app", value="x", children=[A("delay", ms=2000)], else_children=[A("delay", ms=1000)])], 1.0, False),
    ([A("delay", ms=1000), A("stop"), A("delay", ms=9000)], 1.0, True),
    ([A("wait_for_window", value="x"), A("delay", ms=1000)], 1.0, False),
])
def test_estimate_duration(steps, seconds, exact):
    assert estimate_duration(steps) == (seconds, exact)


def test_run_time_respects_both_speeds():
    m = Macro("m", [A("delay", ms=8000)], id=1, speed=2.0)
    assert messages(m, severity=INFO, playback_speed=0.5) == ["takes about 8.0 s"]  # 2x * 0.5x = 1x
    assert messages(m, severity=INFO, playback_speed=2.0) == ["takes about 2.0 s at 4x speed"]
    assert messages(Macro("long", [A("delay", ms=3_600_000)], id=2), severity=INFO) == ["takes about 1 h 0 min"]
    assert messages(Macro("forever", [A("repeat", count=0, children=[A("delay")])], id=3), severity=INFO) == [
        "runs until it is stopped"]


def test_check_all_sorts_errors_first():
    good = Macro("good", [A("delay", ms=5000)], id=1)
    bad = Macro("bad", [A("run_macro", name="x")], id=2)
    warn = Macro("warn", [A("type_text", text="{x}")], id=3)
    found = check_all([good, bad, warn], CheckContext(macros=[good, bad, warn]))
    assert [i.severity for i in found] == [ERROR, WARNING, INFO]
