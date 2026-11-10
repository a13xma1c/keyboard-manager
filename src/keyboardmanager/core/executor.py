"""Macro execution engine (no Qt; fully testable with a fake backend)."""

from __future__ import annotations

import random
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

import os

from keyboardmanager.core import actions as catalog
from keyboardmanager.core import script
from keyboardmanager.core import keys
from keyboardmanager.core import vision
from keyboardmanager.core import windowcontrol
from keyboardmanager.core.actions import get_param
from keyboardmanager.core.backend import InputBackend
from keyboardmanager.core.debugger import Debugger
from keyboardmanager.core.interaction import HeadlessInterface, UserInterface
from keyboardmanager.core.models import Action, Macro
from keyboardmanager.core.variables import (
    SavedVariables,
    builtin_value,
    calculate,
    format_number,
    interpolate,
    to_number,
)

MAX_ACTIONS = 1_000_000  # hard safety net against runaway macros
MAX_MACRO_DEPTH = 8      # "Run another macro" nesting (also stops macros calling each other)
WINDOW_POLL_S = 0.2
IMAGE_POLL_S = 0.3  # between screen grabs while waiting for an image
SPEED_CHUNK_S = 0.1      # scaled waits re-read the speed this often (live speed changes)


class StopMacro(Exception):
    """Raised internally to unwind when the user stops or a 'stop' action runs."""


class BreakLoop(Exception):
    """Raised by a 'break' action; caught by the innermost loop."""


class LimitReached(StopMacro):
    """The macro's time or step limit ended the run."""


MAX_LOGGED_STEPS = 2000  # per run; a long loop would otherwise fill the history
SCRIPT_COMMANDS = frozenset({
    "type", "paste", "press", "hold", "release", "wait", "click", "move", "scroll", "run",
    "notify", "ask", "log", "stop", "find", "pixel", "focus", "layer", "profile",
})


@dataclass
class StepLog:
    """One executed step, for the history's step log."""

    seq: int
    depth: int
    action_type: str
    description: str
    offset_ms: int            # when it started, from the start of the run
    duration_ms: int = 0
    status: str = "ok"        # ok | error | stopped
    detail: str = ""


@dataclass
class ExecutionResult:
    status: str  # success | stopped | error
    actions_run: int = 0
    duration_ms: int = 0
    message: str = ""
    variables: dict[str, str] = field(default_factory=dict)
    steps: list[StepLog] = field(default_factory=list)
    steps_not_logged: int = 0


class MacroExecutor:
    def __init__(
        self,
        backend: InputBackend,
        now: Callable[[], datetime] = datetime.now,
        rng: random.Random | None = None,
        ui: UserInterface | None = None,
        saved: SavedVariables | None = None,
        find_macro: Callable[[str], Macro | None] | None = None,
        speed: Callable[[], float] | None = None,
        log_steps: bool = True,
        debugger: Debugger | None = None,
        set_layer: Callable[[str, bool], None] | None = None,
        set_profile: Callable[[str], None] | None = None,
    ) -> None:
        """``speed`` returns the global playback speed; it multiplies the macro's own."""
        self.backend = backend
        self._now = now
        self._rng = rng or random.Random()
        self.ui = ui or HeadlessInterface()
        self._saved = saved
        self._find_macro = find_macro or (lambda name: None)
        self._macro_name = ""
        self._depth = 0
        self._global_speed = speed or (lambda: 1.0)
        self._macro_speed = 1.0
        self._log_enabled = log_steps
        self._debugger = debugger
        self._set_layer = set_layer      # the service's; None when running stand-alone
        self._set_profile = set_profile
        self._templates: dict[str, vision.Image] = {}  # image files, loaded once per run
        self._step_path: tuple = ()  # path of the step running now (for the live view)
        self._step_text = ""
        self._log: list[StepLog] = []
        self._log_open: list[StepLog | None] = []  # entries of the steps running now (innermost last)
        self._not_logged = 0
        self._started = 0.0
        self._paused_time = 0.0
        self._paused_since = 0.0  # when the current pause began (0 = not paused)
        self._time_limit = 0.0
        self._max_steps = MAX_ACTIONS
        # _stop / _paused are guarded by _cond; waits (delays, pause) sleep on it so
        # stop(), pause() and resume() wake them immediately from any thread.
        self._cond = threading.Condition()
        self._stop = False
        self._paused = False
        self._vars: dict[str, str] = {}
        self._held: list[str] = []
        self._held_buttons: list[str] = []
        self._count = 0

    # ----------------------------------------------------------------- control
    def stop(self) -> None:
        """Stop as soon as possible. Safe to call from any thread, also before ``run``."""
        with self._cond:
            self._stop = True
            self._cond.notify_all()

    def pause(self) -> None:
        """Suspend at the next pause point (between steps, typed characters, within delays).

        While paused, keys and mouse buttons held by the macro are released so the
        user's own input is not affected; they are pressed again on resume. The
        remaining time of an interrupted delay is preserved. Can be called before ``run``.
        """
        with self._cond:
            self._paused = True
            self._cond.notify_all()

    def resume(self) -> None:
        with self._cond:
            self._paused = False
            self._cond.notify_all()

    @property
    def stopped(self) -> bool:
        return self._stop

    @property
    def paused(self) -> bool:
        return self._paused

    def run(
        self,
        macro: Macro,
        variables: dict[str, str] | None = None,
        start_delay: float = 0,
    ) -> ExecutionResult:
        """Execute ``macro``; ``start_delay`` seconds are waited first (pausable, stoppable)."""
        self._vars = dict(variables or {})
        self._held, self._held_buttons, self._count = [], [], 0
        self._macro_name, self._depth = macro.name, 0
        self._macro_speed = macro.speed
        self._log, self._log_open, self._not_logged = [], [], 0
        self._templates = {}
        self._paused_time = 0.0
        self._time_limit = 0.0  # switched on after a test run's countdown, see below
        self._max_steps = macro.limits.max_steps or MAX_ACTIONS
        started = self._started = time.monotonic()
        status, message = "success", ""
        try:
            problems = catalog.validate_actions(macro.actions)
            if problems:
                raise ValueError(problems[0])
            try:
                self._sleep(start_delay)
                self._check()
            except StopMacro:
                message = "Stopped before start"
                raise
            # the time limit counts from here: a test run's countdown is not part of it
            self._started, self._paused_time = time.monotonic(), 0.0
            self._time_limit = float(macro.limits.max_seconds)
            self._run_list(macro.actions)
        except LimitReached as exc:
            status, message = "stopped", f"Stopped: {exc}"
        except StopMacro:
            status = "stopped"
            message = message or "Stopped"
        except Exception as exc:  # noqa: BLE001 - report every failure in the history
            status, message = "error", f"{type(exc).__name__}: {exc}"
        finally:
            self._release_all()
            with self._cond:  # leave the executor reusable
                self._stop = self._paused = False
        return ExecutionResult(
            status=status,
            actions_run=self._count,
            duration_ms=int((time.monotonic() - started) * 1000),
            message=message,
            variables=dict(self._vars),
            steps=list(self._log),
            steps_not_logged=self._not_logged,
        )

    # ---------------------------------------------------------------- internals
    def _release_all(self) -> None:
        for key in reversed(self._held):
            try:
                self.backend.key_up(key)
            except Exception:  # noqa: BLE001
                pass
        for button in self._held_buttons:
            try:
                self.backend.mouse_up(button)
            except Exception:  # noqa: BLE001
                pass
        self._held, self._held_buttons = [], []

    def _active_seconds(self) -> float:
        """Running time so far, without the time spent paused."""
        return time.monotonic() - self._started - self._paused_time

    def _check(self) -> None:
        """Pause point: raise if stopped or over the time limit, block while paused."""
        if self._stop:
            raise StopMacro
        if self._paused:
            self._wait_while_paused()
        if self._time_limit and self._active_seconds() >= self._time_limit:
            raise LimitReached(f"time limit of {self._time_limit:g} s reached")

    def _wait_while_paused(self) -> None:
        keys_held, buttons_held = list(self._held), list(self._held_buttons)
        self._release_all()  # hand the keyboard and mouse back to the user
        paused_at = self._paused_since = time.monotonic()
        with self._cond:
            while self._paused and not self._stop:
                self._cond.wait()
            self._paused_time += time.monotonic() - paused_at  # not counted for the time limit
            self._paused_since = 0.0
            if self._stop:
                raise StopMacro
        # restore what the macro was holding, tracking each one so a failure still releases it
        for key in keys_held:
            self.backend.key_down(key)
            self._held.append(key)
        for button in buttons_held:
            self.backend.mouse_down(button)
            self._held_buttons.append(button)

    def _sleep(self, seconds: float) -> None:
        """Wait ``seconds`` of *unpaused* time; raises ``StopMacro`` when stopped."""
        remaining = seconds
        while remaining > 0:
            self._check()  # blocks while paused, so paused time is not counted
            began = time.monotonic()
            with self._cond:
                if not self._stop and not self._paused:
                    self._cond.wait(remaining)
            remaining -= time.monotonic() - began
        if seconds > 0 and self._stop:
            raise StopMacro

    def speed_factor(self) -> float:
        """How much faster than recorded the macro plays (macro speed x global speed)."""
        try:
            return max(0.01, self._macro_speed * float(self._global_speed()))
        except (TypeError, ValueError):
            return max(0.01, self._macro_speed)

    def _wait_scaled(self, seconds: float) -> None:
        """Wait a *macro* duration at the playback speed. Waited in short pieces so a
        speed change applies straight away, also in the middle of a long delay."""
        remaining = seconds
        while remaining > 1e-6:
            factor = self.speed_factor()
            step = min(SPEED_CHUNK_S, remaining / factor)
            self._sleep(step)
            remaining -= step * factor

    def _run_list(self, actions: list[Action], prefix: tuple = ()) -> None:
        for index, action in enumerate(actions):
            path = (*prefix, index)
            self._step_path, self._step_text = path, catalog.describe(action)
            if self._debugger is not None:
                self._debugger.before_step(path, action)  # may pause: _check() then waits
            self._check()
            self._count += 1
            if self._count > self._max_steps and self._max_steps < MAX_ACTIONS:
                raise LimitReached(f"step limit of {self._max_steps} reached")
            if self._count > MAX_ACTIONS:
                raise RuntimeError("action limit exceeded")
            entry = self._log_start(action)
            began = time.monotonic()
            try:
                getattr(self, f"_do_{action.type}")(action)
            except BreakLoop:
                self._log_end(entry, began, "ok")
                raise
            except StopMacro as exc:
                self._log_end(entry, began, "stopped", str(exc) if isinstance(exc, LimitReached) else "")
                raise
            except Exception as exc:
                self._log_end(entry, began, "error", f"{type(exc).__name__}: {exc}")
                raise
            self._log_end(entry, began, "ok")

    # live view
    def snapshot(self, recent: int = 30) -> dict:
        """The state of the run for the live view (read from another thread)."""
        return {
            "step": self._step_path, "step_text": self._step_text, "steps_run": self._count,
            "elapsed": self._elapsed(),
            "recent": list(self._log[-recent:]), "not_logged": self._not_logged,
            "variables": dict(self._vars),
        }

    def _elapsed(self) -> float:
        """Running time without pauses; frozen while paused."""
        if not self._started:
            return 0.0
        now = self._paused_since or time.monotonic()
        return max(0.0, now - self._started - self._paused_time)

    # step log
    def _log_start(self, action: Action) -> StepLog | None:
        entry = None
        if self._log_enabled:
            if len(self._log) < MAX_LOGGED_STEPS:
                entry = StepLog(
                    seq=len(self._log) + 1, depth=len(self._log_open), action_type=action.type,
                    description=catalog.describe(action),
                    offset_ms=int((time.monotonic() - self._started) * 1000),
                )
                self._log.append(entry)
            else:
                self._not_logged += 1
        self._log_open.append(entry)
        return entry

    def _log_end(self, entry: StepLog | None, began: float, status: str, problem: str = "") -> None:
        self._log_open.pop()
        if entry is not None:
            entry.duration_ms = int((time.monotonic() - began) * 1000)
            entry.status = status
            if problem:
                entry.detail = f"{entry.detail}; {problem}" if entry.detail else problem

    def _note(self, text: str) -> None:
        """Add ``text`` to the log entry of the step that is running now."""
        entry = self._log_open[-1] if self._log_open else None
        if entry is not None:
            entry.detail = f"{entry.detail}; {text}" if entry.detail else text

    # keyboard
    def _do_key_press(self, a: Action) -> None:
        combo = keys.combo_keys(get_param(a, "keys"))
        pressed: list[str] = []
        try:
            for k in combo:
                self.backend.key_down(k)
                pressed.append(k)
        finally:
            for k in reversed(pressed):
                self.backend.key_up(k)

    def _do_key_down(self, a: Action) -> None:
        k = keys.normalize_key(get_param(a, "key"))
        self.backend.key_down(k)
        self._held.append(k)

    def _do_key_up(self, a: Action) -> None:
        k = keys.normalize_key(get_param(a, "key"))
        self.backend.key_up(k)
        if k in self._held:
            self._held.remove(k)

    # variables
    def _lookup(self, name: str, fmt: str = "") -> str | None:
        """A placeholder's value: this run's variables, saved ones, then built-ins."""
        if name in self._vars:
            return self._vars[name]
        if self._saved is not None:
            value = self._saved.get(name)
            if value is not None:
                return value
        return builtin_value(
            name, fmt, now=self._now, rng=self._rng, clipboard=self.ui.get_clipboard,
            window=lambda: (lambda w: (w.app, w.title))(self.backend.active_window()),
            macro_name=self._macro_name,
        )

    def _text(self, a: Action, name: str) -> str:
        return interpolate(get_param(a, name), self._lookup, self._inline_expression)

    def _value(self, name: str):
        """A variable for expressions and scripts; KeyError when it does not exist."""
        value = self._lookup(name)
        if value is None:
            raise KeyError(name)
        return value

    def _evaluate(self, expression: str):
        return script.evaluate(expression, self._value)

    def _inline_expression(self, expression: str) -> str:
        try:
            return script.to_text(self._evaluate(expression))
        except script.ScriptError as exc:
            raise ValueError(f"in {{={expression}}}: {exc}") from None

    def _do_type_text(self, a: Action) -> None:
        text = self._text(a, "text")
        self._note(f"{len(text)} characters")  # not the text itself: it may be private
        interval = get_param(a, "interval_ms") / 1000
        if interval <= 0:
            self.backend.type_text(text)
            return
        for ch in text:
            self._check()
            self.backend.type_text(ch)
            self._wait_scaled(interval)

    # mouse
    def _do_mouse_move(self, a: Action) -> None:
        x, y = get_param(a, "x"), get_param(a, "y")
        if get_param(a, "relative"):
            cx, cy = self.backend.mouse_position()
            x, y = cx + x, cy + y
        self.backend.mouse_move(int(x), int(y))

    def _do_mouse_click(self, a: Action) -> None:
        if get_param(a, "move_to"):
            self.backend.mouse_move(int(get_param(a, "x")), int(get_param(a, "y")))
        button, state = get_param(a, "button"), get_param(a, "state")
        if state == "click":
            self.backend.mouse_click(button, int(get_param(a, "count")))
        elif state == "press":
            self.backend.mouse_down(button)
            self._held_buttons.append(button)
        else:
            self.backend.mouse_up(button)
            if button in self._held_buttons:
                self._held_buttons.remove(button)

    def _do_mouse_scroll(self, a: Action) -> None:
        self.backend.mouse_scroll(int(get_param(a, "dx")), int(get_param(a, "dy")))

    # flow
    def _do_delay(self, a: Action) -> None:
        ms = get_param(a, "ms")
        jitter = get_param(a, "jitter_ms")
        if jitter:
            ms += self._rng.uniform(0, jitter)
        self._wait_scaled(ms / 1000)

    def _do_repeat(self, a: Action) -> None:
        base = self._step_path
        count = int(get_param(a, "count"))
        i = 0
        while count == 0 or i < count:
            self._check()
            i += 1
            self._vars["iteration"] = str(i)
            try:
                self._run_list(a.children, (*base, "children"))
            except BreakLoop:
                self._note(f"left by Break in round {i}")
                return
            finally:
                self._set_rounds(i)
            if not a.children:
                self._sleep(0.01)  # an empty infinite loop must stay stoppable and cheap

    def _do_while(self, a: Action) -> None:
        base = self._step_path
        limit = int(get_param(a, "max_iterations"))
        for i in range(1, limit + 1):
            self._check()
            if not self._condition(a):
                self._set_rounds(i - 1)
                return
            self._vars["iteration"] = str(i)
            try:
                self._run_list(a.children, (*base, "children"))
            except BreakLoop:
                self._set_rounds(i)
                self._note(f"left by Break in round {i}")
                return
            if not a.children:
                self._sleep(0.01)  # an empty loop must stay stoppable and cheap
        self._set_rounds(limit)
        self._note("stopped at its limit")

    def _set_rounds(self, rounds: int) -> None:
        entry = self._log_open[-1] if self._log_open else None
        if entry is not None:
            kept = [p for p in entry.detail.split("; ") if p and not p.endswith(("round", "rounds"))]
            entry.detail = "; ".join([f"{rounds} round{'s' if rounds != 1 else ''}", *kept])

    def _do_break(self, a: Action) -> None:
        raise BreakLoop

    def _do_if(self, a: Action) -> None:
        holds = self._condition(a)
        self._note("condition true: then" if holds else "condition false: else")
        branch = "children" if holds else "else_children"
        self._run_list(getattr(a, branch), (*self._step_path, branch))

    def _do_set_variable(self, a: Action) -> None:
        name, mode = get_param(a, "name").strip(), get_param(a, "mode")
        saved = get_param(a, "scope") == "saved" and self._saved is not None
        value = self._text(a, "value")
        current = (self._saved.get(name) if saved else self._vars.get(name)) or ""  # type: ignore[union-attr]
        if mode in ("add", "subtract"):
            delta = to_number(value) * (1 if mode == "add" else -1)
            value = format_number(to_number(current or "0") + delta)
        elif mode == "append":
            value = current + value
        elif mode == "calculate":
            value = format_number(calculate(value))
        elif mode == "expression":  # the value is an expression, not a text with placeholders
            value = script.to_text(self._evaluate(get_param(a, "value")))
        if saved:
            self._saved.set(name, value)  # type: ignore[union-attr]
            self._vars.pop(name, None)  # the saved value is what later steps see
        else:
            self._vars[name] = value
        self._note(f"{name} = {value!r}")

    def _do_stop(self, a: Action) -> None:
        raise StopMacro

    def _do_comment(self, a: Action) -> None:
        pass

    def _do_run_macro(self, a: Action) -> None:
        self._run_macro_named(self._text(a, "name").strip())

    def _run_macro_named(self, name: str) -> None:
        macro = self._find_macro(name)
        if macro is None:
            raise ValueError(f'there is no macro named "{name}"')
        if self._depth >= MAX_MACRO_DEPTH:
            raise RuntimeError(f"macros run each other more than {MAX_MACRO_DEPTH} levels deep")
        problems = catalog.validate_actions(macro.actions)
        if problems:
            raise ValueError(f'macro "{name}": {problems[0]}')
        self._depth += 1
        try:
            self._run_list(macro.actions, (f"macro:{macro.name}",))  # another macro's steps
        finally:
            self._depth -= 1
        self._note(f'ran "{macro.name}" ({len(macro.actions)} steps)')

    def _do_wait_for_window(self, a: Action) -> None:
        wanted = self._text(a, "value").strip().lower()
        polls = max(1, int(get_param(a, "timeout_s") / WINDOW_POLL_S))
        for _ in range(polls + 1):  # paused time does not count: _sleep excludes it
            window = self.backend.active_window()
            if wanted in (window.app if get_param(a, "kind") == "app" else window.title).lower():
                self._vars["timed_out"] = "0"
                self._note(f'found "{window.app if get_param(a, "kind") == "app" else window.title}"')
                return
            self._sleep(WINDOW_POLL_S)
        self._vars["timed_out"] = "1"
        self._note("timed out")
        if get_param(a, "on_timeout") == "error":
            raise TimeoutError(f'no window matching "{wanted}" within {get_param(a, "timeout_s")} s')

    # vision: seeing the screen
    def _grab(self) -> vision.Screen:
        screen = self.ui.grab_screen()
        if screen is None:
            raise ValueError("cannot see the screen without the application window (no GUI)")
        return screen

    def _template(self, a: Action) -> vision.Image:
        path = os.path.expanduser(self._text(a, "image").strip())
        if path not in self._templates:
            self._templates[path] = self.ui.load_image(path)
        return self._templates[path]

    def _find_image(self, template: vision.Image, tolerance: int) -> vision.Match | None:
        return vision.find(self._grab(), template, tolerance)

    def _found(self, match: vision.Match) -> tuple[int, int]:
        x, y = match.center
        self._vars["found_x"], self._vars["found_y"] = str(x), str(y)
        return x, y

    def _do_wait_for_image(self, a: Action) -> None:
        template, tolerance = self._template(a), int(get_param(a, "tolerance"))
        want_gone = get_param(a, "until") == "disappears"
        polls = max(1, int(get_param(a, "timeout_s") / IMAGE_POLL_S))
        for _ in range(polls + 1):  # paused time does not count: _sleep excludes it
            self._check()
            match = self._find_image(template, tolerance)
            if (match is None) == want_gone:
                self._vars["timed_out"] = "0"
                if match is None:
                    self._note("it is gone")
                else:
                    x, y = self._found(match)
                    self._note(f"found at ({x}, {y})")
                return
            self._sleep(IMAGE_POLL_S)
        self._vars["timed_out"] = "1"
        self._note("timed out")
        if get_param(a, "on_timeout") == "error":
            what = "still on the screen" if want_gone else "not on the screen"
            raise TimeoutError(f"the image is {what} after {get_param(a, 'timeout_s')} s")

    def _do_click_image(self, a: Action) -> None:
        template, tolerance = self._template(a), int(get_param(a, "tolerance"))
        polls = max(1, int(get_param(a, "timeout_s") / IMAGE_POLL_S))
        for _ in range(polls + 1):
            self._check()
            match = self._find_image(template, tolerance)
            if match is not None:
                x, y = self._found(match)
                self._vars["timed_out"] = "0"
                x += int(get_param(a, "offset_x"))
                y += int(get_param(a, "offset_y"))
                self.backend.mouse_move(x, y)
                if get_param(a, "move_only"):
                    self._note(f"moved the mouse to ({x}, {y})")
                    return
                button = get_param(a, "button")
                self.backend.mouse_click(button, int(get_param(a, "count")))
                self._note(f"clicked at ({x}, {y})")
                return
            self._sleep(IMAGE_POLL_S)
        self._vars["timed_out"] = "1"
        self._note("timed out")
        if get_param(a, "on_timeout") == "error":
            raise TimeoutError(f"the image did not appear within {get_param(a, 'timeout_s')} s")

    def _do_focus_window(self, a: Action) -> None:
        app, title = self._text(a, "app").strip(), self._text(a, "title").strip()
        if self._simulated_activate(app, title):
            return
        polls = max(1, int(get_param(a, "timeout_s") / WINDOW_POLL_S))
        for _ in range(polls + 1):
            self._check()
            window = self.backend.active_window()
            if _window_matches(window, app, title):
                self._vars["timed_out"] = "0"
                self._note(f'focused "{window.app}"')
                return
            windowcontrol.activate(app, title)
            self._sleep(WINDOW_POLL_S)
        self._vars["timed_out"] = "1"
        self._note("timed out")
        if get_param(a, "on_timeout") == "error":
            wanted = " / ".join(x for x in (app, title) if x)
            raise TimeoutError(f'could not focus "{wanted}" within {get_param(a, "timeout_s")} s')

    def _simulated_activate(self, app: str, title: str) -> bool:
        """In a simulated (debug) run, focusing is only recorded, not really done."""
        record = getattr(self.backend, "activate_window", None)
        if record is None:
            return False
        record(app, title)
        self._vars["timed_out"] = "0"
        self._note("simulated")
        return True

    # profiles and layers
    def _do_switch_profile(self, a: Action) -> None:
        if self._set_profile is None:
            raise ValueError("profiles can only be switched from the running application")
        profile = self._text(a, "profile").strip()
        self._set_profile(profile)
        self._note(f'profile "{profile or "All profiles"}"')

    def _do_switch_layer(self, a: Action) -> None:
        if self._set_layer is None:
            raise ValueError("layers can only be switched from the running application")
        layer = self._text(a, "layer").strip()
        self._set_layer(layer, bool(get_param(a, "one_shot")))
        self._note(f'layer "{layer or "base"}"')

    # talking to the user / the system
    def _do_ask(self, a: Action) -> None:
        answer = self.ui.ask(self._text(a, "prompt"), self._text(a, "default"))
        if answer is None:
            self._note("cancelled")
            raise StopMacro  # cancelled
        self._vars[get_param(a, "variable").strip()] = answer
        self._note("answered")  # not the answer itself: it may be private

    def _do_set_clipboard(self, a: Action) -> None:
        self.ui.set_clipboard(self._text(a, "text"))

    def _do_paste_text(self, a: Action) -> None:
        self._paste(self._text(a, "text"))

    def _paste(self, text: str) -> None:
        previous = self.ui.get_clipboard()
        self.ui.set_clipboard(text)
        try:
            self._do_key_press(Action("key_press", {"keys": paste_shortcut()}))
            self._sleep(0.15)  # the application reads the clipboard asynchronously
        finally:
            self.ui.set_clipboard(previous)

    # scripts
    def _do_script(self, a: Action) -> None:
        statements = [0]

        def tick() -> None:  # every statement and loop round: stop / pause / limits
            statements[0] += 1
            self._check()
            self._count += 1
            if self._count > self._max_steps and self._max_steps < MAX_ACTIONS:
                raise LimitReached(f"step limit of {self._max_steps} reached")
            if self._count > MAX_ACTIONS:
                raise RuntimeError("action limit exceeded")

        def assign(name: str, value) -> None:
            self._vars[name] = script.to_text(value)

        try:
            script.run_script(get_param(a, "code"), self._value, assign, self._script_commands(), tick)
        except script.ScriptError as exc:
            raise ValueError(f"script: {exc}") from None
        finally:
            self._note(f"{statements[0]} statement(s) run")

    def _script_commands(self) -> dict:
        text = script.to_text

        def type_(value, interval=0):
            value = text(value)
            if not interval:
                self.backend.type_text(value)
                return
            for ch in value:
                self._check()
                self.backend.type_text(ch)
                self._wait_scaled(float(interval) / 1000)

        def click(x=None, y=None, button="left", count=1):
            if x is not None and y is not None:
                self.backend.mouse_move(int(x), int(y))
            if button not in catalog.MOUSE_BUTTONS:
                raise script.ScriptError(f"unknown mouse button {button!r}")
            self.backend.mouse_click(button, int(count))

        def move(x, y, relative=False):
            if relative:
                cx, cy = self.backend.mouse_position()
                x, y = cx + x, cy + y
            self.backend.mouse_move(int(x), int(y))

        def ask(prompt, default=""):
            answer = self.ui.ask(text(prompt), text(default))
            if answer is None:
                raise StopMacro
            return answer

        def stop():
            raise StopMacro

        def find(path, tolerance=0):
            template = self.ui.load_image(os.path.expanduser(text(path).strip()))
            match = vision.find(self._grab(), template, int(tolerance))
            if match is None:
                return []
            return list(self._found(match))

        def pixel(x, y):
            screen = self._grab()
            scale = getattr(screen, "scale", 1.0)
            sx, sy = int(int(x) * scale), int(int(y) * scale)
            return vision.format_color(screen.pixel(sx, sy))

        def focus(app="", title=""):
            app, title = text(app).strip(), text(title).strip()
            if self._simulated_activate(app, title):
                return True
            windowcontrol.activate(app, title)
            self._sleep(0.2)  # give the window manager a moment
            return _window_matches(self.backend.active_window(), app, title)

        def layer(name="", one_shot=False):
            if self._set_layer is None:
                raise script.ScriptError("layers can only be switched from the running application")
            self._set_layer(text(name).strip(), bool(one_shot))

        def profile(name=""):
            if self._set_profile is None:
                raise script.ScriptError("profiles can only be switched from the running application")
            self._set_profile(text(name).strip())

        return {
            "type": type_,
            "paste": lambda value: self._paste(text(value)),
            "press": lambda combo: self._do_key_press(Action("key_press", {"keys": text(combo)})),
            "hold": lambda key: self._do_key_down(Action("key_down", {"key": text(key)})),
            "release": lambda key: self._do_key_up(Action("key_up", {"key": text(key)})),
            "wait": lambda ms: self._wait_scaled(max(0.0, float(ms)) / 1000),
            "click": click,
            "move": move,
            "scroll": lambda dy, dx=0: self.backend.mouse_scroll(int(dx), int(dy)),
            "run": lambda name: self._run_macro_named(text(name)),
            "notify": lambda message, title="Keyboard Manager": self.ui.notify(text(title), text(message)),
            "ask": ask,
            "log": lambda message: self._note(text(message)),
            "stop": stop,
            "find": find,
            "pixel": pixel,
            "focus": focus,
            "layer": layer,
            "profile": profile,
        }

    def _do_launch(self, a: Action) -> None:
        target = self._text(a, "target")
        self._note(f"opened {target!r}")
        self.backend.launch(target)

    def _do_notify(self, a: Action) -> None:
        self.ui.notify(self._text(a, "title"), self._text(a, "message"))

    # conditions
    def _condition(self, a: Action) -> bool:
        kind = get_param(a, "kind")
        if kind == "expression":
            result = script.truthy(self._evaluate(get_param(a, "expression")))
        elif kind == "image":
            result = self._find_image(self._template(a), int(get_param(a, "tolerance"))) is not None
        elif kind in ("app", "title"):
            window = self.backend.active_window()
            haystack = window.app if kind == "app" else window.title
            result = self._text(a, "value").strip().lower() in haystack.lower()
        elif kind == "time":
            start, end = catalog.parse_time_range(self._text(a, "value"))
            now = self._now()
            minute = now.hour * 60 + now.minute
            result = start <= minute <= end if start <= end else (minute >= start or minute <= end)
        else:
            result = _compare(
                self._lookup(get_param(a, "variable").strip()) or "",
                get_param(a, "op"),
                self._text(a, "compare_to"),
            )
        return not result if get_param(a, "negate") else result


def paste_shortcut(platform: str | None = None) -> str:
    return "cmd+v" if (platform or sys.platform) == "darwin" else "ctrl+v"


def _compare(left: str, op: str, right: str) -> bool:
    if op == "contains":
        return right.lower() in left.lower()
    if op == "starts with":
        return left.lower().startswith(right.lower())
    if op == "ends with":
        return left.lower().endswith(right.lower())
    if op == "matches":
        try:
            return re.search(right, left) is not None
        except re.error as exc:
            raise ValueError(f"invalid regular expression {right!r}: {exc}") from None
    if op == "is empty":
        return not left.strip()
    try:
        a, b = float(left), float(right)
    except ValueError:
        a, b = left, right  # type: ignore[assignment]
    if op == "==":
        return a == b
    if op == "!=":
        return a != b
    try:
        return {">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b}[op]  # type: ignore[operator]
    except TypeError:
        return False


def _window_matches(window, app: str, title: str) -> bool:
    """Whether the focused window matches the Focus window step's filters."""
    apps = [p.strip().lower() for p in app.split(",") if p.strip()]
    if apps and not any(a in window.app.lower() for a in apps):
        return False
    return not title or title.lower() in window.title.lower()
