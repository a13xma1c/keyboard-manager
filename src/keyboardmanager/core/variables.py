"""Variables: placeholders in texts, built-in values, saved variables, a safe calculator.

``{name}`` in a text is replaced by the variable's value; ``{name:format}`` passes a
format (dates: strftime, e.g. ``{date:%d.%m.%Y}``; random: a range, ``{random:1-6}``).
``{{`` and ``}}`` give literal braces. Only *known* names are replaced, so texts
that contain braces for other reasons (code, JSON) are typed unchanged.

Lookup order: variables of the current run, then saved variables (kept across runs
and shared by all macros), then the built-ins below.
"""

from __future__ import annotations

import ast
import json
import math
import operator
import random
import re
import threading
from datetime import datetime
from typing import Callable

BUILTINS = {
    "date": "today's date (format: strftime, default %Y-%m-%d)",
    "time": "the current time (default %H:%M)",
    "datetime": "date and time (default %Y-%m-%d %H:%M)",
    "weekday": "the day of the week (default %A)",
    "clipboard": "the text on the clipboard",
    "app": "the focused application",
    "window_title": "the focused window's title",
    "macro": "the name of the running macro",
    "random": "a random whole number (format: range, default 1-100)",
}
_DATE_FORMATS = {"date": "%Y-%m-%d", "time": "%H:%M", "datetime": "%Y-%m-%d %H:%M", "weekday": "%A"}
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PLACEHOLDER = re.compile(r"\{\{|\}\}|\{=([^{}]+)\}|\{([A-Za-z_][A-Za-z0-9_]*)(?::([^{}]*))?\}")

Lookup = Callable[[str, str], "str | None"]  # (name, format) -> value, None = unknown


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name))


def interpolate(text: str, lookup: Lookup, expression: Callable[[str], str] | None = None) -> str:
    """Replace ``{name}`` / ``{name:format}`` placeholders known to ``lookup``, and
    ``{= expression}`` placeholders with ``expression``'s result (when given)."""
    if "{" not in text and "}" not in text:
        return text

    def replace(match: re.Match) -> str:
        token = match.group(0)
        if token == "{{":
            return "{"
        if token == "}}":
            return "}"
        if match.group(1) is not None:
            return token if expression is None else expression(match.group(1))
        value = lookup(match.group(2), match.group(3) or "")
        return token if value is None else value

    return _PLACEHOLDER.sub(replace, text)


def placeholders(text: str) -> list[str]:
    """Names used as ``{name}`` / ``{name:format}`` in ``text`` (escaped braces skipped)."""
    return [m.group(2) for m in _PLACEHOLDER.finditer(text) if m.group(2)]


def inline_expressions(text: str) -> list[str]:
    """The expressions of ``{= ...}`` placeholders in ``text``."""
    return [m.group(1) for m in _PLACEHOLDER.finditer(text) if m.group(1)]


def format_number(value: float) -> str:
    """``3.0`` -> ``"3"``, ``2.5`` -> ``"2.5"``; never loses digits like ``:g`` does."""
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, int) or (isinstance(value, float) and value.is_integer() and abs(value) < 1e15):
        return str(int(value))
    return repr(float(value))


def to_number(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"{text!r} is not a number") from None


# ------------------------------------------------------------ calculator
_BINARY = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCTIONS = {"abs": abs, "round": round, "min": min, "max": max, "floor": math.floor, "ceil": math.ceil}


def calculate(expression: str) -> float:
    """Evaluate arithmetic safely: numbers, + - * / // % **, parentheses and
    abs / round / min / max / floor / ceil. Nothing else can run."""
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError:
        raise ValueError(f"cannot calculate {expression!r}") from None

    def ev(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BINARY:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("exponent too large")
            try:
                return _BINARY[type(node.op)](left, right)
            except ZeroDivisionError:
                raise ValueError("division by zero") from None
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](ev(node.operand))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUNCTIONS
                and not node.keywords and node.args):
            return _FUNCTIONS[node.func.id](*(ev(arg) for arg in node.args))
        raise ValueError(f"cannot calculate {expression!r}")

    return ev(tree)


# ------------------------------------------------------------- saved store
class SavedVariables:
    """Variables kept across runs, shared by all macros (stored as a setting)."""

    SETTING = "saved_variables"

    def __init__(self, load: Callable[[], str], save: Callable[[str], None]) -> None:
        self._load, self._save = load, save
        self._lock = threading.Lock()

    def all(self) -> dict[str, str]:
        with self._lock:
            return self._read()

    def get(self, name: str) -> str | None:
        with self._lock:
            return self._read().get(name)

    def set(self, name: str, value: str) -> None:
        if not valid_name(name):
            raise ValueError(f"{name!r} is not a valid variable name")
        with self._lock:
            data = self._read()
            data[name] = str(value)
            self._save(json.dumps(data, ensure_ascii=False, sort_keys=True))

    def delete(self, name: str) -> None:
        with self._lock:
            data = self._read()
            data.pop(name, None)
            self._save(json.dumps(data, ensure_ascii=False, sort_keys=True))

    def _read(self) -> dict[str, str]:
        try:
            data = json.loads(self._load() or "{}")
        except ValueError:
            return {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def builtin_value(name: str, fmt: str, *, now: Callable[[], datetime], rng: random.Random,
                  clipboard: Callable[[], str], window: Callable[[], tuple[str, str]],
                  macro_name: str) -> str | None:
    if name in _DATE_FORMATS:
        return now().strftime(fmt or _DATE_FORMATS[name])
    if name == "clipboard":
        return clipboard()
    if name == "app":
        return window()[0]
    if name == "window_title":
        return window()[1]
    if name == "macro":
        return macro_name
    if name == "random":
        low, high = 1, 100
        if fmt:
            try:
                low, high = (int(p) for p in fmt.split("-", 1))
            except ValueError:
                raise ValueError(f"random needs a range like 1-6, not {fmt!r}") from None
        return str(rng.randint(min(low, high), max(low, high)))
    return None
