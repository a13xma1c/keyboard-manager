"""Debugging a macro: breakpoints, stepping, and input that is only simulated.

The debugger decides *where* to stop; stopping itself is the ordinary pause (held
keys are released, time limits do not run on). Continue resumes until the next
breakpoint, Step stops again before the next step.

While debugging, the user is busy with Keyboard Manager's own window, so real
keystrokes would land there. By default a debug run therefore uses a simulated
backend that records what would have been sent; reading the focused window and
the mouse position still asks the real system.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from keyboardmanager.core.models import Action, ActiveWindow

MAX_SIMULATED = 500  # entries of simulated input kept for the live view


class Debugger:
    def __init__(self, breakpoints: set[tuple] | None = None, stop_at_start: bool = False) -> None:
        self.breakpoints = set(breakpoints or ())
        self.stepping = stop_at_start
        self.pause: Callable[[], Any] = lambda: None  # set by the service (its pause())
        self.stopped_at: tuple | None = None           # the step it is waiting before
        self.hits = 0
        self._lock = threading.Lock()

    def before_step(self, path: tuple, action: Action) -> bool:
        """Called before every step (worker thread). True when it stops there."""
        with self._lock:
            stop = self.stepping or path in self.breakpoints
            if stop:
                self.stopped_at = path
                self.hits += 1
        if stop:
            self.pause()
        return stop

    def continue_(self) -> None:
        with self._lock:
            self.stepping = False
            self.stopped_at = None

    def step(self) -> None:
        with self._lock:
            self.stepping = True
            self.stopped_at = None


class SimulatedBackend:
    """Records input instead of sending it; reads the real window and mouse."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self._pos: tuple[int, int] | None = None
        self.sent: list[tuple[float, str]] = []
        self._started = time.monotonic()
        self._lock = threading.Lock()

    def _record(self, text: str) -> None:
        with self._lock:
            self.sent.append((time.monotonic() - self._started, text))
            del self.sent[:-MAX_SIMULATED]

    def recent(self, n: int = 50) -> list[tuple[float, str]]:
        with self._lock:
            return list(self.sent[-n:])

    def key_down(self, key: str) -> None: self._record(f"key down {key}")
    def key_up(self, key: str) -> None: self._record(f"key up {key}")
    def type_text(self, text: str) -> None: self._record(f"type {text!r}")
    def mouse_down(self, button: str) -> None: self._record(f"{button} button down")
    def mouse_up(self, button: str) -> None: self._record(f"{button} button up")
    def mouse_click(self, button: str, count: int) -> None: self._record(f"{button} click x{count}")
    def mouse_scroll(self, dx: int, dy: int) -> None: self._record(f"scroll {dx}, {dy}")
    def launch(self, target: str) -> None: self._record(f"open {target!r}")

    def activate_window(self, app: str, title: str) -> bool:
        self._record(f"focus window {' / '.join(x for x in (app, title) if x)!r}")
        return True

    def mouse_move(self, x: int, y: int) -> None:
        self._pos = (x, y)
        self._record(f"move mouse to ({x}, {y})")

    def mouse_position(self) -> tuple[int, int]:
        return self._pos if self._pos is not None else self._real.mouse_position()

    def active_window(self) -> ActiveWindow:
        return self._real.active_window()
