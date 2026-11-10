"""Pre-flight check: find what would go wrong (or surprise) *before* a macro runs.

Errors stop a macro (they are also refused when it runs). Warnings are things that
are probably not intended: a variable that is never set is typed as ``{name}``, a
key that does not exist on this system cannot be sent, a Repeat until stopped
without a pause runs as fast as possible. Info is useful to know, such as how long
a macro takes at its playback speed.

Steps are identified by their path in the step tree, the same as the editor's:
``[2, "children", 0]`` is the first step inside the third one.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from typing import Callable

from keyboardmanager.core import actions as catalog
from keyboardmanager.core import keys, os_shortcuts
from keyboardmanager.core.actions import get_param
from keyboardmanager.core.conflicts import abbreviation_conflict, hotkey_conflict
from keyboardmanager.core.events import validate_event
from keyboardmanager.core.models import Action, Macro
from keyboardmanager.core import script
from keyboardmanager.core.variables import BUILTINS, inline_expressions, placeholders

ERROR, WARNING, INFO = "error", "warning", "info"
SEVERITY_ORDER = {ERROR: 0, WARNING: 1, INFO: 2}

# parameters in which {placeholders} are replaced
_INTERPOLATED = {
    "type_text": ("text",), "paste_text": ("text",), "set_clipboard": ("text",),
    "set_variable": ("value",), "ask": ("prompt", "default"), "launch": ("target",),
    "notify": ("title", "message"), "if": ("value", "compare_to"), "while": ("value", "compare_to"),
    "wait_for_window": ("value",), "run_macro": ("name",),
}
_PAUSING = {"delay", "wait_for_window", "ask", "break", "stop"}  # steps that slow or end a loop


@dataclass(frozen=True)
class Issue:
    severity: str
    message: str
    macro_id: int | None = None
    macro_name: str = ""
    step: tuple = ()  # path of the step, () = the macro itself
    step_text: str = ""
    blocks_run: bool = False  # running the macro would fail (a broken step)


@dataclass
class CheckContext:
    macros: list[Macro] = field(default_factory=list)
    reserved_hotkeys: dict[str, str] = field(default_factory=dict)
    saved_variables: set[str] = field(default_factory=set)
    key_supported: Callable[[str], bool] = lambda key: True
    playback_speed: float = 1.0
    now: Callable[[], float] = time.time
    platform: str = field(default_factory=lambda: sys.platform)  # for the OS shortcut tables
    triggers: list = field(default_factory=list)  # trigger rules (core.triggers.TriggerRule)


def check_all(macros: list[Macro], context: CheckContext) -> list[Issue]:
    issues = [issue for macro in macros for issue in check_macro(macro, context)]
    issues += check_triggers(context.triggers, macros)
    return sorted(issues, key=lambda i: (SEVERITY_ORDER[i.severity], i.macro_name.lower(), i.step))


def check_macro(macro: Macro, context: CheckContext) -> list[Issue]:
    issues: list[Issue] = []

    def add(severity: str, message: str, step: tuple = (), action: Action | None = None,
            blocks_run: bool = False) -> None:
        issues.append(Issue(severity, message, macro.id, macro.name, step,
                            catalog.describe(action) if action is not None else "", blocks_run))

    # ------------------------------------------------------------ the macro
    if not macro.actions:
        add(WARNING, "has no steps: running it does nothing")
    try:
        validate_event(macro.event, macro)
    except ValueError as exc:
        add(ERROR, f"event: {exc}")
    if macro.hotkey:
        try:
            keys.normalize_hotkey_sequence(macro.hotkey)
            problem = hotkey_conflict(macro, context.macros, context.reserved_hotkeys)
            if problem:
                add(ERROR, f"hotkey: {problem}")
        except ValueError as exc:
            add(ERROR, f"hotkey: {exc}")
            problem = "invalid"
        if not problem:
            clash = os_shortcuts.warning(macro.hotkey, context.platform)
            if clash:
                add(WARNING, f"hotkey: {clash}")
    problem = abbreviation_conflict(macro, context.macros)
    if problem:
        add(ERROR, f"abbreviation: {problem}")
    if macro.schedule.active and macro.schedule.kind != "interval":
        try:
            if macro.schedule.next_after(context.now()) is None:
                add(WARNING, "its scheduled time has passed: the schedule will not run again")
        except ValueError as exc:
            add(ERROR, f"schedule: {exc}")
    triggers = [name for name, on in (("hotkey", macro.hotkey), ("abbreviation", macro.abbreviation),
                                       ("schedule", macro.schedule.active), ("event", macro.event)) if on]
    if not macro.enabled and triggers:
        add(INFO, f"disabled: its {', '.join(triggers)} will not start it")
    unattended = macro.schedule.active or macro.event
    if macro.layer:
        if macro.hotkey or macro.abbreviation:
            add(INFO, f'on layer "{macro.layer}": its hotkey / abbreviation only work while that layer is active')
        else:
            add(INFO, f'layer "{macro.layer}" has no effect: the macro has no hotkey or abbreviation')

    # --------------------------------------------------------------- steps
    known = _assigned_variables(macro, context, set()) | context.saved_variables | set(BUILTINS)
    by_name = {m.name.lower(): m for m in context.macros}

    def walk(actions: list[Action], prefix: tuple, in_loop: bool) -> None:
        for index, action in enumerate(actions):
            path = (*prefix, index)
            for problem in catalog.validate_action(Action(action.type, dict(action.params)), in_loop=in_loop):
                add(ERROR, problem.split(": ", 1)[-1], path, action, blocks_run=True)
            _check_step(action, path, add, context, known, by_name, macro, bool(unattended))
            loop = in_loop or action.type in catalog.LOOP_TYPES
            walk(action.children, (*path, "children"), loop)
            walk(action.else_children, (*path, "else_children"), loop)

    walk(macro.actions, (), False)

    if macro.actions and not any(i.severity == ERROR for i in issues):
        seconds, exact = estimate_duration(macro.actions)
        speed = macro.speed * context.playback_speed
        if seconds is None:
            add(INFO, "runs until it is stopped")
        elif seconds / speed >= 1:
            at = f" at {speed:g}x speed" if speed != 1 else ""
            add(INFO, f"takes {'about' if exact else 'at least'} {_duration(seconds / speed)}{at}")
        limit = macro.limits.max_seconds
        if limit and seconds is not None and seconds / speed > limit:
            add(WARNING, f"its time limit ({limit} s) is shorter than it takes "
                         f"({_duration(seconds / speed)}), so it always stops early")
    if macro.limits.active:
        add(INFO, f"limits: {macro.limits.describe()}")
    return issues


def _check_step(action: Action, path: tuple, add, context: CheckContext, known: set[str],
                by_name: dict[str, Macro], macro: Macro, unattended: bool) -> None:
    t = action.type
    for name in _INTERPOLATED.get(t, ()):
        value = get_param(action, name)
        for variable in placeholders(value) if isinstance(value, str) else []:
            if variable not in known:
                add(WARNING, f"{{{variable}}} is never set, so it is typed as it is", path, action)
        for expression in inline_expressions(value) if isinstance(value, str) else []:
            try:
                script.check_expression(expression)
            except script.ScriptError as exc:
                add(ERROR, f"{{={expression}}}: {exc}", path, action, blocks_run=True)
    for source, mode in _expressions_of(action):
        read, assigned = script.names(source, mode)
        for variable in sorted(read - known - assigned):
            add(WARNING, f'"{variable}" is never set: the {"script" if mode == "exec" else "expression"} '
                         "stops the macro with an error when it reads it", path, action)
    if t in ("if", "while") and get_param(action, "kind") == "variable":
        variable = get_param(action, "variable").strip()
        if variable and variable not in known:
            add(WARNING, f'variable "{variable}" is never set: it is always empty here', path, action)
    for key in _keys_of(action):
        if not context.key_supported(key):
            add(WARNING, f'the key "{key}" does not exist on this system, so it cannot be sent', path, action)
    if t == "repeat" and get_param(action, "count") == 0 and not _contains(action.children, _PAUSING):
        add(WARNING, "repeats as fast as possible until stopped (add a Delay inside)", path, action)
    if t == "run_macro":
        name = get_param(action, "name").strip()
        target = by_name.get(name.lower())
        if name and target is None and "{" not in name:
            add(ERROR, f'there is no macro named "{name}"', path, action, blocks_run=True)
        elif target is not None and _runs(target, macro.name.lower(), by_name, set()):
            add(WARNING, f'"{name}" runs "{macro.name}" again: it stops with an error after a few levels',
                path, action)
    if t == "ask" and unattended:
        add(WARNING, "waits for an answer, but this macro runs by itself (schedule / event)", path, action)
    image = _image_of(action)
    if image and "{" not in image and not os.path.isfile(os.path.expanduser(image)):
        add(ERROR, f"there is no image file {image}", path, action, blocks_run=True)
    if t == "switch_layer":
        wanted = get_param(action, "layer").strip().lower()
        known = {m.layer.lower() for m in context.macros if m.layer}
        if wanted and "{" not in wanted and wanted not in known:
            add(WARNING, f'no macro is on layer "{get_param(action, "layer").strip()}": '
                         "switching to it turns all layered hotkeys off", path, action)


def _expressions_of(action: Action) -> list[tuple[str, str]]:
    """The expressions / scripts in a step: (source, "eval" | "exec")."""
    t, found = action.type, []
    if t in ("if", "while") and get_param(action, "kind") == "expression":
        found.append((get_param(action, "expression"), "eval"))
    elif t == "set_variable" and get_param(action, "mode") == "expression":
        found.append((get_param(action, "value"), "eval"))
    elif t == "script":
        found.append((get_param(action, "code"), "exec"))
    for name in _INTERPOLATED.get(t, ()):
        value = get_param(action, name)
        if isinstance(value, str) and not (t == "set_variable" and found):
            found += [(e, "eval") for e in inline_expressions(value)]
    return found


def check_triggers(rules: list, macros: list[Macro]) -> list[Issue]:
    """Trigger rules that cannot work: a missing or disabled macro, a bad expression."""
    from keyboardmanager.core.triggers import MAX_CHAIN, validate_rule

    by_name = {m.name.lower(): m for m in macros}
    issues: list[Issue] = []
    for rule in rules:
        if not rule.enabled:
            continue

        def add(severity: str, message: str) -> None:
            issues.append(Issue(severity, message, None, f"trigger: {rule.name}"))

        try:
            validate_rule(rule)
        except ValueError as exc:
            add(ERROR, str(exc))
            continue
        target = by_name.get(rule.macro.strip().lower())
        if target is None:
            add(ERROR, f'there is no macro named "{rule.macro}" to run')
        elif not target.enabled:
            add(WARNING, f'"{target.name}" is disabled, so this trigger does not run it')
        if rule.kind == "macro_finished":
            watched = str(rule.params.get("macro", ""))
            if watched.strip().lower() not in by_name:
                add(WARNING, f'there is no macro named "{watched}", so this trigger never fires')
            elif target is not None and watched.strip().lower() == target.name.lower():
                add(INFO, f'"{target.name}" starts itself again when it finishes '
                          f"(at most {MAX_CHAIN} times in a row)")
    return issues


def _image_of(action: Action) -> str:
    """The image file a step looks for, "" when it has none."""
    if action.type in ("wait_for_image", "click_image"):
        return get_param(action, "image").strip()
    if action.type in ("if", "while") and get_param(action, "kind") == "image":
        return get_param(action, "image").strip()
    return ""


def _keys_of(action: Action) -> list[str]:
    try:
        if action.type == "key_press":
            return keys.combo_keys(get_param(action, "keys"))
        if action.type in ("key_down", "key_up"):
            return [keys.normalize_key(get_param(action, "key"))]
    except ValueError:
        pass  # reported as an error by the validation
    return []


def _contains(actions: list[Action], types: set[str]) -> bool:
    return any(a.type in types or _contains(a.children, types) or _contains(a.else_children, types)
               for a in actions)


def _all_steps(actions: list[Action]):
    for a in actions:
        yield a
        yield from _all_steps(a.children)
        yield from _all_steps(a.else_children)


def _assigned_variables(macro: Macro, context: CheckContext, visited: set[str]) -> set[str]:
    """Variables that may be set while ``macro`` runs (also by the macros it runs)."""
    visited.add(macro.name.lower())
    names: set[str] = set()
    by_name = {m.name.lower(): m for m in context.macros}
    for a in _all_steps(macro.actions):
        if a.type == "set_variable":
            names.add(get_param(a, "name").strip())
        elif a.type == "script":
            names |= script.names(get_param(a, "code"), "exec")[1]
        elif a.type == "ask":
            names.add(get_param(a, "variable").strip())
        elif a.type in catalog.LOOP_TYPES:
            names.add("iteration")
        elif a.type == "wait_for_window":
            names.add("timed_out")
        elif a.type == "run_macro":
            target = by_name.get(get_param(a, "name").strip().lower())
            if target is not None and target.name.lower() not in visited:
                names |= _assigned_variables(target, context, visited)
    return names


def _runs(macro: Macro, wanted: str, by_name: dict[str, Macro], visited: set[str]) -> bool:
    """Whether running ``macro`` ends up running the macro named ``wanted``."""
    if macro.name.lower() in visited:
        return False
    visited.add(macro.name.lower())
    for a in _all_steps(macro.actions):
        if a.type == "run_macro":
            name = get_param(a, "name").strip().lower()
            if name == wanted or (name in by_name and _runs(by_name[name], wanted, by_name, visited)):
                return True
    return False


def estimate_duration(actions: list[Action]) -> tuple[float | None, bool]:
    """Seconds the waits of ``actions`` take at 1x (``None``: until stopped), and
    whether that is exact (loops with conditions and waits for windows are not)."""
    total, exact = 0.0, True
    for a in actions:
        t = a.type
        if t == "delay":
            total += (get_param(a, "ms") + get_param(a, "jitter_ms") / 2) / 1000
            exact = exact and not get_param(a, "jitter_ms")
        elif t == "type_text":
            total += get_param(a, "interval_ms") * len(get_param(a, "text")) / 1000
        elif t == "paste_text":
            total += 0.15
        elif t in ("wait_for_window", "ask", "while", "run_macro"):
            exact = False
            if t == "while":
                inner, _ = estimate_duration(a.children)
                if inner is None:
                    return None, False
                total += inner  # at least one round, usually
        elif t == "repeat":
            inner, inner_exact = estimate_duration(a.children)
            if get_param(a, "count") == 0:
                return None, False
            if inner is None:
                return None, False
            total += inner * get_param(a, "count")
            exact = exact and inner_exact and not _contains(a.children, {"break"})
        elif t == "if":
            branches = [estimate_duration(a.children), estimate_duration(a.else_children)]
            if any(b[0] is None for b in branches):
                return None, False
            total += min(b[0] for b in branches)  # type: ignore[type-var]
            exact = exact and branches[0][0] == branches[1][0]
        elif t == "stop":
            break
    return total, exact


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, rest = divmod(int(round(seconds)), 60)
    return f"{minutes} min {rest} s" if minutes < 60 else f"{minutes // 60} h {minutes % 60} min"
