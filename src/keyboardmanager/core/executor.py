"""Macro execution engine (no Qt; fully testable with a fake backend)."""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from keyboardmanager.core import actions as catalog
from keyboardmanager.core import keys
from keyboardmanager.core.actions import get_param
from keyboardmanager.core.backend import InputBackend
from keyboardmanager.core.models import Action, Macro

MAX_ACTIONS = 1_000_000  # hard safety net against runaway macros


class StopMacro(Exception):
    """Raised internally to unwind when the user stops or a 'stop' action runs."""


@dataclass
class ExecutionResult:
    status: str  # success | stopped | error
    actions_run: int = 0
    duration_ms: int = 0
    message: str = ""
    variables: dict[str, str] = field(default_factory=dict)


class MacroExecutor:
    def __init__(
        self,
        backend: InputBackend,
        now: Callable[[], datetime] = datetime.now,
        rng: random.Random | None = None,
    ) -> None:
        self.backend = backend
        self._now = now
        self._rng = rng or random.Random()
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
        started = time.monotonic()
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
            self._run_list(macro.actions)
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

    def _check(self) -> None:
        """Pause point: raise if stopped, block while paused."""
        if self._stop:
            raise StopMacro
        if self._paused:
            self._wait_while_paused()

    def _wait_while_paused(self) -> None:
        keys_held, buttons_held = list(self._held), list(self._held_buttons)
        self._release_all()  # hand the keyboard and mouse back to the user
        with self._cond:
            while self._paused and not self._stop:
                self._cond.wait()
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

    def _run_list(self, actions: list[Action]) -> None:
        for action in actions:
            self._check()
            self._count += 1
            if self._count > MAX_ACTIONS:
                raise RuntimeError("action limit exceeded")
            getattr(self, f"_do_{action.type}")(action)

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

    def _do_type_text(self, a: Action) -> None:
        text = get_param(a, "text")
        interval = get_param(a, "interval_ms") / 1000
        if interval <= 0:
            self.backend.type_text(text)
            return
        for ch in text:
            self._check()
            self.backend.type_text(ch)
            self._sleep(interval)

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
        self._sleep(ms / 1000)

    def _do_repeat(self, a: Action) -> None:
        count = int(get_param(a, "count"))
        i = 0
        while count == 0 or i < count:
            self._check()
            i += 1
            self._vars["iteration"] = str(i)
            self._run_list(a.children)
            if not a.children:
                self._sleep(0.01)  # an empty infinite loop must stay stoppable and cheap

    def _do_if(self, a: Action) -> None:
        self._run_list(a.children if self._condition(a) else a.else_children)

    def _do_set_variable(self, a: Action) -> None:
        name, value = get_param(a, "name").strip(), get_param(a, "value")
        if get_param(a, "mode") == "add":
            total = _number(self._vars.get(name, "0")) + _number(value)
            value = f"{total:g}"
        self._vars[name] = value

    def _do_stop(self, a: Action) -> None:
        raise StopMacro

    # conditions
    def _condition(self, a: Action) -> bool:
        kind = get_param(a, "kind")
        if kind in ("app", "title"):
            window = self.backend.active_window()
            haystack = window.app if kind == "app" else window.title
            result = get_param(a, "value").strip().lower() in haystack.lower()
        elif kind == "time":
            start, end = catalog.parse_time_range(get_param(a, "value"))
            now = self._now()
            minute = now.hour * 60 + now.minute
            result = start <= minute <= end if start <= end else (minute >= start or minute <= end)
        else:
            result = _compare(
                self._vars.get(get_param(a, "variable").strip(), ""),
                get_param(a, "op"),
                get_param(a, "compare_to"),
            )
        return not result if get_param(a, "negate") else result


def _number(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"{text!r} is not a number") from None


def _compare(left: str, op: str, right: str) -> bool:
    if op == "contains":
        return right.lower() in left.lower()
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
