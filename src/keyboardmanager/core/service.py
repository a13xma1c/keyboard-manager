"""Ties together storage, hotkeys and execution (no Qt; UI listens via callbacks)."""

from __future__ import annotations

import threading
import time
from typing import Callable

from keyboardmanager.core import keys
from keyboardmanager.core.backend import InputBackend
from keyboardmanager.core.database import Database
from keyboardmanager.core.executor import ExecutionResult, MacroExecutor
from keyboardmanager.core.hotkeys import HotkeyBackend
from keyboardmanager.core.models import Macro
from keyboardmanager.core.scheduler import Scheduler

DEFAULT_STOP_HOTKEY = "ctrl+alt+f12"
STOP_HOTKEY_SETTING = "stop_hotkey"
DEFAULT_PAUSE_HOTKEY = "ctrl+alt+f11"
PAUSE_HOTKEY_SETTING = "pause_hotkey"

# Event names passed to the listener: "started", "finished", "paused", "resumed"
# (payload: the macro), "hotkeys" (registration / schedules reloaded) and
# "schedule" (a scheduled occurrence was handled, so the next run times changed) and
# "skipped" (payload: (macro, reason); a scheduled run could not start in time).
Listener = Callable[[str, object], None]


class MacroService:
    def __init__(
        self,
        db: Database,
        backend: InputBackend,
        hotkeys: HotkeyBackend | None = None,
        listener: Listener | None = None,
        clock: Callable[[], float] = time.time,
        scheduler_thread: bool = True,
    ) -> None:
        self.db = db
        self.backend = backend
        self.hotkeys = hotkeys
        self.listener = listener or (lambda event, payload: None)
        self.hotkeys_enabled = True
        self.schedules_enabled = True
        self.clock = clock  # the scheduler's notion of "now" (injectable for tests)
        self.scheduler = Scheduler(
            run=lambda macro, trigger: self.run(macro, trigger=trigger),
            matches=lambda macro: macro.matches(self.backend.active_window()),
            record_skipped=self._record_skipped,
            on_change=lambda: self.listener("schedule", None),
            clock=clock,
        )
        self._scheduler_thread = scheduler_thread
        self.hotkey_failures: dict[str, str] = {}
        self._lock = threading.Lock()
        # Held while emitting run events so listeners always see them in order:
        # "started", then any "paused"/"resumed", then "finished".
        self._events_lock = threading.RLock()
        self._announced = False  # "started" has been emitted for the current run
        self._executor: MacroExecutor | None = None
        self._running: Macro | None = None
        self._paused = False
        self._by_hotkey: dict[str, list[Macro]] = {}
        self.db.mark_interrupted()

    # ------------------------------------------------------------------ hotkeys
    @property
    def stop_hotkey(self) -> str:
        return self.db.get_setting(STOP_HOTKEY_SETTING, DEFAULT_STOP_HOTKEY)

    def set_stop_hotkey(self, hotkey: str) -> None:
        hotkey = keys.normalize_hotkey(hotkey)
        if hotkey == self.pause_hotkey:
            raise ValueError(f"{hotkey} is already the pause hotkey.")
        self.db.set_setting(STOP_HOTKEY_SETTING, hotkey)
        self.reload()

    @property
    def pause_hotkey(self) -> str:
        """Global hotkey toggling pause/resume of the running macro ("" = none)."""
        return self.db.get_setting(PAUSE_HOTKEY_SETTING, DEFAULT_PAUSE_HOTKEY)

    def set_pause_hotkey(self, hotkey: str) -> None:
        hotkey = keys.normalize_hotkey(hotkey) if hotkey.strip() else ""
        if hotkey and hotkey == self.stop_hotkey:
            raise ValueError(f"{hotkey} is already the stop hotkey.")
        self.db.set_setting(PAUSE_HOTKEY_SETTING, hotkey)
        self.reload()

    def reserved_hotkeys(self) -> dict[str, str]:
        """Hotkeys used by the application itself: ``{hotkey: purpose}``."""
        reserved = {self.stop_hotkey: "stop"}
        if self.pause_hotkey:
            reserved[self.pause_hotkey] = "pause"
        return reserved

    def reload(self) -> dict[str, str]:
        """Re-read macros from the database, re-register all global hotkeys and
        update the schedules."""
        self._by_hotkey = {}
        macros = self.db.list_macros()
        if self.hotkeys_enabled:
            for macro in macros:
                if macro.enabled and macro.hotkey:
                    self._by_hotkey.setdefault(macro.hotkey, []).append(macro)
        self.scheduler.update(
            [m for m in macros if m.enabled and m.schedule.active] if self.schedules_enabled else []
        )
        if self._scheduler_thread:
            self.scheduler.start()
        self.hotkey_failures = {}
        if self.hotkeys is not None:
            mapping: dict[str, Callable[[], None]] = {
                hk: (lambda hk=hk: self.on_hotkey(hk)) for hk in self._by_hotkey
            }
            try:
                mapping[self.stop_hotkey] = self.stop
            except ValueError:
                pass
            conflicts: dict[str, str] = {}
            pause_hotkey = self.pause_hotkey
            if pause_hotkey in self._by_hotkey:
                # Never silently take over a hotkey a macro already uses (e.g. a macro
                # created before the pause hotkey existed): the macro keeps it.
                owner = self._by_hotkey[pause_hotkey][0].name
                conflicts[pause_hotkey] = f'pause hotkey not registered: already used by "{owner}"'
            elif pause_hotkey and pause_hotkey in mapping:
                conflicts[pause_hotkey] = "pause hotkey not registered: same as the stop hotkey"
            elif pause_hotkey:
                mapping[pause_hotkey] = self.toggle_pause
            try:
                self.hotkey_failures = {**conflicts, **self.hotkeys.start(mapping)}
            except Exception as exc:  # noqa: BLE001 - e.g. missing OS permission
                self.hotkey_failures = {"*": f"{type(exc).__name__}: {exc}"}
        self.listener("hotkeys", self.hotkey_failures)
        return self.hotkey_failures

    def set_hotkeys_enabled(self, enabled: bool) -> None:
        self.hotkeys_enabled = enabled
        self.reload()

    # ---------------------------------------------------------------- schedules
    def set_schedules_enabled(self, enabled: bool) -> None:
        self.schedules_enabled = enabled
        self.reload()

    def next_run(self, macro_id: int) -> float | None:
        """Timestamp of the macro's next scheduled run, or ``None``."""
        return self.scheduler.next_run(macro_id)

    def _record_skipped(self, macro: Macro, trigger: str, message: str) -> None:
        self.db.add_skipped(macro, trigger, message)
        self.listener("skipped", (macro, message))

    def on_hotkey(self, hotkey: str) -> None:
        """Called on the hotkey thread when a registered combination is pressed."""
        candidates = self._by_hotkey.get(hotkey, [])
        if not candidates:
            return
        running = self._running
        if running is not None and any(running.id == c.id for c in candidates):
            self.stop()  # pressing the hotkey of a running macro toggles it off
            return
        window = self.backend.active_window()
        matching = [m for m in candidates if m.matches(window)]
        # an application-specific macro wins over a global one
        matching.sort(key=lambda m: not m.is_app_specific)
        if matching:
            self.run(matching[0], trigger=f"hotkey {hotkey}")

    # ---------------------------------------------------------------- execution
    @property
    def running_macro(self) -> Macro | None:
        return self._running

    @property
    def paused(self) -> bool:
        """True while the running macro is paused (its start countdown included)."""
        return self._paused

    def run(
        self,
        macro: Macro,
        trigger: str = "manual",
        start_delay: float = 0,
        blocking: bool = False,
    ) -> bool:
        """Start ``macro`` on a worker thread. Returns False if another is running."""
        with self._lock:
            if self._running is not None:
                return False
            self._running = macro
            self._paused = False
            self._announced = False
            # Created here, not on the worker, so stop() / pause() issued right after
            # run() returns always reach it.
            self._executor = MacroExecutor(self.backend)
            executor = self._executor
        thread = threading.Thread(
            target=self._worker, args=(executor, macro, trigger, start_delay), daemon=True, name="macro"
        )
        thread.start()
        if blocking:
            thread.join()
        return True

    def stop(self) -> None:
        """Stop the running macro (also when it is paused)."""
        with self._lock:
            executor = self._executor
        if executor is not None:
            executor.stop()

    def pause(self) -> bool:
        """Pause the running macro. Returns False if nothing is running or already paused."""
        return self._set_paused(True)

    def resume(self) -> bool:
        """Resume a paused macro. Returns False if nothing is paused."""
        return self._set_paused(False)

    def _set_paused(self, paused: bool) -> bool:
        with self._events_lock:
            with self._lock:
                if self._executor is None or self._paused == paused:
                    return False
                self._paused = paused
                if paused:
                    self._executor.pause()
                else:
                    self._executor.resume()
                macro, announce = self._running, self._announced
            # before "started" the worker announces the pause itself (see _worker)
            if announce:
                self.listener("paused" if paused else "resumed", macro)
        return True

    def toggle_pause(self) -> None:
        """Target of the pause hotkey: pause if running, resume if paused."""
        if not self.pause():
            self.resume()

    def _worker(self, executor: MacroExecutor, macro: Macro, trigger: str, start_delay: float) -> None:
        history_id = self.db.start_history(macro, trigger)
        with self._events_lock:
            self.listener("started", macro)
            with self._lock:
                self._announced = True
                paused_early = self._paused
            if paused_early:  # pause() was called before "started" went out
                self.listener("paused", macro)
        result: ExecutionResult
        try:
            result = executor.run(macro, start_delay=start_delay)
        except Exception as exc:  # noqa: BLE001
            result = ExecutionResult("error", message=f"{type(exc).__name__}: {exc}")
        with self._events_lock:
            try:
                # history first: once running_macro is None the run is fully recorded
                self.db.finish_history(
                    history_id, result.status, result.duration_ms, result.actions_run, result.message
                )
            finally:  # even if the database fails, the service must not stay "running"
                with self._lock:
                    self._executor, self._running, self._paused = None, None, False
                    self._announced = False
            self.listener("finished", (macro, result))

    def shutdown(self) -> None:
        self.scheduler.stop()
        self.stop()
        if self.hotkeys is not None:
            self.hotkeys.stop()
