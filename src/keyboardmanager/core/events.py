"""Event triggers: run a macro when something happens (no Qt, injectable for tests).

Supported events:

* ``app_start``    - Keyboard Manager has started.
* ``focus_gained`` - the focused window started matching the macro's application /
  window-title filters (the user switched *to* that application).
* ``focus_lost``   - the focused window stopped matching them.

Focus events need a filter: a macro that matches every window can never gain or
lose focus. The focused window is polled (0.4 s) only while focus-event macros
exist, and like hotkeys, events do nothing while another macro is running.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from keyboardmanager.core.models import ActiveWindow, Macro

POLL_INTERVAL_S = 0.4

EVENTS: dict[str, str] = {
    "": "Not on an event",
    "app_start": "When Keyboard Manager starts",
    "focus_gained": "When its application gains focus",
    "focus_lost": "When its application loses focus",
}
FOCUS_EVENTS = ("focus_gained", "focus_lost")


def validate_event(event: str, macro: Macro) -> None:
    """Raise ``ValueError`` when ``event`` is unknown or unusable for ``macro``."""
    if event not in EVENTS:
        raise ValueError(f"unknown event {event!r}")
    if event in FOCUS_EVENTS and not macro.is_app_specific:
        raise ValueError(
            "a focus event needs an application or window-title filter "
            "(without one the macro matches every window and would never fire)"
        )


def describe_event(event: str) -> str:
    return EVENTS.get(event, event)


class EventWatcher:
    """Runs macros on focus changes; polls only while there is something to watch."""

    def __init__(
        self,
        active_window: Callable[[], ActiveWindow],
        run: Callable[[Macro, str], bool],
        interval: float = POLL_INTERVAL_S,
    ) -> None:
        self._active_window = active_window
        self._run = run
        self._interval = interval
        self._lock = threading.Lock()
        self._macros: list[Macro] = []
        self._last: ActiveWindow | None = None
        self._thread: threading.Thread | None = None
        self._quit = threading.Event()

    def update(self, macros: list[Macro], thread: bool = True) -> None:
        """Set the focus-event macros to watch; starts or stops the polling thread."""
        with self._lock:
            self._macros = [m for m in macros if m.event in FOCUS_EVENTS]
            needed = bool(self._macros)
        if thread and needed:
            self.start()
        elif not needed:
            self.stop()

    def check(self) -> None:
        """One poll: compare the focused window with the previous one."""
        window = self._active_window()
        with self._lock:
            previous, self._last = self._last, window
            macros = list(self._macros)
        if previous is None or window == previous:
            return
        for macro in macros:
            was, now = macro.matches(previous), macro.matches(window)
            if now and not was and macro.event == "focus_gained":
                self._run(macro, f"event: {describe_event('focus_gained')}")
            elif was and not now and macro.event == "focus_lost":
                self._run(macro, f"event: {describe_event('focus_lost')}")

    # ------------------------------------------------------------------ thread
    def start(self) -> None:
        if self._thread is not None:
            return
        self._quit.clear()
        self._last = None  # a fresh watcher must not see the current window as a change
        self._thread = threading.Thread(target=self._loop, daemon=True, name="events")
        self._thread.start()

    def stop(self) -> None:
        self._quit.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(2)

    def _loop(self) -> None:
        while not self._quit.wait(self._interval):
            try:
                self.check()
            except Exception:  # noqa: BLE001 - a lookup failure must not kill watching
                time.sleep(1)
