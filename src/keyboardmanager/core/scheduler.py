"""Runs scheduled macros at their time (no Qt; the clock is injectable for tests).

Rules:

* Only occurrences after the scheduler started count: runs missed while the
  application was closed are never caught up.
* An occurrence that cannot start within ``GRACE_S`` seconds of its time (another
  macro kept running, or the computer was asleep) is skipped and recorded as
  ``skipped`` in the history. Many missed interval occurrences count as one.
* The macro's application / window filters apply as for hotkeys: if they do not
  match the focused window at that moment, the occurrence is silently passed over.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable

from keyboardmanager.core.models import Macro
from keyboardmanager.core.schedule import Schedule

GRACE_S = 60.0
TICK_S = 1.0


@dataclass
class _Entry:
    macro: Macro
    schedule: Schedule
    next_due: float | None
    busy: bool = False  # the current occurrence was attempted while another macro ran


class Scheduler:
    def __init__(
        self,
        run: Callable[[Macro, str], bool],
        matches: Callable[[Macro], bool],
        record_skipped: Callable[[Macro, str, str], None],
        on_change: Callable[[], None] = lambda: None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._run = run
        self._matches = matches
        self._record_skipped = record_skipped
        self._on_change = on_change
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[int, _Entry] = {}
        self._thread: threading.Thread | None = None
        self._quit = threading.Event()

    # ----------------------------------------------------------------- config
    def update(self, macros: list[Macro]) -> None:
        """Set the macros to schedule (already filtered to enabled + scheduled).

        Unchanged schedules keep their next run (an interval keeps its phase when
        another macro is saved); new or edited ones start counting from now.
        """
        now = self._clock()
        with self._lock:
            entries: dict[int, _Entry] = {}
            for macro in macros:
                if macro.id is None or not macro.schedule.active:
                    continue
                old = self._entries.get(macro.id)
                if old is not None and old.schedule == macro.schedule:
                    old.macro = macro  # pick up edited steps / filters
                    entries[macro.id] = old
                else:
                    entries[macro.id] = _Entry(macro, macro.schedule, _first_due(macro.schedule, now))
            self._entries = entries

    def next_run(self, macro_id: int) -> float | None:
        with self._lock:
            entry = self._entries.get(macro_id)
            return entry.next_due if entry else None

    def upcoming(self) -> list[tuple[float, Macro]]:
        """``(timestamp, macro)`` of every pending run, soonest first."""
        with self._lock:
            pending = [(e.next_due, e.macro) for e in self._entries.values() if e.next_due is not None]
        return sorted(pending, key=lambda p: p[0])

    # -------------------------------------------------------------- execution
    def tick(self, now: float | None = None) -> None:
        """Start whatever is due. Called every second by the thread (or by tests)."""
        now = self._clock() if now is None else now
        changed = False
        with self._lock:
            entries = list(self._entries.values())
        for entry in entries:
            due = entry.next_due
            if due is None or now < due:
                continue
            trigger = f"schedule: {entry.schedule.describe()}"
            if now - due > GRACE_S:
                reason = (
                    "another macro was still running" if entry.busy
                    else "missed its time (computer asleep or application busy)"
                )
                self._record_skipped(entry.macro, trigger, f"Skipped: {reason}")
            elif not self._matches(entry.macro):
                pass  # like a hotkey pressed in another application: nothing happens
            elif not self._run(entry.macro, trigger):
                entry.busy = True  # retry on the next tick until the grace period ends
                continue
            with self._lock:
                entry.busy = False
                entry.next_due = _following_due(entry.schedule, due, now)
            changed = True
        if changed:
            self._on_change()

    # ------------------------------------------------------------------ thread
    def start(self) -> None:
        if self._thread is not None:
            return
        self._quit.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._quit.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(2)

    def _loop(self) -> None:
        while not self._quit.wait(TICK_S):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a bad entry must not kill scheduling
                pass


def _first_due(schedule: Schedule, now: float) -> float | None:
    if schedule.kind == "interval":
        return now + schedule.minutes * 60
    return schedule.next_after(now)


def _following_due(schedule: Schedule, due: float, now: float) -> float | None:
    """Next occurrence after ``due`` was handled at ``now`` (skipping any already past)."""
    if schedule.kind == "interval":
        step = schedule.minutes * 60
        return due + max(1, math.floor((now - due) / step) + 1) * step
    return schedule.next_after(max(now, due))
