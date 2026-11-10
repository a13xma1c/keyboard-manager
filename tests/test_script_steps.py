"""Scripts and expressions in macros, the debugger and the live view (executor level)."""

import threading
import time

import pytest

from keyboardmanager.core import actions as catalog
from keyboardmanager.core.debugger import Debugger, SimulatedBackend
from keyboardmanager.core.executor import MacroExecutor
from keyboardmanager.core.interaction import HeadlessInterface
from keyboardmanager.core.limits import Limits
from keyboardmanager.core.models import ActiveWindow, Macro
from keyboardmanager.core.variables import SavedVariables
from tests_support import FakeBackend, wait_until


def A(t, children=None, else_children=None, **params):
    a = catalog.new_action(t)
    a.params.update(params)
    a.children = children or []
    a.else_children = else_children or []
    return a


def script(code):
    return A("script", code=code)


class UI(HeadlessInterface):
    def __init__(self, answers=()):
        super().__init__()
        self.answers = list(answers)

    def ask(self, prompt, default):
        return self.answers.pop(0) if self.answers else None


def run(backend, *actions, ui=None, find_macro=None, saved=None, limits=None, variables=None):
    ex = MacroExecutor(backend, ui=ui or UI(), find_macro=find_macro, saved=saved)
    return ex.run(Macro("t", list(actions), limits=limits or Limits()), variables)


def typed(backend):
    return "".join(c[1] for c in backend.calls if c[0] == "type")


# ----------------------------------------------------------------- scripts
def test_script_types_and_shares_variables_with_later_steps(backend):
    r = run(backend, A("set_variable", name="name", value="ann"),
            script('name = upper(name)\ntype("Hi " + name)\ncount = 3'),
            A("type_text", text=" {count}"))
    assert r.status == "success"
    assert typed(backend) == "Hi ANN 3"
    assert r.variables["name"] == "ANN" and r.variables["count"] == "3"


def test_script_keys_and_mouse_commands(backend):
    r = run(backend, script('press("ctrl+c")\nhold("shift")\nrelease("shift")\nclick(5, 6, button="right")\n'
                            'move(1, 1, relative=true)\nscroll(-2)\nhold("alt")'))
    assert r.status == "success"
    assert backend.calls == [
        ("down", "ctrl"), ("down", "c"), ("up", "c"), ("up", "ctrl"),
        ("down", "shift"), ("up", "shift"),
        ("move", 5, 6), ("click", "right", 1), ("move", 6, 7), ("scroll", 0, -2),
        ("down", "alt"), ("up", "alt"),  # still held at the end: released
    ]


def test_script_type_with_interval_and_wait(backend):
    began = time.monotonic()
    r = run(backend, script('type("ab", interval=30)\nwait(50)'))
    assert r.status == "success" and typed(backend) == "ab"
    assert [c for c in backend.calls] == [("type", "a"), ("type", "b")]
    assert time.monotonic() - began >= 0.1


def test_script_unknown_mouse_button(backend):
    r = run(backend, script('click(button="thumb")'))
    assert r.status == "error" and "unknown mouse button" in r.message


def test_script_paste_restores_the_clipboard(backend):
    ui = UI()
    ui.clipboard = "before"
    r = run(backend, script('paste("pasted")'), ui=ui)
    assert r.status == "success" and ui.clipboard == "before"
    assert ("down", "v") in backend.calls


def test_script_ask_notify_log_and_stop(backend):
    ui = UI(answers=["Bob"])
    r = run(backend, script('n = ask("Name?")\nnotify("Hello " + n)\nlog("greeted " + n)\nstop()\ntype("x")'),
            ui=ui)
    assert r.status == "stopped" and typed(backend) == ""
    assert ui.notifications == [("Keyboard Manager", "Hello Bob")]
    assert "greeted Bob" in r.steps[0].detail


def test_script_ask_cancelled_stops(backend):
    r = run(backend, script('n = ask("Name?")\ntype(n)'), ui=UI(answers=[]))
    assert r.status == "stopped" and typed(backend) == ""


def test_script_runs_another_macro(backend):
    other = Macro("Other", [A("type_text", text="from other")])
    r = run(backend, script('run("Other")\ntype("!")'), find_macro=lambda n: other if n == "Other" else None)
    assert r.status == "success" and typed(backend) == "from other!"
    r = run(backend, script('run("Missing")'), find_macro=lambda n: None)
    assert r.status == "error" and 'no macro named "Missing"' in r.message


def test_script_error_reports_line(backend):
    r = run(backend, script("x = 1\ny = nope + 1"))
    assert r.status == "error" and "script: unknown variable nope (line 2)" in r.message


def test_script_statements_count_towards_the_step_limit(backend):
    r = run(backend, script("while true:\n    pass"), limits=Limits(max_steps=50))
    assert r.status == "stopped" and "step limit of 50" in r.message


def test_endless_script_can_be_stopped_and_paused(backend):
    ex = MacroExecutor(backend, ui=UI())
    holder = {}
    th = threading.Thread(target=lambda: holder.update(r=ex.run(Macro("t", [script(
        'hold("shift")\ni = 0\nwhile true:\n    i += 1')]))))
    th.start()
    assert wait_until(lambda: ("down", "shift") in backend.calls)
    ex.pause()
    assert wait_until(lambda: ("up", "shift") in backend.calls)  # released while paused
    steps = ex.snapshot()["steps_run"]
    time.sleep(0.1)
    assert ex.snapshot()["steps_run"] == steps and th.is_alive()
    ex.resume()
    assert wait_until(lambda: ex.snapshot()["steps_run"] > steps + 10)
    ex.stop()
    th.join(2)
    assert holder["r"].status == "stopped"
    assert backend.calls.count(("down", "shift")) == 2 and backend.calls[-1] == ("up", "shift")


def test_script_saved_variable_and_note(backend):
    store = {}
    saved = SavedVariables(load=lambda: store.get("v", ""), save=lambda text: store.__setitem__("v", text))
    saved.set("visits", "4")
    r = run(backend, script("visits = visits + 1\ntype(visits)"), saved=saved)
    assert typed(backend) == "5"
    assert r.variables["visits"] == "5"  # a script assigns run variables (Set variable saves)
    assert "statement(s) run" in r.steps[0].detail


# ------------------------------------------------------------- expressions
def test_expression_condition(backend):
    backend.window = ActiveWindow("Mail", "Inbox")
    cond = A("if", kind="expression", expression='"Mail" in app and n > 2',
             children=[A("type_text", text="yes")], else_children=[A("type_text", text="no")])
    run(backend, cond, variables={"n": "3"})
    run(backend, cond, variables={"n": "1"})
    assert typed(backend) == "yesno"


def test_expression_condition_negated_and_in_while(backend):
    r = run(backend, A("set_variable", name="i", value="0"),
            A("while", kind="expression", expression="i < 3", max_iterations=10,
              children=[A("set_variable", name="i", value="i + 1", mode="expression")]),
            A("if", kind="expression", expression="i == 3", negate=True,
              children=[A("type_text", text="wrong")]))
    assert r.status == "success" and r.variables["i"] == "3" and typed(backend) == ""


def test_set_variable_expression_mode(backend):
    r = run(backend, A("set_variable", name="price", value="10"),
            A("set_variable", name="total", value="round(price * 1.19, 2)", mode="expression"),
            A("set_variable", name="label", value='f"{total} EUR"', mode="expression"))
    assert r.variables["total"] == "11.9" and r.variables["label"] == "11.9 EUR"


def test_inline_expressions_in_texts(backend):
    r = run(backend, A("set_variable", name="n", value="4"),
            A("type_text", text="{n} squared is {= n * n}, {{= literal}}"))
    assert r.status == "success" and typed(backend) == "4 squared is 16, {= literal}"


def test_inline_expression_error_stops_the_macro(backend):
    r = run(backend, A("type_text", text="{= missing + 1}"))
    assert r.status == "error" and "in {= missing + 1}: unknown variable missing" in r.message


def test_expression_error_in_condition(backend):
    r = run(backend, A("if", kind="expression", expression="1 / 0"))
    assert r.status == "error" and "division by zero" in r.message


def test_invalid_script_or_expression_is_refused_before_running(backend):
    r = run(backend, A("type_text", text="a"), script("import os"))
    assert r.status == "error" and backend.calls == []  # nothing ran
    problems = catalog.validate_actions([A("if", kind="expression", expression="x.y")])
    assert problems and "not supported" in problems[0]
    problems = catalog.validate_actions([A("set_variable", name="v", value="1 +", mode="expression")])
    assert problems and "syntax error" in problems[0]


def test_describe_script_and_expression_steps():
    assert catalog.describe(script("x = 1\n\ntype(x)\n")) == "Script: x = 1 (+1 lines)"
    assert catalog.describe(A("if", kind="expression", expression="n > 1")) == "If n > 1"
    assert catalog.describe(A("set_variable", name="t", value="n * 2", mode="expression")) == 'Set t = expr "n * 2"'


def test_script_round_trips_through_a_macro_file():
    macro = Macro("S", [script('type("a")')], when_busy="queue")
    data = macro.to_dict()
    assert data["when_busy"] == "queue"
    again = Macro.from_dict(data)
    assert again.actions[0].params["code"] == 'type("a")' and again.when_busy == "queue"
    assert "when_busy" not in Macro("T", []).to_dict()  # the default is not written
    with pytest.raises(ValueError, match="when_busy"):
        Macro.from_dict({**data, "when_busy": "later"})


# ---------------------------------------------------------------- debugger
def steps_of(*texts):
    return [A("type_text", text=t) for t in texts]


def debug_run(backend, actions, debugger):
    """Run with a debugger whose pause() pauses the executor (like the service)."""
    ex = MacroExecutor(backend, ui=UI(), debugger=debugger)
    debugger.pause = ex.pause
    holder = {}
    th = threading.Thread(target=lambda: holder.update(r=ex.run(Macro("t", actions))))
    th.start()
    return ex, th, holder


def test_breakpoint_stops_before_the_step(backend):
    debugger = Debugger({(1,)})
    ex, th, holder = debug_run(backend, steps_of("a", "b", "c"), debugger)
    assert wait_until(lambda: ex.paused)
    assert typed(backend) == "a" and debugger.stopped_at == (1,) and debugger.hits == 1
    assert ex.snapshot()["step"] == (1,) and ex.snapshot()["step_text"] == 'Type "b"'
    debugger.continue_()
    ex.resume()
    th.join(2)
    assert holder["r"].status == "success" and typed(backend) == "abc"
    assert debugger.stopped_at is None


def test_stepping(backend):
    debugger = Debugger(stop_at_start=True)
    ex, th, holder = debug_run(backend, steps_of("a", "b"), debugger)
    assert wait_until(lambda: ex.paused) and typed(backend) == ""
    assert debugger.stopped_at == (0,)
    debugger.step()
    ex.resume()
    assert wait_until(lambda: debugger.stopped_at == (1,) and ex.paused)
    assert typed(backend) == "a"
    debugger.step()
    ex.resume()
    th.join(2)
    assert holder["r"].status == "success" and typed(backend) == "ab"


def test_breakpoints_inside_containers_and_branches(backend):
    actions = [A("repeat", count=2, children=steps_of("r")),
               A("if", kind="expression", expression="false", else_children=steps_of("e"))]
    debugger = Debugger({(0, "children", 0), (1, "else_children", 0)})
    ex, th, _ = debug_run(backend, actions, debugger)
    for expected in ["", "r", "rr"]:  # twice in the loop, then in the else branch
        assert wait_until(lambda: ex.paused), expected
        assert typed(backend) == expected
        time.sleep(0.02)
        ex.resume()
        assert wait_until(lambda: not ex.paused or not th.is_alive())
    th.join(2)
    assert typed(backend) == "rre" and debugger.hits == 3


def test_sub_macro_steps_have_their_own_paths(backend):
    other = Macro("Other", steps_of("o"))
    debugger = Debugger()
    paths = []
    debugger.before_step = lambda path, action: paths.append(path) or False
    ex = MacroExecutor(backend, ui=UI(), debugger=debugger, find_macro=lambda n: other)
    ex.run(Macro("t", [A("run_macro", name="Other")]))
    assert paths == [(0,), ("macro:Other", 0)]


def test_stop_while_stopped_at_a_breakpoint(backend):
    debugger = Debugger({(1,)})
    ex, th, holder = debug_run(backend, [A("key_down", key="shift"), *steps_of("b")], debugger)
    assert wait_until(lambda: ex.paused)
    ex.stop()
    th.join(2)
    assert holder["r"].status == "stopped" and typed(backend) == ""
    assert backend.calls[-1] == ("up", "shift")


def test_simulated_backend_records_instead_of_sending():
    real = FakeBackend(ActiveWindow("Editor", "doc"))
    sim = SimulatedBackend(real)
    ex = MacroExecutor(sim, ui=UI())
    r = ex.run(Macro("t", [A("type_text", text="hi"), A("key_press", keys="ctrl+s"),
                           A("mouse_click", button="left", count=2, move_to=True, x=3, y=4),
                           A("mouse_move", x=1, y=1, relative=True), A("mouse_scroll", dx=0, dy=2),
                           A("launch", target="https://example.com"),
                           A("if", kind="app", value="editor", children=[A("type_text", text="in editor")])]))
    assert r.status == "success" and real.calls == []  # nothing reached the system
    texts = [t for _, t in sim.sent]
    assert texts == ["type 'hi'", "key down ctrl", "key down s", "key up s", "key up ctrl",
                     "move mouse to (3, 4)", "left click x2", "move mouse to (4, 5)", "scroll 0, 2",
                     "open 'https://example.com'", "type 'in editor'"]  # the real window was read
    assert sim.recent(2) == sim.sent[-2:]


def test_simulated_backend_keeps_a_bounded_record():
    from keyboardmanager.core import debugger as debugger_mod

    sim = SimulatedBackend(FakeBackend())
    for i in range(debugger_mod.MAX_SIMULATED + 20):
        sim.type_text(str(i))
    assert len(sim.sent) == debugger_mod.MAX_SIMULATED and sim.sent[-1][1] == f"type '{debugger_mod.MAX_SIMULATED + 19}'"


# --------------------------------------------------------------- live view
def test_snapshot_while_running(backend):
    ex = MacroExecutor(backend, ui=UI())
    holder = {}
    th = threading.Thread(target=lambda: holder.update(r=ex.run(Macro("t", [
        A("set_variable", name="x", value="1"), A("delay", ms=5000)]))))
    th.start()
    assert wait_until(lambda: ex.snapshot()["step"] == (1,) and ex.snapshot()["steps_run"] == 2)
    snap = ex.snapshot()
    assert snap["step_text"] == "Wait 5000 ms"
    assert snap["variables"] == {"x": "1"} and [s.description for s in snap["recent"]][0] == 'Set x = "1"'
    ex.pause()
    time.sleep(0.1)
    frozen = ex.snapshot()["elapsed"]
    time.sleep(0.15)
    assert ex.snapshot()["elapsed"] == pytest.approx(frozen, abs=0.01)  # time stands still while paused
    ex.stop()
    th.join(2)


def test_snapshot_before_running(backend):
    snap = MacroExecutor(backend).snapshot()
    assert snap["elapsed"] == 0.0 and snap["step"] == () and snap["steps_run"] == 0
