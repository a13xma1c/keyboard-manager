"""Action catalogue: what each action type is, which parameters it takes, how it is
validated and how it is described to the user. The GUI builds its forms from this
table, so adding an action here (plus an executor handler) is all that is needed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from keyboardmanager.core import keys
from keyboardmanager.core.models import Action

MAX_DELAY_MS = 3_600_000
CONDITION_KINDS = ("app", "title", "time", "variable")
COMPARE_OPS = ("==", "!=", ">", "<", ">=", "<=", "contains")
MOUSE_BUTTONS = ("left", "right", "middle")
MOUSE_STATES = ("click", "press", "release")


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    kind: str  # text | combo | key | int | float | bool | choice
    default: Any = ""
    choices: tuple[str, ...] = ()
    minimum: float = 0
    maximum: float = 1_000_000
    show_if: tuple[str, tuple[Any, ...]] | None = None  # (other param, accepted values)
    hint: str = ""


@dataclass(frozen=True)
class ActionSpec:
    type: str
    label: str
    category: str
    fields: tuple[Field, ...] = ()
    container: bool = False  # has children
    has_else: bool = False
    help: str = ""


_COND_SHOW = {
    "value": ("kind", ("app", "title", "time")),
    "variable": ("kind", ("variable",)),
}

SPECS: dict[str, ActionSpec] = {
    s.type: s
    for s in (
        ActionSpec(
            "key_press", "Press keys", "Keyboard",
            (Field("keys", "Keys", "combo", "ctrl+c", hint="e.g. ctrl+shift+t, enter, f5"),),
            help="Presses the keys together and releases them again.",
        ),
        ActionSpec("key_down", "Hold key down", "Keyboard", (Field("key", "Key", "key", "shift"),)),
        ActionSpec("key_up", "Release key", "Keyboard", (Field("key", "Key", "key", "shift"),)),
        ActionSpec(
            "type_text", "Type text", "Keyboard",
            (
                Field("text", "Text", "text", ""),
                Field("interval_ms", "Delay per character (ms)", "int", 0, maximum=5000),
            ),
        ),
        ActionSpec(
            "mouse_move", "Move mouse", "Mouse",
            (
                Field("x", "X", "int", 0, minimum=-100000, maximum=100000),
                Field("y", "Y", "int", 0, minimum=-100000, maximum=100000),
                Field("relative", "Relative to current position", "bool", False),
            ),
        ),
        ActionSpec(
            "mouse_click", "Mouse button", "Mouse",
            (
                Field("button", "Button", "choice", "left", MOUSE_BUTTONS),
                Field("state", "Action", "choice", "click", MOUSE_STATES),
                Field("count", "Click count", "int", 1, minimum=1, maximum=10,
                      show_if=("state", ("click",))),
                Field("move_to", "Move to position first", "bool", False),
                Field("x", "X", "int", 0, minimum=-100000, maximum=100000,
                      show_if=("move_to", (True,))),
                Field("y", "Y", "int", 0, minimum=-100000, maximum=100000,
                      show_if=("move_to", (True,))),
            ),
        ),
        ActionSpec(
            "mouse_scroll", "Scroll wheel", "Mouse",
            (
                Field("dx", "Horizontal", "int", 0, minimum=-1000, maximum=1000),
                Field("dy", "Vertical (negative = down)", "int", -3, minimum=-1000, maximum=1000),
            ),
        ),
        ActionSpec(
            "delay", "Delay", "Flow",
            (
                Field("ms", "Duration (ms)", "int", 500, maximum=MAX_DELAY_MS),
                Field("jitter_ms", "Random extra up to (ms)", "int", 0, maximum=MAX_DELAY_MS),
            ),
        ),
        ActionSpec(
            "repeat", "Repeat", "Flow",
            (Field("count", "Times (0 = until stopped)", "int", 2, maximum=1_000_000),),
            container=True,
            help="Runs the nested actions several times. Variable 'iteration' holds the loop number.",
        ),
        ActionSpec(
            "if", "If / else", "Flow",
            (
                Field("kind", "Condition", "choice", "app", CONDITION_KINDS),
                Field("value", "Value", "text", "",
                      show_if=_COND_SHOW["value"],
                      hint="app/title: text to look for; time: HH:MM-HH:MM"),
                Field("variable", "Variable", "text", "", show_if=_COND_SHOW["variable"]),
                Field("op", "Operator", "choice", "==", COMPARE_OPS, show_if=_COND_SHOW["variable"]),
                Field("compare_to", "Compare to", "text", "", show_if=_COND_SHOW["variable"]),
                Field("negate", "Negate (NOT)", "bool", False),
            ),
            container=True,
            has_else=True,
            help="Runs 'then' actions when the condition holds, otherwise the 'else' actions.",
        ),
        ActionSpec(
            "set_variable", "Set variable", "Flow",
            (
                Field("name", "Name", "text", "counter"),
                Field("value", "Value", "text", "0"),
                Field("mode", "Mode", "choice", "set", ("set", "add")),
            ),
        ),
        ActionSpec("stop", "Stop macro", "Flow", help="Ends the macro immediately."),
    )
}

# Only these types are offered as "flow containers".
CATEGORIES = ("Keyboard", "Mouse", "Flow")


def spec_for(action_type: str) -> ActionSpec:
    try:
        return SPECS[action_type]
    except KeyError:
        raise ValueError(f"unknown action type {action_type!r}") from None


def default_params(action_type: str) -> dict[str, Any]:
    return {f.name: f.default for f in spec_for(action_type).fields}


def new_action(action_type: str) -> Action:
    return Action(action_type, default_params(action_type))


def get_param(action: Action, name: str) -> Any:
    """Parameter value with the spec default as fallback."""
    if name in action.params:
        return action.params[name]
    for f in spec_for(action.type).fields:
        if f.name == name:
            return f.default
    raise KeyError(name)


_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def parse_time_range(text: str) -> tuple[int, int]:
    """``"09:00-17:30"`` -> minutes since midnight (start, end)."""
    m = _TIME_RE.match(text)
    if not m:
        raise ValueError("time must look like 09:00-17:30")
    h1, m1, h2, m2 = (int(g) for g in m.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        raise ValueError("time out of range")
    return h1 * 60 + m1, h2 * 60 + m2


def validate_action(action: Action, path: str = "") -> list[str]:
    """Return human readable problems for ``action`` and all nested actions."""
    where = path or action.type
    try:
        spec = spec_for(action.type)
    except ValueError as exc:
        return [f"{where}: {exc}"]

    errors: list[str] = []
    for f in spec.fields:
        value = get_param(action, f.name)
        if f.kind in ("int", "float"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors.append(f"{where}: '{f.label}' must be a number")
            elif not f.minimum <= value <= f.maximum:
                errors.append(f"{where}: '{f.label}' must be between {f.minimum:g} and {f.maximum:g}")
        elif f.kind == "bool":
            if not isinstance(value, bool):
                errors.append(f"{where}: '{f.label}' must be true or false")
        elif f.kind == "choice":
            if value not in f.choices:
                errors.append(f"{where}: '{f.label}' must be one of {', '.join(f.choices)}")
        elif not isinstance(value, str):
            errors.append(f"{where}: '{f.label}' must be text")

    if errors:
        return errors

    t = action.type
    try:
        if t == "key_press":
            if not keys.combo_keys(get_param(action, "keys")):
                raise ValueError("no keys given")
        elif t in ("key_down", "key_up"):
            keys.normalize_key(get_param(action, "key"))
        elif t == "type_text":
            if not get_param(action, "text"):
                errors.append(f"{where}: text is empty")
        elif t == "if":
            kind = get_param(action, "kind")
            if kind in ("app", "title") and not get_param(action, "value").strip():
                errors.append(f"{where}: condition value is empty")
            elif kind == "time":
                parse_time_range(get_param(action, "value"))
            elif kind == "variable" and not get_param(action, "variable").strip():
                errors.append(f"{where}: variable name is empty")
        elif t == "set_variable":
            if not get_param(action, "name").strip():
                errors.append(f"{where}: variable name is empty")
    except ValueError as exc:
        errors.append(f"{where}: {exc}")

    if not spec.container and (action.children or action.else_children):
        errors.append(f"{where}: this action cannot contain other actions")
    if not spec.has_else and action.else_children:
        errors.append(f"{where}: this action has no else branch")

    for i, child in enumerate(action.children, 1):
        errors += validate_action(child, f"{where} > then #{i}" if spec.has_else else f"{where} > #{i}")
    for i, child in enumerate(action.else_children, 1):
        errors += validate_action(child, f"{where} > else #{i}")
    return errors


def validate_actions(actions: list[Action]) -> list[str]:
    errors: list[str] = []
    for i, a in enumerate(actions, 1):
        errors += validate_action(a, f"Step {i} ({a.type})")
    return errors


def describe_condition(action: Action) -> str:
    kind = get_param(action, "kind")
    if kind == "app":
        text = f'active application contains "{get_param(action, "value")}"'
    elif kind == "title":
        text = f'window title contains "{get_param(action, "value")}"'
    elif kind == "time":
        text = f"time is between {get_param(action, 'value')}"
    else:
        text = f'{get_param(action, "variable")} {get_param(action, "op")} "{get_param(action, "compare_to")}"'
    return f"NOT ({text})" if get_param(action, "negate") else text


def describe(action: Action) -> str:
    """One-line summary used in the step list."""
    p = lambda n: get_param(action, n)  # noqa: E731
    t = action.type
    if t == "key_press":
        return f"Press {p('keys')}"
    if t == "key_down":
        return f"Hold down {p('key')}"
    if t == "key_up":
        return f"Release {p('key')}"
    if t == "type_text":
        text = p("text")
        shown = text if len(text) <= 40 else text[:37] + "..."
        return f'Type "{shown}"'
    if t == "mouse_move":
        return f"Move mouse {'by' if p('relative') else 'to'} ({p('x')}, {p('y')})"
    if t == "mouse_click":
        where = f" at ({p('x')}, {p('y')})" if p("move_to") else ""
        if p("state") == "click":
            n = p("count")
            return f"{p('button').capitalize()} click{' x' + str(n) if n > 1 else ''}{where}"
        return f"{p('button').capitalize()} button {p('state')}{where}"
    if t == "mouse_scroll":
        return f"Scroll (dx={p('dx')}, dy={p('dy')})"
    if t == "delay":
        extra = f" + up to {p('jitter_ms')} ms" if p("jitter_ms") else ""
        return f"Wait {p('ms')} ms{extra}"
    if t == "repeat":
        n = p("count")
        return "Repeat until stopped" if n == 0 else f"Repeat {n} time{'s' if n != 1 else ''}"
    if t == "if":
        return f"If {describe_condition(action)}"
    if t == "set_variable":
        op = "+=" if p("mode") == "add" else "="
        return f'Set {p("name")} {op} "{p("value")}"'
    if t == "stop":
        return "Stop macro"
    return t


def count_actions(actions: list[Action]) -> int:
    return sum(1 + count_actions(a.children) + count_actions(a.else_children) for a in actions)
