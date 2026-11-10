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
CONDITION_KINDS = ("app", "title", "time", "variable", "expression", "image")
COMPARE_OPS = ("==", "!=", ">", "<", ">=", "<=", "contains", "starts with", "ends with", "matches", "is empty")
VARIABLE_MODES = ("set", "add", "subtract", "append", "calculate", "expression")
VARIABLE_SCOPES = ("run", "saved")
TIMEOUT_ACTIONS = ("error", "continue")
MOUSE_BUTTONS = ("left", "right", "middle")
MOUSE_STATES = ("click", "press", "release")


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    kind: str  # text | multiline | code | macro | combo | key | int | float | bool | choice | image
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
_IMAGE_HINT = "a screenshot of what to look for (PNG); capture it with the button"
_TOLERANCE_HINT = ("0 = the pixels must match exactly; higher allows small colour "
                   "differences (slower)")
_PLACEHOLDER_HINT = "{name} inserts a variable, e.g. {date}, {clipboard}, {counter}; {= expression} a result"


def _condition_fields() -> tuple[Field, ...]:
    return (
        Field("kind", "Condition", "choice", "app", CONDITION_KINDS),
        Field("value", "Value", "text", "",
              show_if=_COND_SHOW["value"],
              hint="app/title: text to look for; time: HH:MM-HH:MM"),
        Field("variable", "Variable", "text", "", show_if=_COND_SHOW["variable"]),
        Field("op", "Operator", "choice", "==", COMPARE_OPS, show_if=_COND_SHOW["variable"]),
        Field("compare_to", "Compare to", "text", "", show_if=_COND_SHOW["variable"],
              hint="matches: a regular expression. " + _PLACEHOLDER_HINT),
        Field("expression", "Expression", "text", "", show_if=("kind", ("expression",)),
              hint='e.g. len(clipboard) > 10 and "Mail" in app'),
        Field("image", "Image", "image", "", show_if=("kind", ("image",)), hint=_IMAGE_HINT),
        Field("tolerance", "Tolerance", "int", 0, maximum=255, show_if=("kind", ("image",)),
              hint=_TOLERANCE_HINT),
        Field("negate", "Negate (NOT)", "bool", False),
    )

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
                Field("text", "Text", "multiline", "", hint=_PLACEHOLDER_HINT),
                Field("interval_ms", "Delay per character (ms)", "int", 0, maximum=5000),
            ),
        ),
        ActionSpec(
            "paste_text", "Paste text", "Keyboard",
            (Field("text", "Text", "multiline", "", hint=_PLACEHOLDER_HINT),),
            help="Puts the text on the clipboard and pastes it: fast, and works for any "
                 "characters. The previous clipboard content is restored afterwards.",
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
            "while", "While (loop)", "Flow",
            (*_condition_fields(),
             Field("max_iterations", "At most (times)", "int", 1000, minimum=1, maximum=1_000_000)),
            container=True,
            help="Repeats the nested actions as long as the condition holds (checked before "
                 "each round). Variable 'iteration' holds the round number.",
        ),
        ActionSpec("break", "Break (leave loop)", "Flow", help="Leaves the innermost Repeat or While loop."),
        ActionSpec(
            "if", "If / else", "Flow", _condition_fields(),
            container=True,
            has_else=True,
            help="Runs 'then' actions when the condition holds, otherwise the 'else' actions.",
        ),
        ActionSpec(
            "run_macro", "Run another macro", "Flow",
            (Field("name", "Macro", "macro", ""),),
            help="Runs another macro's steps here, sharing this run's variables.",
        ),
        ActionSpec(
            "wait_for_window", "Wait for window", "Flow",
            (
                Field("kind", "Wait until the", "choice", "app", ("app", "title")),
                Field("value", "contains", "text", "", hint="part of the application name or window title"),
                Field("timeout_s", "Timeout (seconds)", "int", 10, minimum=1, maximum=3600),
                Field("on_timeout", "On timeout", "choice", "error", TIMEOUT_ACTIONS),
            ),
            help="Waits until the focused application (or window title) contains the text.",
        ),
        ActionSpec("stop", "Stop macro", "Flow", help="Ends the macro immediately."),
        ActionSpec(
            "comment", "Comment", "Flow", (Field("text", "Note", "text", ""),),
            help="A note for yourself; does nothing when the macro runs.",
        ),
        ActionSpec(
            "set_variable", "Set variable", "Data",
            (
                Field("name", "Name", "text", "counter"),
                Field("value", "Value", "text", "0",
                      hint=_PLACEHOLDER_HINT + "; calculate: e.g. {counter} * 2; expression: e.g. upper(name)"),
                Field("mode", "Mode", "choice", "set", VARIABLE_MODES),
                Field("scope", "Keep", "choice", "run", VARIABLE_SCOPES,
                      hint="run: only during this run; saved: kept for later runs and all macros"),
            ),
        ),
        ActionSpec(
            "ask", "Ask for input", "Data",
            (
                Field("prompt", "Question", "text", "Value?"),
                Field("variable", "Store in variable", "text", "answer"),
                Field("default", "Suggested answer", "text", ""),
            ),
            help="Shows a question; the answer is stored in a variable. Cancel stops the macro.",
        ),
        ActionSpec(
            "script", "Run script", "Data",
            (Field("code", "Script", "code", 'name = upper(name)\ntype("Hello " + name)'),),
            help="Statements in a small Python-like language: variables, if / while / for and "
                 "commands such as type(), press(), wait(), run(). See the Variables tab.",
        ),
        ActionSpec(
            "set_clipboard", "Set clipboard", "Data",
            (Field("text", "Text", "multiline", "", hint=_PLACEHOLDER_HINT),),
        ),
        ActionSpec(
            "wait_for_image", "Wait for image", "Vision",
            (
                Field("image", "Image", "image", "", hint=_IMAGE_HINT),
                Field("until", "Wait until it", "choice", "appears", ("appears", "disappears")),
                Field("timeout_s", "Timeout (seconds)", "int", 10, minimum=1, maximum=3600),
                Field("tolerance", "Tolerance", "int", 0, maximum=255, hint=_TOLERANCE_HINT),
                Field("on_timeout", "On timeout", "choice", "error", TIMEOUT_ACTIONS),
            ),
            help="Waits until the image is (or is no longer) somewhere on the screen. "
                 "Variables: found_x / found_y (the middle of the match) and timed_out.",
        ),
        ActionSpec(
            "click_image", "Click image", "Vision",
            (
                Field("image", "Image", "image", "", hint=_IMAGE_HINT),
                Field("button", "Button", "choice", "left", MOUSE_BUTTONS),
                Field("count", "Click count", "int", 1, minimum=1, maximum=10),
                Field("offset_x", "Offset X", "int", 0, minimum=-10000, maximum=10000,
                      hint="click this far from the middle of the match"),
                Field("offset_y", "Offset Y", "int", 0, minimum=-10000, maximum=10000),
                Field("move_only", "Only move the mouse (no click)", "bool", False),
                Field("timeout_s", "Wait for it up to (seconds)", "int", 5, minimum=1, maximum=3600),
                Field("tolerance", "Tolerance", "int", 0, maximum=255, hint=_TOLERANCE_HINT),
                Field("on_timeout", "If it does not appear", "choice", "error", TIMEOUT_ACTIONS),
            ),
            help="Finds the image on the screen, moves the mouse to the middle of the match "
                 "and clicks. Variables: found_x / found_y and timed_out.",
        ),
        ActionSpec(
            "focus_window", "Focus window", "Vision",
            (
                Field("app", "Application", "text", "", hint="e.g. chrome, code (as in the Application filter)"),
                Field("title", "Window title", "text", "", hint="part of the window title"),
                Field("timeout_s", "Timeout (seconds)", "int", 5, minimum=1, maximum=3600),
                Field("on_timeout", "On timeout", "choice", "error", TIMEOUT_ACTIONS),
            ),
            help="Brings a window of another application to the front (best effort; on macOS "
                 "the application is activated as a whole). Variable: timed_out.",
        ),
        ActionSpec(
            "switch_profile", "Switch profile", "System",
            (Field("profile", "Profile", "text", "", hint="empty = All profiles"),),
            help="Changes the active profile, as the toolbar does.",
        ),
        ActionSpec(
            "switch_layer", "Switch layer", "System",
            (
                Field("layer", "Layer", "text", "", hint="empty = back to the base layer"),
                Field("one_shot", "One shot (back to base after the next hotkey)", "bool", False),
            ),
            help="Changes the active keymap layer: hotkeys and abbreviations of macros on "
                 "other layers stop working, those on this layer start working.",
        ),
        ActionSpec(
            "launch", "Open app, file or URL", "System",
            (Field("target", "Open", "text", "", hint="e.g. https://example.com, a file path or an application name"),),
        ),
        ActionSpec(
            "notify", "Show notification", "System",
            (Field("title", "Title", "text", "Keyboard Manager"),
             Field("message", "Message", "text", "", hint=_PLACEHOLDER_HINT)),
        ),
    )
}

CATEGORIES = ("Keyboard", "Mouse", "Flow", "Data", "Vision", "System")
LOOP_TYPES = ("repeat", "while")


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


def validate_action(action: Action, path: str = "", in_loop: bool = False) -> list[str]:
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
        elif not isinstance(value, str):  # text, multiline, macro, key, combo
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
        elif t in ("if", "while"):
            kind = get_param(action, "kind")
            if kind in ("app", "title") and not get_param(action, "value").strip():
                errors.append(f"{where}: condition value is empty")
            elif kind == "image" and not get_param(action, "image").strip():
                errors.append(f"{where}: choose the image to look for")
            elif kind == "time":
                parse_time_range(get_param(action, "value"))
            elif kind == "expression":
                from keyboardmanager.core.script import check_expression

                check_expression(get_param(action, "expression") or " ")
            elif kind == "variable":
                _check_variable_name(get_param(action, "variable"))
                pattern = get_param(action, "compare_to")
                if get_param(action, "op") == "matches" and "{" not in pattern:
                    try:
                        re.compile(pattern)
                    except re.error as exc:
                        raise ValueError(f"invalid regular expression: {exc}") from None
        elif t == "set_variable":
            _check_variable_name(get_param(action, "name"))
            if get_param(action, "mode") == "expression":
                from keyboardmanager.core.script import check_expression

                check_expression(get_param(action, "value") or " ")
        elif t == "script":
            from keyboardmanager.core.script import check_script

            check_script(get_param(action, "code"))
        elif t == "ask":
            _check_variable_name(get_param(action, "variable"))
        elif t == "paste_text" and not get_param(action, "text"):
            errors.append(f"{where}: text is empty")
        elif t == "run_macro" and not get_param(action, "name").strip():
            errors.append(f"{where}: choose the macro to run")
        elif t == "launch" and not get_param(action, "target").strip():
            errors.append(f"{where}: say what to open")
        elif t == "wait_for_window" and not get_param(action, "value").strip():
            errors.append(f"{where}: say which window to wait for")
        elif t in ("wait_for_image", "click_image") and not get_param(action, "image").strip():
            errors.append(f"{where}: choose the image to look for")
        elif t == "focus_window" and not (get_param(action, "app").strip() or get_param(action, "title").strip()):
            errors.append(f"{where}: say which window to focus (application and/or title)")
        elif t == "break" and not in_loop:
            errors.append(f"{where}: Break only works inside a Repeat or While loop")
    except ValueError as exc:
        errors.append(f"{where}: {exc}")

    if not spec.container and (action.children or action.else_children):
        errors.append(f"{where}: this action cannot contain other actions")
    if not spec.has_else and action.else_children:
        errors.append(f"{where}: this action has no else branch")

    inner_loop = in_loop or t in LOOP_TYPES
    for i, child in enumerate(action.children, 1):
        errors += validate_action(child, f"{where} > then #{i}" if spec.has_else else f"{where} > #{i}", inner_loop)
    for i, child in enumerate(action.else_children, 1):
        errors += validate_action(child, f"{where} > else #{i}", inner_loop)
    return errors


def _check_variable_name(name: str) -> None:
    from keyboardmanager.core.variables import valid_name

    if not name.strip():
        raise ValueError("variable name is empty")
    if not valid_name(name.strip()):
        raise ValueError(f"{name!r} is not a valid variable name (letters, digits and _)")


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
    elif kind == "expression":
        text = get_param(action, "expression")
    elif kind == "image":
        text = f'{_file_name(get_param(action, "image"))} is on the screen'
    elif get_param(action, "op") == "is empty":
        text = f'{get_param(action, "variable")} is empty'
    else:
        text = f'{get_param(action, "variable")} {get_param(action, "op")} "{get_param(action, "compare_to")}"'
    return f"NOT ({text})" if get_param(action, "negate") else text


def _file_name(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or path


def _short(text: str, limit: int = 40) -> str:
    text = text.replace("\n", " \u23ce ")
    return text if len(text) <= limit else text[: limit - 3] + "..."


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
        return f'Type "{_short(p("text"))}"'
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
        op = {"set": "=", "add": "+=", "subtract": "-=", "append": "append", "calculate": "= calc",
              "expression": "= expr"}[p("mode")]
        saved = " (saved)" if p("scope") == "saved" else ""
        return f'Set {p("name")}{saved} {op} "{_short(p("value"))}"'
    if t == "stop":
        return "Stop macro"
    if t == "script":
        lines = [line for line in p("code").splitlines() if line.strip()]
        first = lines[0].strip() if lines else ""
        return f"Script: {_short(first, 36)}" + (f" (+{len(lines) - 1} lines)" if len(lines) > 1 else "")
    if t == "paste_text":
        return f'Paste "{_short(p("text"))}"'
    if t == "while":
        return f"While {describe_condition(action)}"
    if t == "break":
        return "Break out of the loop"
    if t == "run_macro":
        return f'Run macro "{p("name")}"'
    if t == "wait_for_window":
        what = "application" if p("kind") == "app" else "window title"
        return f'Wait until the {what} contains "{p("value")}" (max {p("timeout_s")} s)'
    if t == "wait_for_image":
        return f'Wait until {_file_name(p("image"))} {p("until")} (max {p("timeout_s")} s)'
    if t == "click_image":
        doing = "Move mouse to" if p("move_only") else f"{p('button').capitalize()} click"
        n = p("count")
        times = f" x{n}" if n > 1 and not p("move_only") else ""
        return f'{doing}{times} {_file_name(p("image"))}'
    if t == "focus_window":
        wanted = " / ".join(x for x in (p("app").strip(), p("title").strip()) if x)
        return f'Focus window "{wanted}"'
    if t == "switch_profile":
        return f'Switch to profile "{p("profile")}"' if p("profile").strip() else "Switch to All profiles"
    if t == "switch_layer":
        if not p("layer").strip():
            return "Back to the base layer"
        return f'Switch to layer "{p("layer")}"' + (" (one shot)" if p("one_shot") else "")
    if t == "comment":
        return f"# {p('text')}"
    if t == "ask":
        return f'Ask "{_short(p("prompt"))}" -> {p("variable")}'
    if t == "set_clipboard":
        return f'Set clipboard to "{_short(p("text"))}"'
    if t == "launch":
        return f'Open "{_short(p("target"))}"'
    if t == "notify":
        return f'Notify "{_short(p("message") or p("title"))}"'
    return t


def count_actions(actions: list[Action]) -> int:
    return sum(1 + count_actions(a.children) + count_actions(a.else_children) for a in actions)
