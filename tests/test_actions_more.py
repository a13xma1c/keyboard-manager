"""The additional actions and richer variables, run by the executor with fakes."""

import time
from datetime import datetime

import pytest

from keyboardmanager.core import actions as catalog
from keyboardmanager.core.executor import MAX_MACRO_DEPTH, MacroExecutor, paste_shortcut
from keyboardmanager.core.interaction import HeadlessInterface
from keyboardmanager.core.models import ActiveWindow, Macro
from keyboardmanager.core.variables import SavedVariables


def A(t, children=(), else_children=(), **params):
    a = catalog.new_action(t)
    a.params.update(params)
    a.children, a.else_children = list(children), list(else_children)
    return a


class AskingUI(HeadlessInterface):
    def __init__(self, answers=()):
        super().__init__()
        self.answers, self.asked = list(answers), []

    def ask(self, prompt, default):
        self.asked.append((prompt, default))
        return self.answers.pop(0) if self.answers else None


@pytest.fixture
def saved(db):
    return SavedVariables(load=lambda: db.get_setting("v", ""), save=lambda t: db.set_setting("v", t))


def run(backend, *actions, ui=None, saved=None, macros=None, variables=None, name="t"):
    ex = MacroExecutor(backend, now=lambda: datetime(2030, 1, 2, 9, 5), ui=ui, saved=saved,
                       find_macro=lambda n: (macros or {}).get(n))
    return ex.run(Macro(name, list(actions)), variables), ex


def typed(backend):
    return [c[1] for c in backend.calls if c[0] == "type"]


# ------------------------------------------------------------- placeholders
def test_placeholders_in_typed_text(backend):
    r, _ = run(backend, A("set_variable", name="who", value="Ada"),
               A("type_text", text="Hi {who}, today is {date:%d.%m.}. {{literal}} {missing}"),
               name="greeter")
    assert r.status == "success"
    assert typed(backend) == ["Hi Ada, today is 02.01.. {literal} {missing}"]


def test_placeholders_in_set_variable_and_conditions(backend):
    run(backend, A("set_variable", name="a", value="4"),
        A("set_variable", name="b", value="{a}{a}"),
        A("if", kind="variable", variable="b", op="==", compare_to="{a}4",
          children=[A("type_text", text="yes")], else_children=[A("type_text", text="no")]))
    assert typed(backend) == ["yes"]


# ---------------------------------------------------------------- set modes
@pytest.mark.parametrize("mode,start,value,result", [
    ("set", "x", "y", "y"), ("add", "5", "2.5", "7.5"), ("subtract", "5", "7", "-2"),
    ("append", "ab", "cd", "abcd"), ("calculate", "", "(2 + 3) * 4", "20"),
    ("add", "", "3", "3"),  # a missing variable counts as 0
])
def test_set_variable_modes(backend, mode, start, value, result):
    r, _ = run(backend, A("set_variable", name="v", value=value, mode=mode), variables={"v": start} if start else None)
    assert r.status == "success" and r.variables["v"] == result


def test_calculate_with_variables_and_errors(backend):
    r, _ = run(backend, A("set_variable", name="n", value="6"),
               A("set_variable", name="n", value="{n} * 7", mode="calculate"))
    assert r.variables["n"] == "42"
    r, _ = run(backend, A("set_variable", name="n", value="os.system(1)", mode="calculate"))
    assert r.status == "error" and "cannot calculate" in r.message


def test_saved_variables_persist_across_runs_and_macros(backend, saved):
    counter = A("set_variable", name="runs", value="1", mode="add", scope="saved")
    for _ in range(3):
        run(backend, counter, saved=saved)
    assert saved.get("runs") == "3"
    run(backend, A("type_text", text="run #{runs}"), saved=saved, name="another macro")
    assert typed(backend)[-1] == "run #3"
    # a run variable with the same name takes precedence within that run
    run(backend, A("set_variable", name="runs", value="local"), A("type_text", text="{runs}"), saved=saved)
    assert typed(backend)[-1] == "local" and saved.get("runs") == "3"


@pytest.mark.parametrize("op,left,right,expected", [
    ("starts with", "Hello", "he", True), ("ends with", "report.PDF", ".pdf", True),
    ("matches", "order 1234", r"\d{4}", True), ("matches", "order", r"^\d+$", False),
    ("is empty", "  ", "", True), ("is empty", "x", "", False),
])
def test_new_comparisons(backend, op, left, right, expected):
    run(backend, A("if", kind="variable", variable="v", op=op, compare_to=right,
                   children=[A("type_text", text="y")], else_children=[A("type_text", text="n")]),
        variables={"v": left})
    assert typed(backend) == ["y" if expected else "n"]


def test_conditions_see_saved_and_builtin_variables(backend, saved):
    saved.set("mode", "fast")
    run(backend, A("if", kind="variable", variable="mode", op="==", compare_to="fast",
                   children=[A("type_text", text="saved seen")]), saved=saved)
    run(backend, A("if", kind="variable", variable="app", op="contains", compare_to="term",
                   children=[A("type_text", text="builtin seen")]))
    assert typed(backend) == ["saved seen", "builtin seen"]


# -------------------------------------------------------------------- loops
def test_while_loop_with_condition_and_limit(backend):
    r, _ = run(backend, A("set_variable", name="n", value="0"),
               A("while", kind="variable", variable="n", op="<", compare_to="3",
                 children=[A("set_variable", name="n", value="1", mode="add"), A("type_text", text="{n}")]))
    assert typed(backend) == ["1", "2", "3"] and r.variables["iteration"] == "3"
    backend.calls.clear()
    run(backend, A("while", kind="variable", variable="x", op="is empty", max_iterations=4,
                   children=[A("type_text", text=".")]))
    assert typed(backend) == ["."] * 4  # never false: stops at the limit


def test_break_leaves_the_innermost_loop(backend):
    run(backend, A("repeat", count=3, children=[
        A("repeat", count=5, children=[
            A("type_text", text="{iteration}"),
            A("if", kind="variable", variable="iteration", op="==", compare_to="2", children=[A("break")]),
        ]),
        A("type_text", text="|"),
    ]))
    assert "".join(typed(backend)) == "12|12|12|"


def test_break_outside_a_loop_is_refused(backend):
    r, _ = run(backend, A("break"))
    assert r.status == "error" and "inside a Repeat or While" in r.message
    assert catalog.validate_actions([A("if", kind="app", value="x", children=[A("break")])])


# ------------------------------------------------------------- run a macro
def test_run_macro_shares_variables(backend):
    sub = Macro("greet", [A("type_text", text="hi {who}"), A("set_variable", name="done", value="yes")])
    r, _ = run(backend, A("set_variable", name="who", value="Ada"), A("run_macro", name="greet"),
               macros={"greet": sub})
    assert typed(backend) == ["hi Ada"] and r.variables["done"] == "yes"


def test_run_macro_errors(backend):
    r, _ = run(backend, A("run_macro", name="missing"))
    assert r.status == "error" and 'no macro named "missing"' in r.message
    loop = Macro("loop", [A("run_macro", name="loop")])  # runs itself forever
    r, _ = run(backend, A("run_macro", name="loop"), macros={"loop": loop})
    assert r.status == "error" and f"{MAX_MACRO_DEPTH} levels" in r.message
    broken = Macro("broken", [A("key_press", keys="ctrl+nope")])
    r, _ = run(backend, A("run_macro", name="broken"), macros={"broken": broken})
    assert r.status == "error" and 'macro "broken"' in r.message


# ------------------------------------------------------------ wait for window
def test_wait_for_window(backend, monkeypatch):
    from keyboardmanager.core import executor as executor_mod

    monkeypatch.setattr(executor_mod, "WINDOW_POLL_S", 0.01)
    windows = iter([ActiveWindow("Terminal", ""), ActiveWindow("Terminal", ""), ActiveWindow("Safari", "")])
    backend.active_window = lambda: next(windows, ActiveWindow("Safari", ""))
    r, _ = run(backend, A("wait_for_window", kind="app", value="safari", timeout_s=5), A("type_text", text="ok"))
    assert typed(backend) == ["ok"] and r.variables["timed_out"] == "0"

    backend.active_window = lambda: ActiveWindow("Terminal", "bash")
    t = time.monotonic()
    r, _ = run(backend, A("wait_for_window", kind="title", value="never", timeout_s=1))
    assert r.status == "error" and "within 1 s" in r.message and time.monotonic() - t < 3
    r, _ = run(backend, A("wait_for_window", kind="app", value="never", timeout_s=1, on_timeout="continue"),
               A("type_text", text="{timed_out}"))
    assert r.status == "success" and typed(backend)[-1] == "1"


# ------------------------------------------------------ user and system I/O
def test_ask_stores_the_answer_and_cancel_stops(backend):
    ui = AskingUI(["Ada"])
    r, _ = run(backend, A("ask", prompt="Name for {date}?", variable="who", default="{macro}"),
               A("type_text", text="Dear {who}"), ui=ui, name="letter")
    assert ui.asked == [("Name for 2030-01-02?", "letter")] and typed(backend) == ["Dear Ada"]
    r, _ = run(backend, A("ask", prompt="?"), A("type_text", text="never"), ui=AskingUI([]))
    assert r.status == "stopped" and typed(backend) == ["Dear Ada"]


def test_ask_without_a_user_interface_is_an_error(backend):
    r, _ = run(backend, A("ask", prompt="?"))
    assert r.status == "error" and "user interface" in r.message


def test_clipboard_actions_and_placeholder(backend):
    ui = HeadlessInterface()
    ui.clipboard = "old"
    r, _ = run(backend, A("set_clipboard", text="copied {date}"), A("type_text", text="[{clipboard}]"), ui=ui)
    assert ui.clipboard == "copied 2030-01-02" and typed(backend) == ["[copied 2030-01-02]"]


def test_paste_text_uses_and_restores_the_clipboard(backend):
    ui = HeadlessInterface()
    ui.clipboard = "keep me"
    seen = []
    real_down = backend.key_down
    backend.key_down = lambda key: (seen.append((key, ui.clipboard)), real_down(key))
    run(backend, A("paste_text", text="Ünïcode ✓ {date}"), ui=ui)
    combo = paste_shortcut().split("+")
    assert [k for k, _ in seen] == combo and seen[-1][1] == "Ünïcode ✓ 2030-01-02"
    assert ui.clipboard == "keep me"  # restored afterwards
    assert paste_shortcut("darwin") == "cmd+v" and paste_shortcut("win32") == "ctrl+v"


def test_launch_notify_and_comment(backend):
    ui = HeadlessInterface()
    r, _ = run(backend, A("comment", text="does nothing"), A("launch", target="https://example.com/{date}"),
               A("notify", title="Done", message="at {time}"), ui=ui)
    assert r.status == "success"
    assert ("launch", "https://example.com/2030-01-02") in backend.calls
    assert ui.notifications == [("Done", "at 09:05")]


@pytest.mark.parametrize("action,problem", [
    (A("set_variable", name="has space"), "valid variable name"),
    (A("ask", variable=""), "variable name is empty"),
    (A("if", kind="variable", variable="v", op="matches", compare_to="(unclosed"), "regular expression"),
    (A("run_macro", name=" "), "choose the macro"),
    (A("launch", target=""), "what to open"),
    (A("paste_text", text=""), "text is empty"),
])
def test_validation_of_new_actions(action, problem):
    assert problem in " ".join(catalog.validate_action(action, in_loop=True))


def test_descriptions_of_new_actions():
    assert catalog.describe(A("set_variable", name="n", value="1", mode="add", scope="saved")) == 'Set n (saved) += "1"'
    assert catalog.describe(A("while", kind="variable", variable="n", op="is empty")) == "While n is empty"
    assert catalog.describe(A("comment", text="note")) == "# note"
    assert catalog.describe(A("wait_for_window", kind="title", value="Inbox", timeout_s=5)).startswith(
        'Wait until the window title contains "Inbox"')
    for t in catalog.SPECS:
        assert catalog.describe(catalog.new_action(t)) != t or t in ("break",)
