"""The expression and script language (core.script)."""

import pytest

from keyboardmanager.core import script
from keyboardmanager.core.script import (
    ScriptError,
    check_expression,
    check_script,
    evaluate,
    names,
    run_script,
    to_text,
    to_value,
    truthy,
)


def lookup_from(values):
    def lookup(name):
        return values[name]  # KeyError when unknown, as the executor's lookup
    return lookup


def ev(expression, **values):
    return evaluate(expression, lookup_from(values))


def run(source, commands=None, **values):
    """Run a script; returns (variables after it, the commands called, ticks)."""
    store = dict(values)
    calls, ticks = [], [0]

    def record(name):
        return lambda *a, **k: calls.append((name, *a, *sorted(k.items())))

    table = {name: record(name) for name in ("type", "press", "wait", "log")}
    table.update(commands or {})

    def tick():
        ticks[0] += 1

    run_script(source, lookup_from(store), lambda n, v: store.__setitem__(n, to_text(v)), table, tick)
    return store, calls, ticks[0]


# ------------------------------------------------------------- conversions
@pytest.mark.parametrize("text, value", [
    ("42", 42), ("-1.5", -1.5), ("0", 0), ("true", True), ("false", False),
    ("01234", "01234"),  # a code with a leading zero stays text
    ("1e3", "1e3"), ("12 apples", "12 apples"), ("", ""), (" 5", " 5"),
])
def test_stored_text_becomes_a_value(text, value):
    assert to_value(text) == value and type(to_value(text)) is type(value)


@pytest.mark.parametrize("value, text", [
    (None, ""), (True, "true"), (False, "false"), (3, "3"), (2.5, "2.5"), (3.0, "3"),
    ([1, "a"], '[1, "a"]'), ("x", "x"),
])
def test_values_are_stored_as_text(value, text):
    assert to_text(value) == text


@pytest.mark.parametrize("value, expected", [
    ("", False), ("false", False), ("0", False), ("no", False), (" ", False),
    ("x", True), (0, False), (1, True), ([], False), ([0], True),
])
def test_truthy(value, expected):
    assert truthy(value) is expected


# ------------------------------------------------------------- expressions
@pytest.mark.parametrize("expression, expected", [
    ("1 + 2 * 3", 7),
    ("10 / 4", 2.5),
    ("7 // 2", 3),
    ("7 % 3", 1),
    ("2 ** 10", 1024),
    ("-(3)", -3),
    ('"a" + "b"', "ab"),
    ('"n = " + 3', "n = 3"),
    ('"ab" * 3', "ababab"),
    ("[1, 2] + [3]", [1, 2, 3]),
    ("3 > 2 and 2 > 1", True),
    ("1 < 2 < 3", True),
    ("3 < 2 < 5", False),
    ("not 0", True),
    ('0 or "fallback"', "fallback"),
    ('"yes" if 5 > 3 else "no"', "yes"),
    ('"ell" in "Hello"', True),
    ('"x" not in "Hello"', True),
    ("2 in [1, 2]", True),
    ('"Hello"[1]', "e"),
    ('"Hello"[-1]', "o"),
    ('"Hello"[1:3]', "el"),
    ('f"{1 + 1} items"', "2 items"),
    ("true and not false", True),
])
def test_expressions(expression, expected):
    assert ev(expression) == expected


def test_variables_are_read_as_values():
    assert ev("count + 1", count="41") == 42  # stored as text, used as a number
    assert ev('code + "!"', code="007") == "007!"
    assert ev('flag and "on"', flag="true") == "on"
    assert ev("price * 2", price="1.25") == 2.5


def test_comparisons_of_text_and_numbers():
    assert ev("count > 9", count="10")  # as numbers, not "10" < "9" as text
    assert ev('n == "5"', n="5")  # 5 (from the variable) equals the text "5"
    assert ev('"b" > "a"')
    assert ev('time >= "09:00"', time="14:30")  # clock times compare as text
    with pytest.raises(ScriptError, match="cannot compare"):
        ev("[1] < 2")


@pytest.mark.parametrize("expression, expected", [
    ('len("abc")', 3), ("len([1, 2])", 2), ('upper("a")', "A"), ('lower("A")', "a"),
    ('trim("  x ")', "x"), ('replace("a-b", "-", "+")', "a+b"), ('contains("Hello", "ELL")', True),
    ('startswith("Hello", "He")', True), ('endswith("Hello", "lo")', True),
    ('substr("Hello", 1, 3)', "ell"), ('substr("Hello", 2)', "llo"),
    ('split("a,b", ",")', ["a", "b"]), ('split("a b")', ["a", "b"]), ('join(["a", "b"], "-")', "a-b"),
    ('lines("a\\nb")', ["a", "b"]), ('matches("abc123", "[0-9]+")', True), ('number("3.5")', 3.5),
    ("text(5)", "5"), ("round(2.567, 2)", 2.57), ("round(2.6)", 3), ("abs(-3)", 3),
    ("min(3, 1, 2)", 1), ("max([3, 9])", 9), ("floor(2.7)", 2), ("ceil(2.1)", 3),
    ("range(3)", [0, 1, 2]), ('empty("  ")', True), ('empty("x")', False),
])
def test_functions(expression, expected):
    assert ev(expression) == expected


def test_random_stays_in_range():
    assert all(1 <= ev("random(1, 3)") <= 3 for _ in range(50))


@pytest.mark.parametrize("expression, message", [
    ("unknown + 1", "unknown variable unknown"),
    ("1 / 0", "division by zero"),
    ('"a" - 1', "not a number"),
    ('number("x")', "not a number"),
    ('"abc"[10]', "index out of range"),
    ('"abc"[::0]', "step cannot be 0"),
    ("nothing()", "unknown function nothing"),
    ("upper()", "upper()"),
    ('matches("a", "(")', "matches()"),
])
def test_expression_errors(expression, message):
    with pytest.raises(ScriptError, match=message.replace("(", r"\(").replace(")", r"\)")):
        ev(expression)


@pytest.mark.parametrize("expression", [
    "__import__('os')",
    "open('x')",
    "().__class__",
    "x.upper()",
    "lambda: 1",
    "[x for x in range(3)]",
    "{'a': 1}",
    "_secret",
    "eval('1')",
    "exec('1')",
    "getattr(1, 'x')",
    "(x := 1)",
    "print(*[1])",
])
def test_unsafe_or_unsupported_constructs_are_refused(expression):
    with pytest.raises(ScriptError):
        check_expression(expression)
    with pytest.raises(ScriptError):
        ev(expression, x="1")


@pytest.mark.parametrize("expression", [
    '"a" * 10000000', "9 ** 9 ** 9", "(10 ** 100) ** 100", "1e308 * 10", "range(10000000)",
    '"ab" * 600000',
])
def test_resource_limits(expression):
    with pytest.raises(ScriptError):
        ev(expression)


def test_syntax_errors_are_reported():
    with pytest.raises(ScriptError, match="syntax error"):
        check_expression("1 +")
    with pytest.raises(ScriptError, match=r"syntax error \(line 2\)"):
        check_script("x = 1\nif x\n    pass")


def test_statements_are_not_expressions():
    with pytest.raises(ScriptError):
        check_expression("x = 1")


# ----------------------------------------------------------------- scripts
def test_assignments_reach_the_macro_as_text():
    store, _, _ = run("a = 1 + 1\nb = upper(name)\nc = [1, 2]\nd = 2 > 1", name="bob")
    assert store["a"] == "2" and store["b"] == "BOB" and store["c"] == "[1, 2]" and store["d"] == "true"


def test_values_keep_their_type_inside_the_script():
    store, calls, _ = run("items = split('a,b,c', ',')\nfor x in items:\n    type(x)\nn = len(items)")
    assert calls == [("type", "a"), ("type", "b"), ("type", "c")]
    assert store["n"] == "3"


def test_augmented_assignment_and_while():
    store, _, _ = run("total = 0\ni = 0\nwhile i < 5:\n    i += 1\n    total += i")
    assert store["total"] == "15" and store["i"] == "5"


def test_augmented_assignment_of_an_existing_variable():
    store, _, _ = run("count += 1", count="9")
    assert store["count"] == "10"


def test_if_elif_else():
    source = 'if n > 10:\n    r = "big"\nelif n > 5:\n    r = "medium"\nelse:\n    r = "small"'
    assert run(source, n="7")[0]["r"] == "medium"
    assert run(source, n="70")[0]["r"] == "big"
    assert run(source, n="1")[0]["r"] == "small"


def test_break_continue_and_pass():
    store, calls, _ = run(
        "for i in range(10):\n    if i == 1:\n        continue\n    if i == 3:\n        break\n    type(i)\npass"
    )
    assert calls == [("type", 0), ("type", 2)]


def test_break_outside_a_loop_is_an_error():
    with pytest.raises(ScriptError, match="outside a loop"):
        run("break")


def test_for_over_text_goes_through_the_characters():
    _, calls, _ = run("for ch in 'ab':\n    type(ch)")
    assert calls == [("type", "a"), ("type", "b")]


def test_commands_with_keyword_arguments():
    _, calls, _ = run('press("ctrl+c")\nwait(ms=5)\nlog("done")')
    assert calls == [("press", "ctrl+c"), ("wait", ("ms", 5)), ("log", "done")]


def test_command_results_can_be_used():
    store, _, _ = run('answer = ask("name?")', commands={"ask": lambda q: "Ann"})
    assert store["answer"] == "Ann"


def test_every_statement_and_loop_round_ticks():
    _, _, ticks = run("x = 1\nfor i in range(3):\n    x += i")
    # 2 top-level statements + 3 rounds + 3 statements inside
    assert ticks == 8
    _, _, ticks = run("i = 0\nwhile i < 2:\n    i += 1")
    assert ticks == 6  # the same for while loops


def test_tick_can_stop_an_endless_loop():
    class Stop(Exception):
        pass

    count = [0]

    def tick():
        count[0] += 1
        if count[0] > 100:
            raise Stop

    with pytest.raises(Stop):
        run_script("while true:\n    pass", lookup_from({}), lambda n, v: None, {}, tick)


def test_errors_name_the_line():
    with pytest.raises(ScriptError, match=r"unknown variable missing \(line 2\)"):
        run("x = 1\ny = missing + 1")


def test_functions_and_commands_cannot_be_assigned():
    with pytest.raises(ScriptError, match="cannot be assigned"):
        run("upper = 1")
    with pytest.raises(ScriptError, match="cannot be assigned"):
        run("type = 1")


@pytest.mark.parametrize("source", [
    "import os",
    "from os import path",
    "def f():\n    pass",
    "class A:\n    pass",
    "with x:\n    pass",
    "try:\n    pass\nexcept Exception:\n    pass",
    "a, b = 1, 2",
    "x.y = 1",
    "x[0] = 1",
    "for a, b in []:\n    pass",
    "while false:\n    pass\nelse:\n    pass",
    "del x",
    "global x",
    "return 1",
    "raise ValueError",
    "lambda: 1",
    "unknown_command()",
])
def test_unsupported_statements(source):
    with pytest.raises(ScriptError):
        check_script(source)


def test_known_commands_pass_the_check():
    check_script('type("a")\npress("enter")\nhold("shift")\nrelease("shift")\nwait(1)\nclick(1, 2)\n'
                 'move(1, 2)\nscroll(-1)\nrun("x")\nnotify("m")\nask("q")\nlog("l")\npaste("p")\nstop()')


def test_names_read_and_assigned():
    read, assigned = names('x = upper(name)\nfor i in range(3):\n    type(i + y)\nz += 1', "exec")
    assert read == {"name", "i", "y", "z"} and assigned == {"x", "i", "z"}
    assert names("len(clipboard) > 3 and true") == ({"clipboard"}, set())
    assert names("1 +") == (set(), set())  # does not parse: nothing to report


def test_help_texts_mention_every_function_and_command():
    from keyboardmanager.core.executor import SCRIPT_COMMANDS

    for name in script.FUNCTIONS:
        assert name in script.FUNCTION_HELP
    for name in SCRIPT_COMMANDS:
        assert f"{name}(" in script.COMMAND_HELP
