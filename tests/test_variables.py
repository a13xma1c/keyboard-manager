"""Placeholders, built-in values, saved variables and the safe calculator."""

import random
from datetime import datetime

import pytest

from keyboardmanager.core.variables import (
    SavedVariables,
    builtin_value,
    calculate,
    format_number,
    interpolate,
)


def lookup_from(values):
    return lambda name, fmt: values.get(name)


@pytest.mark.parametrize("text,expected", [
    ("Hi {name}!", "Hi Ada!"),
    ("{a}{b}", "12"),
    ("{{name}}", "{name}"),                       # escaped braces
    ("{unknown} stays", "{unknown} stays"),       # unknown names are left alone...
    ("function() { return {x: 1}; }", "function() { return {x: 1}; }"),  # ...so code is safe
    ("no placeholders", "no placeholders"),
    ("{name:upper}", "Ada"),                      # a format is passed on (ignored here)
])
def test_interpolate(text, expected):
    assert interpolate(text, lookup_from({"name": "Ada", "a": "1", "b": "2"})) == expected


def test_format_number_never_loses_digits():
    assert format_number(3.0) == "3" and format_number(2.5) == "2.5"
    assert format_number(1234567.0 + 1) == "1234568"  # ":g" would give 1.23457e+06
    assert format_number(0.1 + 0.2) == "0.30000000000000004"
    assert format_number(10**20 * 1.0) == "1e+20"


@pytest.mark.parametrize("expression,value", [
    ("1 + 2 * 3", 7), ("(1 + 2) * 3", 9), ("7 // 2", 3), ("7 % 3", 1), ("2 ** 10", 1024),
    ("-4 + +1", -3), ("round(2.567, 1)", 2.6), ("max(1, 5, 3)", 5), ("abs(-2)", 2),
    ("floor(2.7)", 2), ("ceil(2.1)", 3), ("10 / 4", 2.5),
])
def test_calculate(expression, value):
    assert calculate(expression) == value


@pytest.mark.parametrize("expression", [
    "__import__('os').system('x')", "open('f')", "a + 1", "1 +", "2 ** 1000", "1 / 0", "[1, 2]",
    "round(x=1)", "'text'", "True + 1",
])
def test_calculate_refuses_anything_else(expression):
    with pytest.raises(ValueError):
        calculate(expression)


def test_builtins():
    kw = dict(now=lambda: datetime(2030, 1, 2, 9, 5), rng=random.Random(3),
              clipboard=lambda: "copied", window=lambda: ("Mail", "Inbox"), macro_name="m")
    assert builtin_value("date", "", **kw) == "2030-01-02"
    assert builtin_value("date", "%d.%m.%Y", **kw) == "02.01.2030"
    assert builtin_value("time", "", **kw) == "09:05"
    assert builtin_value("datetime", "", **kw) == "2030-01-02 09:05"
    assert builtin_value("weekday", "", **kw) == "Wednesday"
    assert builtin_value("clipboard", "", **kw) == "copied"
    assert (builtin_value("app", "", **kw), builtin_value("window_title", "", **kw)) == ("Mail", "Inbox")
    assert builtin_value("macro", "", **kw) == "m"
    assert all(1 <= int(builtin_value("random", "1-6", **kw)) <= 6 for _ in range(50))
    assert builtin_value("random", "6-1", **kw) in {str(i) for i in range(1, 7)}
    with pytest.raises(ValueError, match="range"):
        builtin_value("random", "dice", **kw)
    assert builtin_value("nonsense", "", **kw) is None


def test_saved_variables(db):
    store = SavedVariables(load=lambda: db.get_setting("v", ""), save=lambda t: db.set_setting("v", t))
    assert store.all() == {} and store.get("x") is None
    store.set("count", 3)
    store.set("name", "Ada")
    assert store.all() == {"count": "3", "name": "Ada"}
    store.delete("count")
    store.delete("missing")  # fine
    assert store.all() == {"name": "Ada"}
    with pytest.raises(ValueError):
        store.set("not valid", "x")
    db.set_setting("v", "{corrupt")
    assert store.all() == {}  # never fails because of a damaged setting
