"""Ties together storage, hotkeys and execution (no Qt; UI listens via callbacks)."""

from __future__ import annotations

import os
import threading
import time
from collections import deque
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from keyboardmanager.core import keys
from keyboardmanager.core.backend import InputBackend
from keyboardmanager.core.database import Database
from keyboardmanager.core.executor import ExecutionResult, MacroExecutor
from keyboardmanager.core import remap as remapping
from keyboardmanager.core.remap import create_remap_backend
from keyboardmanager.core.actions import new_action
from keyboardmanager.core.events import EventWatcher, describe_event
from keyboardmanager.core.expander import TextExpander
from keyboardmanager.core.debugger import Debugger, SimulatedBackend
from keyboardmanager.core.interaction import HeadlessInterface, UserInterface
from keyboardmanager.core.triggers import TriggerEngine, TriggerRule, validate_rule
from keyboardmanager.core.limits import MANUAL_TRIGGERS
from keyboardmanager.core.context import (
    ENABLED_SETTING as CONTEXTS_ENABLED_SETTING,
    SETTING as CONTEXT_RULES_SETTING,
    ContextRule,
    ContextWatcher,
    rules_from_json,
    rules_to_json,
    validate_rule as validate_context_rule,
)
from keyboardmanager.core.organize import FOLDERS_SETTING, FolderStore, validate_speed
from keyboardmanager.core.profiles import (
    ACTIVE_PROFILE_SETTING,
    PROFILES_SETTING,
    ProfileStore,
    in_profile,
    normalize_profile,
)
from keyboardmanager.core.variables import SavedVariables
from keyboardmanager.core.hotkeys import HotkeyBackend, SequenceMatcher
from keyboardmanager.core.models import Macro
from keyboardmanager.core.scheduler import Scheduler
from keyboardmanager.core import remote as remote_mod
from keyboardmanager.core import sync as sync_mod
from keyboardmanager.core import team as team_mod

DEFAULT_STOP_HOTKEY = "ctrl+alt+f12"
STOP_HOTKEY_SETTING = "stop_hotkey"
DEFAULT_PAUSE_HOTKEY = "ctrl+alt+f11"
PAUSE_HOTKEY_SETTING = "pause_hotkey"
PLAYBACK_SPEED_SETTING = "playback_speed"
MAX_QUEUE = 20
LOG_STEPS_SETTING = "log_steps"
DEFAULT_SYNC_INTERVAL_MIN = 5
SYNC_DEBOUNCE_S = 3.0  # a requested sync waits this long for more changes


@dataclass
class SyncOutcome:
    """What one "sync now" did: the sync folder and every team library."""

    report: "sync_mod.SyncReport | None" = None
    error: str = ""
    teams: dict = field(default_factory=dict)        # team name -> PullReport
    team_errors: dict = field(default_factory=dict)  # team name -> message
    time: float = 0.0

    @property
    def failed(self) -> bool:
        return bool(self.error or self.team_errors)

    @property
    def changed_here(self) -> bool:
        return bool((self.report and self.report.changed_here) or any(r.changed for r in self.teams.values()))

    def summary(self) -> str:
        parts = []
        if self.error:
            parts.append(f"Sync failed: {self.error}")
        elif self.report is not None:
            parts.append(f"Sync: {self.report.summary()}")
        for name, report in self.teams.items():
            parts.append(f'Team "{name}": {report.summary()}')
        for name, message in self.team_errors.items():
            parts.append(f'Team "{name}" failed: {message}')
        return "; ".join(parts) if parts else "Nothing to sync (no folder or team library set up)"

# Event names passed to the listener: "started", "finished", "paused", "resumed"
# (payload: the macro), "hotkeys" (registration / schedules reloaded) and
# "schedule" (a scheduled occurrence was handled, so the next run times changed) and
# "skipped" (payload: (macro, reason); a scheduled run could not start in time).
Listener = Callable[[str, object], None]


@dataclass
class _Job:
    """A run: started, or waiting in the queue."""

    macro: Macro
    trigger: str
    start_delay: float
    chain: int = 0
    debugger: Debugger | None = None
    simulate: bool = False
    queued_at: float = 0.0


class MacroService:
    def __init__(
        self,
        db: Database,
        backend: InputBackend,
        hotkeys: HotkeyBackend | None = None,
        listener: Listener | None = None,
        clock: Callable[[], float] = time.time,
        scheduler_thread: bool = True,
        remapper: remapping.RemapBackend | None = None,
    ) -> None:
        """``scheduler_thread=False`` (tests) starts no background threads or input
        listeners; ``remapper`` replaces the platform's key remapping backend."""
        self.db = db
        self.backend = backend
        self.hotkeys = hotkeys
        self.listener = listener or (lambda event, payload: None)
        self.hotkeys_enabled = True
        self.autorun_enabled = True  # schedules and event triggers
        self.clock = clock  # the scheduler's notion of "now" (injectable for tests)
        self.scheduler = Scheduler(
            run=lambda macro, trigger: self.run(macro, trigger=trigger),
            matches=lambda macro: macro.matches(self.backend.active_window()),
            record_skipped=self._record_skipped,
            on_change=lambda: self.listener("schedule", None),
            clock=clock,
        )
        self._scheduler_thread = scheduler_thread
        self.events = EventWatcher(
            active_window=self.backend.active_window,
            run=lambda macro, trigger: self.run(macro, trigger=trigger),
        )
        self._sequences = SequenceMatcher(clock=clock)
        self.ui: UserInterface = HeadlessInterface()  # the GUI installs its own
        try:
            self._playback_speed = validate_speed(float(db.get_setting(PLAYBACK_SPEED_SETTING, "1")))
        except ValueError:
            self._playback_speed = 1.0
        self.folders = FolderStore(
            load=lambda: db.get_setting(FOLDERS_SETTING, ""),
            save=lambda text: db.set_setting(FOLDERS_SETTING, text),
        )
        self.profiles = ProfileStore(
            load=lambda: db.get_setting(PROFILES_SETTING, ""),
            save=lambda text: db.set_setting(PROFILES_SETTING, text),
        )
        self.contexts = ContextWatcher(
            active_window=lambda: self.backend.active_window(), apply=self._apply_context,
        )
        self._context_profile: str | None = None  # a context rule's override (None = none)
        self._context_layer: str | None = None
        self._layer = ""  # the keymap layer chosen by hand / a macro ("" = base)
        self._layer_one_shot = False
        self._reload_lock = threading.RLock()    # reload() may come from several threads
        self._sync_lock = threading.Lock()       # one sync at a time
        self._sync_wake = threading.Event()      # "sync soon" (after a change)
        self._sync_quit = threading.Event()
        self._sync_thread: threading.Thread | None = None
        self.remote: remote_mod.RemoteServer | None = None
        self.remote_error = ""
        self.saved_variables = SavedVariables(
            load=lambda: db.get_setting(SavedVariables.SETTING, ""),
            save=lambda text: db.set_setting(SavedVariables.SETTING, text),
        )
        self.expander = TextExpander(on_match=self.on_abbreviation)
        self.triggers = TriggerEngine(
            fire=self._fire_rule, evaluate=self.evaluate, clipboard=lambda: self.ui.get_clipboard(),
        )
        self._by_abbreviation: dict[str, list[Macro]] = {}
        self.remapper = remapper or create_remap_backend(db.get_setting, db.set_setting)
        self.remap_failures: dict[str, str] = {}
        recover = getattr(self.remapper, "recover", None)
        if recover is not None:  # undo a mapping left behind by a crash
            try:
                recover()
            except Exception:  # noqa: BLE001
                pass
        self.hotkey_failures: dict[str, str] = {}
        self._lock = threading.Lock()
        # Held while emitting run events so listeners always see them in order:
        # "started", then any "paused"/"resumed", then "finished".
        self._events_lock = threading.RLock()
        self._announced = False  # "started" has been emitted for the current run
        self._executor: MacroExecutor | None = None
        self._running: Macro | None = None
        self._job: _Job | None = None
        self._queue: deque[_Job] = deque()
        self._worker: threading.Thread | None = None
        self._paused = False
        self._closed = False  # shutdown() began: nothing new may start
        self._last_state: dict | None = None
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

    def macro_active(self, macro: Macro) -> bool:
        """Enabled and in the active profile (what hotkeys, schedules etc. require)."""
        return macro.enabled and in_profile(macro.profiles, self.active_profile)

    def _on_layer(self, macro: Macro) -> bool:
        """Whether the macro's hotkey / abbreviation works on the active layer."""
        return not macro.layer or macro.layer.lower() == self.active_layer.lower()

    def reload(self) -> dict[str, str]:
        """Re-read macros from the database, re-register all global hotkeys and
        update the schedules. Any thread may call it (sync, context rules, macros
        switching layers); calls are serialised."""
        with self._reload_lock:
            return self._reload()

    def _reload(self) -> dict[str, str]:
        self._by_hotkey = {}
        self._by_abbreviation = {}
        macros = [m for m in self.db.list_macros() if self.macro_active(m)]
        if self.hotkeys_enabled:  # the same switch covers abbreviations (typed triggers)
            for macro in macros:
                if macro.hotkey and self._on_layer(macro):
                    self._by_hotkey.setdefault(macro.hotkey, []).append(macro)
                if macro.abbreviation and self._on_layer(macro):
                    self._by_abbreviation.setdefault(macro.abbreviation, []).append(macro)
        self.expander.update(set(self._by_abbreviation), listen=self._scheduler_thread)
        self.scheduler.update(
            [m for m in macros if m.schedule.active] if self.autorun_enabled else []
        )
        if self._scheduler_thread:
            self.scheduler.start()
        self.events.update(
            [m for m in macros if m.event] if self.autorun_enabled else [],
            thread=self._scheduler_thread,
        )
        self._reload_triggers()
        self.contexts.update(self.context_rules if self.contexts_enabled else [],
                             thread=self._scheduler_thread)
        self.hotkey_failures = {}
        if self.hotkeys is not None:
            # Register single chords; the sequence matcher assembles multi-chord
            # hotkeys such as "ctrl+k ctrl+s" from them.
            chords = self._sequences.configure(self._by_hotkey)
            mapping: dict[str, Callable[[], None]] = {
                chord: (lambda chord=chord: self.on_chord(chord)) for chord in chords
            }
            conflicts: dict[str, str] = {}
            reserved = [(self.stop_hotkey, self.stop, "stop")]
            if self.pause_hotkey:
                reserved.append((self.pause_hotkey, self.toggle_pause, "pause"))
            seen: dict[str, str] = {}
            for chord, callback, purpose in reserved:
                if chord in seen:
                    conflicts[chord] = f"{purpose} hotkey not registered: same as the {seen[chord]} hotkey"
                    continue
                for sequence, owners in self._by_hotkey.items():
                    if chord in sequence.split():
                        # Never silently take over a chord a macro already uses (e.g. a
                        # macro created before this setting existed): the macro keeps it.
                        conflicts[chord] = (
                            f'{purpose} hotkey not registered: already used by "{owners[0].name}"'
                        )
                        break
                else:
                    mapping[chord] = callback
                    seen[chord] = purpose
            try:
                self.hotkey_failures = {**conflicts, **self._sequence_failures(self.hotkeys.start(mapping))}
            except Exception as exc:  # noqa: BLE001 - e.g. missing OS permission
                self.hotkey_failures = {"*": f"{type(exc).__name__}: {exc}"}
        self.listener("hotkeys", self.hotkey_failures)
        return self.hotkey_failures

    def _sequence_failures(self, chord_failures: dict[str, str]) -> dict[str, str]:
        """Report a failed chord against every hotkey sequence that needs it."""
        failures: dict[str, str] = {}
        for chord, message in chord_failures.items():
            affected = [s for s in self._by_hotkey if chord in s.split()]
            for sequence in affected:
                failures[sequence] = message
            if not affected:
                failures[chord] = message
        return failures

    def on_abbreviation(self, abbreviation: str) -> None:
        """Called (on the listener thread) when an abbreviation has just been typed."""
        window = self.backend.active_window()
        matching = [m for m in self._by_abbreviation.get(abbreviation, []) if m.matches(window)]
        matching.sort(key=lambda m: (not m.layer, not m.is_app_specific))  # as for hotkeys
        if matching:
            self._consume_one_shot()
            self.run(expansion_macro(matching[0], abbreviation), trigger=f"abbreviation {abbreviation}")

    def on_chord(self, chord: str) -> None:
        """Called on the hotkey thread whenever a registered chord is pressed."""
        sequence = self._sequences.press(chord)
        if sequence is not None:
            self.on_hotkey(sequence)

    def set_hotkeys_enabled(self, enabled: bool) -> None:
        self.hotkeys_enabled = enabled
        self.reload()

    # ----------------------------------------------------- schedules and events
    def set_autorun_enabled(self, enabled: bool) -> None:
        """Switch everything that runs by itself (schedules and events) on or off."""
        self.autorun_enabled = enabled
        self.reload()

    # ------------------------------------------------------ profiles and layers
    @property
    def active_profile(self) -> str:
        """The profile in effect now: a context rule's, else the one chosen by hand."""
        if self._context_profile is not None:
            return self._context_profile
        return self.db.get_setting(ACTIVE_PROFILE_SETTING, "")

    @property
    def chosen_profile(self) -> str:
        """The profile chosen by hand (what a context override returns to)."""
        return self.db.get_setting(ACTIVE_PROFILE_SETTING, "")

    def set_active_profile(self, name: str) -> None:
        """Choose the profile ("" = All profiles) by hand; clears a context override."""
        name = normalize_profile(name)
        if name and name.lower() not in [p.lower() for p in self.all_profiles()]:
            self.profiles.add(name)
        changed = name != self.active_profile
        self.db.set_setting(ACTIVE_PROFILE_SETTING, name)
        self._context_profile = None
        if changed:
            self.reload()
        self.listener("profile", self.active_profile)

    def all_profiles(self) -> list[str]:
        return self.profiles.all(p for m in self.db.list_macros() for p in m.profiles)

    def rename_profile(self, old: str, new: str) -> None:
        """Rename everywhere: the list, the macros, context rules, the active choice."""
        new = self.profiles.rename(old, new)
        for macro in self.db.list_macros():
            if old.lower() in [p.lower() for p in macro.profiles]:
                macro.profiles = [new if p.lower() == old.lower() else p for p in macro.profiles]
                self.db.save_macro(macro)
        rules = self.context_rules
        for rule in rules:
            if rule.profile is not None and rule.profile.lower() == old.lower():
                rule.profile = new
        self.set_context_rules(rules)
        if self.chosen_profile.lower() == old.lower():
            self.db.set_setting(ACTIVE_PROFILE_SETTING, new)
        self.reload()
        self.listener("profile", self.active_profile)

    def delete_profile(self, name: str) -> None:
        """Forget the profile; macros restricted to it only lose that restriction."""
        self.profiles.remove(name)
        for macro in self.db.list_macros():
            if name.lower() in [p.lower() for p in macro.profiles]:
                macro.profiles = [p for p in macro.profiles if p.lower() != name.lower()]
                self.db.save_macro(macro)
        rules = []
        for rule in self.context_rules:
            if rule.profile is not None and rule.profile.lower() == name.lower():
                if rule.layer is None:
                    continue  # the rule only switched the deleted profile
                rule.profile = None  # keep it for its layer
            rules.append(rule)
        self.set_context_rules(rules)
        if self.chosen_profile.lower() == name.lower():
            self.db.set_setting(ACTIVE_PROFILE_SETTING, "")
        self.reload()
        self.listener("profile", self.active_profile)

    @property
    def active_layer(self) -> str:
        """The keymap layer in effect now ("" = base)."""
        if self._context_layer is not None:
            return self._context_layer
        return self._layer

    def set_layer(self, layer: str, one_shot: bool = False) -> None:
        """Switch the keymap layer by hand or from a macro ("" = back to base)."""
        layer = " ".join(layer.split())
        self._layer, self._layer_one_shot = layer, one_shot and bool(layer)
        self._context_layer = None  # switching by hand wins over a context rule
        self.reload()
        self.listener("layer", self.active_layer)

    def all_layers(self) -> list[str]:
        """The layers any macro uses, sorted."""
        names: dict[str, str] = {}
        for macro in self.db.list_macros():
            if macro.layer:
                names.setdefault(macro.layer.lower(), macro.layer)
        return sorted(names.values(), key=str.lower)

    def _consume_one_shot(self) -> None:
        """A one-shot layer lasts for one hotkey / abbreviation, then base returns."""
        if self._layer_one_shot:
            self._layer, self._layer_one_shot = "", False
            self.reload()
            self.listener("layer", self.active_layer)

    # ------------------------------------------------------------ context rules
    @property
    def context_rules(self) -> list[ContextRule]:
        try:
            return rules_from_json(self.db.get_setting(CONTEXT_RULES_SETTING, ""))
        except ValueError:
            return []

    def set_context_rules(self, rules: list[ContextRule]) -> None:
        """Validate, store and apply; raises ``ValueError`` for an invalid rule."""
        for rule in rules:
            validate_context_rule(rule)
        self.db.set_setting(CONTEXT_RULES_SETTING, rules_to_json(rules))
        self.contexts.update(rules if self.contexts_enabled else [], thread=self._scheduler_thread)

    @property
    def contexts_enabled(self) -> bool:
        return self.db.get_setting(CONTEXTS_ENABLED_SETTING, "1") == "1"

    def set_contexts_enabled(self, enabled: bool) -> None:
        self.db.set_setting(CONTEXTS_ENABLED_SETTING, "1" if enabled else "0")
        self.contexts.update(self.context_rules if enabled else [], thread=self._scheduler_thread)

    def _apply_context(self, rule: ContextRule | None) -> None:
        """The focused window now matches ``rule`` (None: no rule any more)."""
        if self._closed:  # shutting down: nothing may be re-registered
            self._context_profile = self._context_layer = None
            return
        before_profile, before_layer = self.active_profile, self.active_layer
        if rule is None:
            self._context_profile = self._context_layer = None
        else:
            if rule.profile is not None:
                self._context_profile = rule.profile
            if rule.layer is not None:
                self._context_layer = rule.layer
        if (self.active_profile, self.active_layer) != (before_profile, before_layer):
            self.reload()
            if self.active_profile != before_profile:
                self.listener("profile", self.active_profile)
            if self.active_layer != before_layer:
                self.listener("layer", self.active_layer)

    # -------------------------------------------------------------------- sync
    @property
    def sync_folder(self) -> str:
        return self.db.get_setting(sync_mod.FOLDER_SETTING, "")

    @property
    def device_name(self) -> str:
        return self.db.get_setting(sync_mod.DEVICE_SETTING, "") or sync_mod.default_device_name()

    @property
    def sync_interval(self) -> int:
        """Minutes between automatic syncs (0 = only by hand / after changes)."""
        try:
            return max(0, int(self.db.get_setting(sync_mod.INTERVAL_SETTING, str(DEFAULT_SYNC_INTERVAL_MIN))))
        except ValueError:
            return DEFAULT_SYNC_INTERVAL_MIN

    @property
    def sync_settings_too(self) -> bool:
        return self.db.get_setting(sync_mod.SETTINGS_SYNC_SETTING, "1") == "1"

    def set_sync_options(self, folder: str, device: str = "", interval: int | None = None,
                         settings_too: bool | None = None) -> None:
        folder = folder.strip()
        if folder and not os.path.isdir(os.path.expanduser(folder)):
            raise ValueError(f"the folder {folder} does not exist")
        self.db.set_setting(sync_mod.FOLDER_SETTING, folder)
        self.db.set_setting(sync_mod.DEVICE_SETTING, " ".join(device.split()))
        if interval is not None:
            if not 0 <= interval <= 24 * 60:
                raise ValueError("the interval must be between 0 and 1440 minutes")
            self.db.set_setting(sync_mod.INTERVAL_SETTING, str(interval))
        if settings_too is not None:
            self.db.set_setting(sync_mod.SETTINGS_SYNC_SETTING, "1" if settings_too else "0")
        self._sync_wake.set()  # the loop picks up the new interval

    def last_sync(self) -> dict:
        try:
            data = json.loads(self.db.get_setting(sync_mod.LAST_SYNC_SETTING, "") or "{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def sync_now(self) -> SyncOutcome:
        """Sync with the folder and pull every team library, now (any thread)."""
        outcome = SyncOutcome(time=self.clock())
        with self._sync_lock:
            reserved = self.reserved_hotkeys()
            if self.sync_folder:
                settings_too = self.sync_settings_too
                try:
                    outcome.report = sync_mod.sync_folder(
                        self.db, self.sync_folder, self.device_name,
                        get_settings=self.shared_settings if settings_too else None,
                        apply_settings=self.apply_shared_settings if settings_too else None,
                        reserved=reserved,
                    )
                except (sync_mod.SyncError, OSError) as exc:
                    outcome.error = str(exc)
            for sub in self.teams:
                try:
                    outcome.teams[sub.name or sub.path] = team_mod.pull(self.db, sub, reserved)
                except (team_mod.TeamError, OSError) as exc:
                    outcome.team_errors[sub.name or sub.path] = str(exc)
        self.db.set_setting(sync_mod.LAST_SYNC_SETTING, json.dumps({
            "time": outcome.time, "summary": outcome.summary(), "failed": outcome.failed,
            "errors": (outcome.report.errors if outcome.report else []),
        }))
        if outcome.changed_here and not self._closed:
            self.reload()
            self.listener("library", outcome.summary())
        self.listener("sync", outcome)
        return outcome

    def request_sync(self) -> None:
        """Something changed here: sync soon (if a folder or team is set up)."""
        if self.sync_folder or self.teams:
            self._sync_wake.set()

    def start_auto_sync(self) -> None:
        """Sync now, then every ``sync_interval`` minutes and shortly after changes."""
        if self._sync_thread is not None or self._closed:
            return
        self._sync_wake.clear()  # it syncs right away anyway
        self._sync_thread = threading.Thread(target=self._sync_loop, daemon=True, name="sync")
        self._sync_thread.start()

    def _sync_loop(self) -> None:
        while not self._sync_quit.is_set():
            try:
                if self.sync_folder or self.teams:
                    self.sync_now()
                interval = self.sync_interval
            except Exception:  # noqa: BLE001 - the loop must survive anything...
                if self._sync_quit.is_set():
                    return  # ...except shutting down (the database may be closed)
                interval = DEFAULT_SYNC_INTERVAL_MIN
            if self._sync_quit.is_set():
                return
            woken = self._sync_wake.wait(interval * 60 if interval else None)
            self._sync_wake.clear()
            if woken and not self._sync_quit.is_set():
                self._sync_quit.wait(SYNC_DEBOUNCE_S)  # let a burst of edits settle

    def shared_settings(self) -> dict:
        """What "sync settings too" shares: profiles, context rules, trigger rules."""
        triggers = sorted((r.to_dict() for r in self.db.list_triggers()), key=lambda d: (d["name"], d["kind"]))
        return {"profiles": sorted(self.profiles.stored(), key=str.lower),
                "context_rules": [r.to_dict() for r in self.context_rules],
                "triggers": triggers}

    def apply_shared_settings(self, data: dict) -> None:
        from keyboardmanager.core.context import ContextRule
        from keyboardmanager.core.triggers import TriggerRule

        if isinstance(data.get("profiles"), list):
            self.profiles.replace(data["profiles"])
        if isinstance(data.get("context_rules"), list):
            try:
                self.set_context_rules([ContextRule.from_dict(d) for d in data["context_rules"]])
            except ValueError:
                pass  # keep ours rather than half of theirs
        if isinstance(data.get("triggers"), list):
            rules = []
            for d in data["triggers"]:
                try:
                    rules.append(validate_rule(TriggerRule(
                        name=d["name"], kind=d["kind"], macro=d["macro"], params=dict(d.get("params", {})),
                        condition=d.get("condition", ""), queue=bool(d.get("queue")),
                        enabled=bool(d.get("enabled", True)))))
                except (KeyError, TypeError, ValueError):
                    continue
            for old in self.db.list_triggers():
                self.db.delete_trigger(old.id)
            for rule in rules:
                self.db.save_trigger(rule)
            self._reload_triggers()

    # ------------------------------------------------------------------- teams
    @property
    def teams(self) -> list[team_mod.Subscription]:
        return team_mod.subscriptions_from_json(self.db.get_setting(team_mod.TEAMS_SETTING, ""))

    def _save_teams(self, subs: list[team_mod.Subscription]) -> None:
        self.db.set_setting(team_mod.TEAMS_SETTING, team_mod.subscriptions_to_json(subs))

    @property
    def author(self) -> str:
        return self.db.get_setting(team_mod.AUTHOR_SETTING, "") or self.device_name

    def set_author(self, name: str) -> None:
        self.db.set_setting(team_mod.AUTHOR_SETTING, " ".join(name.split()))

    def subscribe_team(self, path: str) -> team_mod.PullReport:
        path = path.strip()
        if any(os.path.abspath(os.path.expanduser(s.path)) == os.path.abspath(os.path.expanduser(path))
               for s in self.teams):
            raise team_mod.TeamError("you already use this team library")
        sub = team_mod.new_subscription(path)
        self._save_teams([*self.teams, sub])
        report = team_mod.pull(self.db, sub, self.reserved_hotkeys())
        self.reload()
        self.listener("library", report.summary())
        return report

    def create_team(self, path: str, name: str) -> team_mod.Subscription:
        team_mod.TeamLibrary.create(path, name)
        self.subscribe_team(path)
        return self.teams[-1]

    def unsubscribe_team(self, team_id: str) -> int:
        subs = self.teams
        sub = next((s for s in subs if s.id == team_id), None)
        if sub is None:
            return 0
        removed = team_mod.unsubscribe(self.db, sub)
        self._save_teams([s for s in subs if s.id != team_id])
        self.reload()
        self.listener("library", f'left "{sub.name}"')
        return removed

    def team(self, team_id: str) -> team_mod.Subscription:
        sub = next((s for s in self.teams if s.id == team_id), None)
        if sub is None:
            raise team_mod.TeamError("no such team library")
        return sub

    def publish(self, macro_ids: list[int], team_id: str, force: bool = False) -> list[str]:
        """Publish macros to a team library; returns one line per macro."""
        sub = self.team(team_id)
        lines = []
        for macro_id in macro_ids:
            macro = self.db.get_macro(macro_id)
            if macro is None:
                continue
            try:
                version = team_mod.publish(self.db, sub, macro, self.author, force=force)
                lines.append(f'"{macro.name}": published (version {version})')
            except team_mod.TeamConflict as exc:
                lines.append(f"{exc} - not published")
            except (team_mod.TeamError, OSError) as exc:
                lines.append(f'"{macro.name}": {exc}')
        return lines

    def unpublish(self, macro_id: int, team_id: str) -> None:
        macro = self.db.get_macro(macro_id)
        if macro is not None:
            team_mod.unpublish(self.db, self.team(team_id), macro.uid)

    def detach(self, macro_id: int) -> None:
        macro = self.db.get_macro(macro_id)
        if macro is not None and macro.source:
            team_mod.detach(self.db, macro)
            self.reload()
            self.listener("library", f'"{macro.name}" is now your own macro')

    def team_of(self, macro) -> team_mod.Subscription | None:
        return next((s for s in self.teams if s.source == macro.source), None) if macro.source else None

    # ----------------------------------------------------------------- sharing
    def share_code(self, macro_ids: list[int]) -> str:
        from keyboardmanager.core import share

        macros = [m for m in (self.db.get_macro(i) for i in macro_ids) if m is not None]
        return share.encode(macros)

    def import_share_code(self, code: str):
        from keyboardmanager.core import io as macro_io
        from keyboardmanager.core import share

        result = macro_io.import_from_text(share.decode(code), self.db)
        self.reload()
        return result

    # ----------------------------------------------------------- remote control
    def remote_settings(self) -> dict:
        try:
            port = int(self.db.get_setting(remote_mod.PORT_SETTING, str(remote_mod.DEFAULT_PORT)))
        except ValueError:
            port = remote_mod.DEFAULT_PORT
        return {"enabled": self.db.get_setting(remote_mod.ENABLED_SETTING, "0") == "1",
                "port": port, "lan": self.db.get_setting(remote_mod.LAN_SETTING, "0") == "1",
                "scope": self.db.get_setting(remote_mod.SCOPE_SETTING, "all"),
                "token": self.remote_token()}

    def remote_token(self) -> str:
        token = self.db.get_setting(remote_mod.TOKEN_SETTING, "")
        if not token:
            token = remote_mod.new_token()
            self.db.set_setting(remote_mod.TOKEN_SETTING, token)
        return token

    def regenerate_remote_token(self) -> str:
        self.db.set_setting(remote_mod.TOKEN_SETTING, remote_mod.new_token())
        self.apply_remote()
        return self.remote_token()

    def set_remote_settings(self, enabled: bool, port: int, lan: bool, scope: str = "all") -> str:
        """Store and apply; returns the error that kept the server from starting ("" = fine)."""
        if not 1 <= port <= 65535:
            raise ValueError("the port must be between 1 and 65535")
        if scope not in remote_mod.SCOPES:
            raise ValueError(f"scope must be one of {', '.join(remote_mod.SCOPES)}")
        self.db.set_setting(remote_mod.ENABLED_SETTING, "1" if enabled else "0")
        self.db.set_setting(remote_mod.PORT_SETTING, str(port))
        self.db.set_setting(remote_mod.LAN_SETTING, "1" if lan else "0")
        self.db.set_setting(remote_mod.SCOPE_SETTING, scope)
        return self.apply_remote()

    def apply_remote(self) -> str:
        """(Re)start or stop the remote control server as configured."""
        self.stop_remote()
        settings = self.remote_settings()
        self.remote_error = ""
        if settings["enabled"] and not self._closed:
            try:
                self.remote = remote_mod.RemoteServer(self, settings["token"], settings["port"], settings["lan"])
            except OSError as exc:
                self.remote_error = f"cannot listen on port {settings['port']}: {exc.strerror or exc}"
        self.listener("remote", self.remote_error)
        return self.remote_error

    def stop_remote(self) -> None:
        server, self.remote = self.remote, None
        if server is not None:
            server.stop()

    def remote_url(self, host: str = "127.0.0.1") -> str:
        port = self.remote.port if self.remote is not None else self.remote_settings()["port"]
        return f"http://{host}:{port}/?token={self.remote_token()}"

    # -------------------------------------------------------------- remapping
    @property
    def remaps(self) -> list[remapping.Remap]:
        try:
            return remapping.remaps_from_json(self.db.get_setting(remapping.SETTING, ""))
        except ValueError:
            return []

    @property
    def remapping_enabled(self) -> bool:
        return self.db.get_setting(remapping.ENABLED_SETTING, "1") == "1"

    def set_remaps(self, remaps: list[remapping.Remap]) -> dict[str, str]:
        """Validate, store and apply; raises ``ValueError`` for an invalid list."""
        remaps = remapping.validate_remaps(remaps)
        self.db.set_setting(remapping.SETTING, remapping.remaps_to_json(remaps))
        return self.apply_remaps()

    def set_remapping_enabled(self, enabled: bool) -> dict[str, str]:
        self.db.set_setting(remapping.ENABLED_SETTING, "1" if enabled else "0")
        return self.apply_remaps()

    def apply_remaps(self) -> dict[str, str]:
        remaps = self.remaps if self.remapping_enabled else []
        try:
            self.remap_failures = self.remapper.apply(remaps) if remaps else {}
            if not remaps:
                self.remapper.clear()
        except Exception as exc:  # noqa: BLE001 - never let remapping break the app
            self.remap_failures = {"*": f"{type(exc).__name__}: {exc}"}
        self.listener("remaps", self.remap_failures)
        return self.remap_failures

    def fire_startup_event(self) -> None:
        """Run the "when Keyboard Manager starts" macros (called once, by the UI)."""
        if not self.autorun_enabled:
            return
        window = self.backend.active_window()
        for macro in self.db.list_macros():
            if self.macro_active(macro) and macro.event == "app_start" and macro.matches(window):
                self.run(macro, trigger=f"event: {describe_event('app_start')}")

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
        # a macro on the active layer wins over a base one, an application-specific
        # one over a global one
        matching.sort(key=lambda m: (not m.layer, not m.is_app_specific))
        if matching:
            self._consume_one_shot()
            self.run(matching[0], trigger=f"hotkey {hotkey}")

    # ---------------------------------------------------------------- execution
    @property
    def running_macro(self) -> Macro | None:
        return self._running

    @property
    def closed(self) -> bool:
        """True once ``shutdown()`` has begun."""
        return self._closed

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
        queue: bool | None = None,
        chain: int = 0,
        debugger: Debugger | None = None,
        simulate: bool = False,
    ) -> bool:
        """Start ``macro`` on a worker thread.

        Returns False if another macro is running and this one is not queued.
        ``queue`` (default: the macro's "when busy" setting) puts it in the queue
        instead, to run when the current one ends. Triggers (hotkeys, abbreviations,
        schedules, events, trigger rules) respect the macro's cooldown and daily
        limit; a run refused for that is recorded as skipped and counts as handled
        (True). Starting a macro on purpose (manual, test run, favorite) is never
        limited. ``debugger`` / ``simulate`` are for debug runs; ``chain`` counts runs
        started by "macro finished" triggers in a row.
        """
        if self._closed:
            return False
        if trigger not in MANUAL_TRIGGERS:
            reason = self.limit_reason(macro)
            if reason:
                self._record_skipped(macro, trigger, f"Skipped: {reason}")
                return True
        job = _Job(macro, trigger, start_delay, chain, debugger, simulate, self.clock())
        with self._lock:
            if self._running is not None:
                wants_queue = queue if queue is not None else macro.when_busy == "queue"
                if not wants_queue:
                    return False
                if len(self._queue) >= MAX_QUEUE:
                    queued = False
                else:
                    self._queue.append(job)
                    queued = True
            else:
                queued = None
                thread = self._begin(job)
        if queued is not None:
            if queued:
                self.listener("queue", self.queue_snapshot())
            else:
                self._record_skipped(macro, trigger, f"Skipped: the queue is full ({MAX_QUEUE} runs)")
            return True
        if blocking:
            thread.join()
        return True

    def _begin(self, job: "_Job") -> threading.Thread:
        """Start ``job`` (with self._lock held)."""
        self._running = job.macro
        self._job = job
        self._paused = False
        self._announced = False
        backend = SimulatedBackend(self.backend) if job.simulate else self.backend
        # Created here, not on the worker, so stop() / pause() issued right after
        # run() returns always reach it.
        self._executor = MacroExecutor(
            backend, ui=self.ui, saved=self.saved_variables, find_macro=self.find_macro,
            speed=lambda: self._playback_speed, log_steps=self.log_steps, debugger=job.debugger,
            set_layer=self.set_layer, set_profile=self.set_active_profile,
        )
        if job.debugger is not None:
            job.debugger.pause = self.pause  # a breakpoint is an ordinary pause
        thread = threading.Thread(
            target=self._work, args=(self._executor, job.macro, job.trigger, job.start_delay),
            daemon=True, name="macro",
        )
        self._worker = thread
        thread.start()
        return thread

    # ---------------------------------------------------------- trigger rules
    def _reload_triggers(self) -> None:
        # trigger rules run macros by themselves, like schedules: the same switch
        self.triggers.update(self.db.list_triggers() if self.autorun_enabled else [], thread=self._scheduler_thread)

    def save_trigger(self, rule: TriggerRule) -> TriggerRule:
        validate_rule(rule)
        self.db.save_trigger(rule)
        self.triggers.status.pop(rule.id, None)  # type: ignore[arg-type]
        self._reload_triggers()
        return rule

    def delete_trigger(self, rule_id: int) -> None:
        self.db.delete_trigger(rule_id)
        self.triggers.status.pop(rule_id, None)
        self._reload_triggers()

    def rename_in_triggers(self, old: str, new: str) -> int:
        """A macro was renamed: point the trigger rules that name it to the new name.
        Returns how many rules changed."""
        changed = 0
        for rule in self.db.list_triggers():
            touched = False
            if rule.macro.strip().lower() == old.strip().lower():
                rule.macro, touched = new, True
            if rule.kind == "macro_finished" and str(rule.params.get("macro", "")).strip().lower() == old.strip().lower():
                rule.params["macro"], touched = new, True
            if touched:
                self.db.save_trigger(rule)
                changed += 1
        if changed:
            self._reload_triggers()
        return changed

    def _fire_rule(self, rule: TriggerRule, trigger: str, chain: int) -> None:
        macro = self.find_macro(rule.macro)
        if macro is None:
            self.triggers.set_status(rule, f'not started: there is no macro named "{rule.macro}"')
            return
        if not macro.enabled:
            self.triggers.set_status(rule, f'not started: "{macro.name}" is disabled')
            return
        if not self.macro_active(macro):
            self.triggers.set_status(rule, f'not started: "{macro.name}" is not in the active profile')
            return
        reason = self.limit_reason(macro)
        busy = self._running is not None
        full = len(self._queue) >= MAX_QUEUE
        if not self.run(macro, trigger=trigger, queue=True if rule.queue else None, chain=chain):
            self.triggers.set_status(rule, "not started: another macro was running")
        elif reason:
            self.triggers.set_status(rule, f"not started: {reason}")
        elif busy:
            self.triggers.set_status(rule, "not started: the queue is full" if full
                                     else "queued: another macro was running")

    def evaluate(self, expression: str):
        """An expression outside a run: saved variables and built-ins."""
        import random as _random

        from keyboardmanager.core.script import evaluate
        from keyboardmanager.core.variables import builtin_value

        def lookup(name: str):
            value = self.saved_variables.get(name)
            if value is None:
                value = builtin_value(
                    name, "", now=lambda: datetime.fromtimestamp(self.clock()), rng=_random.Random(),
                    clipboard=self.ui.get_clipboard,
                    window=lambda: (lambda w: (w.app, w.title))(self.backend.active_window()), macro_name="",
                )
            if value is None:
                raise KeyError(name)
            return value

        return evaluate(expression, lookup)

    # ------------------------------------------------------------------- queue
    def queue_snapshot(self) -> list[tuple[str, str]]:
        """The queued runs, first one first: (macro name, trigger)."""
        with self._lock:
            return [(job.macro.name, job.trigger) for job in self._queue]

    def remove_queued(self, index: int) -> None:
        with self._lock:
            if 0 <= index < len(self._queue):
                del self._queue[index]
        self.listener("queue", self.queue_snapshot())

    def clear_queue(self) -> None:
        with self._lock:
            had = bool(self._queue)
            self._queue.clear()
        if had:
            self.listener("queue", [])

    # --------------------------------------------------------------- live view
    def live_state(self) -> dict | None:
        """What the running macro is doing, for the live view (None when idle)."""
        with self._lock:
            executor, job = self._executor, self._job
            macro, paused = self._running, self._paused
        if executor is None or macro is None or job is None:
            return None
        state = executor.snapshot()
        debugger = job.debugger
        state.update(
            macro=macro.name, trigger=job.trigger, paused=paused, debugging=debugger is not None,
            stopped_at=debugger.stopped_at if debugger is not None else None,
            simulated=executor.backend.recent() if isinstance(executor.backend, SimulatedBackend) else None,
        )
        return state

    def last_run_state(self) -> dict | None:
        """``live_state()`` as it was when the last run ended (None before any run)."""
        return self._last_state

    def debug_continue(self) -> None:
        job = self._job
        if job is not None and job.debugger is not None:
            job.debugger.continue_()
        self.resume()

    def debug_step(self) -> None:
        job = self._job
        if job is not None and job.debugger is not None:
            job.debugger.step()
        self.resume()

    # ------------------------------------------------------------------ limits
    def limit_reason(self, macro: Macro) -> str | None:
        """Why a trigger may not start ``macro`` now (cooldown, daily limit), or None."""
        limits, now = macro.limits, self.clock()
        if macro.id is None or not (limits.cooldown_s or limits.max_runs_per_day):
            return None
        if limits.cooldown_s:
            last = self.db.last_run(macro.id)
            if last is not None and now - last < limits.cooldown_s:
                wait = limits.cooldown_s - (now - last)
                return f"cooldown: it may run again in {wait:.0f} s"
        if limits.max_runs_per_day:
            midnight = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
            runs = self.db.runs_since(macro.id, midnight.timestamp())
            if runs >= limits.max_runs_per_day:
                return f"it already ran {runs} time(s) today (limit {limits.max_runs_per_day})"
        return None

    # --------------------------------------------------------------- step logs
    @property
    def log_steps(self) -> bool:
        """Whether runs record each step in the history."""
        return self.db.get_setting(LOG_STEPS_SETTING, "1") == "1"

    def set_log_steps(self, on: bool) -> None:
        self.db.set_setting(LOG_STEPS_SETTING, "1" if on else "0")

    # ---------------------------------------------------------- playback speed
    @property
    def playback_speed(self) -> float:
        """Global speed multiplier for every macro (applies to a running one at once)."""
        return self._playback_speed

    def set_playback_speed(self, speed: float) -> None:
        self._playback_speed = validate_speed(speed)
        self.db.set_setting(PLAYBACK_SPEED_SETTING, repr(self._playback_speed))

    def find_macro(self, name: str) -> Macro | None:
        """A macro by name (case-insensitive), for "Run another macro"."""
        wanted = name.strip().lower()
        return next((m for m in self.db.list_macros() if m.name.lower() == wanted), None)

    def stop(self) -> None:
        """Stop the running macro (also when it is paused) and empty the queue: after
        an emergency stop, nothing queued may start typing."""
        self.clear_queue()
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
            # before "started" the worker announces the pause itself (see _work)
            if announce:
                self.listener("paused" if paused else "resumed", macro)
        return True

    def toggle_pause(self) -> None:
        """Target of the pause hotkey: pause if running, resume if paused."""
        if not self.pause():
            self.resume()

    def _work(self, executor: MacroExecutor, macro: Macro, trigger: str, start_delay: float) -> None:
        history_id = self.db.start_history(macro, trigger, started=self.clock())
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
        self._last_state = self.live_state()  # how the run ended, for the live view
        with self._events_lock:
            try:
                # history first: once running_macro is None the run is fully recorded
                self.db.finish_history(
                    history_id, result.status, result.duration_ms, result.actions_run, result.message
                )
                if result.steps or result.steps_not_logged:
                    self.db.add_history_steps(history_id, result.steps, result.steps_not_logged)
            finally:  # even if the database fails, the service must not stay "running"
                with self._lock:
                    job = self._job
                    self._executor, self._running, self._paused, self._job = None, None, False, None
                    self._announced = False
            self.listener("finished", (macro, result))
        if trigger != "debug":  # a debug run (maybe with simulated input) starts nothing else
            self.triggers.macro_finished(macro.name, result.status, job.chain if job is not None else 0)
        self._start_next()

    def _start_next(self) -> None:
        """Start the next queued run, if nothing else started meanwhile."""
        with self._lock:
            if self._running is not None or not self._queue or self._closed:
                return
            self._begin(self._queue.popleft())
        self.listener("queue", self.queue_snapshot())

    def shutdown(self, timeout: float = 2.0) -> None:
        """Stop everything before the process exits. Safe to call more than once.

        Waits (up to ``timeout``) for a running macro to end, because it releases the
        keys and mouse buttons it holds on its way out; exiting first could leave e.g.
        Shift logically pressed in the operating system.
        """
        self._closed = True  # e.g. a "macro finished" trigger may not start a run now
        self._sync_quit.set()
        self._sync_wake.set()
        sync_thread, self._sync_thread = self._sync_thread, None
        if sync_thread is not None and sync_thread is not threading.current_thread():
            sync_thread.join(5)  # a sync in progress finishes writing its files
        self.stop_remote()
        self.scheduler.stop()
        self.events.stop()
        self.expander.stop()
        self.triggers.stop()
        self.contexts.update([], thread=False)  # also stops its thread
        self.stop()
        if self.hotkeys is not None:
            self.hotkeys.stop()
        try:
            self.remapper.clear()  # hidutil mappings would otherwise outlive the app
        except Exception:  # noqa: BLE001
            pass
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout)


def expansion_macro(macro: Macro, abbreviation: str) -> Macro:
    """``macro`` preceded by deleting the abbreviation that was just typed."""
    pause = new_action("delay")
    pause.params["ms"] = 30  # let the application receive the last typed character first
    erase = new_action("repeat")
    erase.params["count"] = len(abbreviation)
    backspace = new_action("key_press")
    backspace.params["keys"] = "backspace"
    erase.children = [backspace]
    copy = Macro.from_dict(macro.to_dict())
    copy.id, copy.actions = macro.id, [pause, erase, *copy.actions]
    return copy
