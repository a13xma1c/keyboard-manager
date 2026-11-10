"""Global hotkey registration.

Two interchangeable backends are provided:

* ``PynputHotkeys``   - ``pynput.keyboard.GlobalHotKeys`` (Windows, macOS, Linux/X11).
* ``KeyboardHotkeys`` - the ``keyboard`` package (best on Windows; needs root on
  Linux and macOS).

``create_hotkey_backend("auto")`` picks ``keyboard`` on Windows and ``pynput``
elsewhere, and falls back to pynput if the ``keyboard`` package cannot hook.
"""

from __future__ import annotations

import sys
import time
from typing import Callable, Iterable, Protocol

from keyboardmanager.core import keys

Callback = Callable[[], None]

SEQUENCE_TIMEOUT_S = 2.0  # max pause between the chords of a hotkey sequence


class SequenceMatcher:
    """Turns single chord presses into (multi-chord) hotkey activations.

    The operating system backends only register single chords; this matcher keeps
    the "which chords came before" state for sequences such as ``ctrl+k ctrl+s``.
    An exact match fires immediately (the editor refuses a hotkey that is also how
    a longer one starts, so waiting would never be right); an unfinished prefix
    waits up to ``SEQUENCE_TIMEOUT_S`` for the next chord.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._sequences: dict[tuple[str, ...], str] = {}
        self._prefix: list[str] = []
        self._prefix_at = 0.0

    def configure(self, sequences: Iterable[str]) -> set[str]:
        """Set the active sequences; returns every chord that must be registered."""
        self._sequences = {tuple(s.split()): s for s in sequences}
        self._prefix = []
        return {chord for chords in self._sequences for chord in chords}

    def press(self, chord: str) -> str | None:
        """Feed one pressed chord; returns the completed hotkey sequence, if any."""
        now = self._clock()
        if self._prefix and now - self._prefix_at > SEQUENCE_TIMEOUT_S:
            self._prefix = []
        # prefer continuing a pending sequence; otherwise the chord starts fresh
        for candidate in ([*self._prefix, chord], [chord]) if self._prefix else ([chord],):
            pressed = tuple(candidate)
            if pressed in self._sequences:
                self._prefix = []
                return self._sequences[pressed]
            if any(known[: len(pressed)] == pressed for known in self._sequences if len(known) > len(pressed)):
                self._prefix, self._prefix_at = candidate, now
                return None
        self._prefix = []
        return None


class HotkeyBackend(Protocol):
    name: str

    def start(self, mapping: dict[str, Callback]) -> dict[str, str]:
        """Register ``mapping`` (canonical hotkey -> callback).

        Returns ``{hotkey: error}`` for hotkeys that could not be registered.
        """

    def stop(self) -> None: ...


_LAYOUT_AWARE_CLASS = None


def layout_aware_hotkeys_class():
    """``pynput.keyboard.GlobalHotKeys`` that matches keys by their unmodified
    character where the platform reports composed ones (macOS: Option+S is ``ß``),
    so hotkeys such as ``ctrl+alt+s`` fire. Created lazily: importing this module
    must not require pynput.
    """
    global _LAYOUT_AWARE_CLASS
    if _LAYOUT_AWARE_CLASS is None:
        from pynput import keyboard

        from keyboardmanager.core.backend import base_key, safe_listener

        class LayoutAwareGlobalHotKeys(safe_listener(keyboard.GlobalHotKeys)):
            def __init__(self, hotkeys, base_chars: dict[int, str] | None = None, *args, **kwargs):
                self._base_chars = base_chars or {}
                super().__init__(hotkeys, *args, **kwargs)

            def canonical(self, key):  # pynput's hook for platform normalisation
                return super().canonical(base_key(key, self._base_chars))

        _LAYOUT_AWARE_CLASS = LayoutAwareGlobalHotKeys
    return _LAYOUT_AWARE_CLASS


class PynputHotkeys:
    name = "pynput"

    def __init__(self) -> None:
        self._listener = None
        self._generation = 0

    def _guarded(self, callback: Callback) -> Callback:
        """Only the current registration may act: pynput does not always manage to
        stop a replaced listener (seen on X11), and a stale one must not fire hotkeys
        after they were changed or switched off."""
        generation = self._generation

        def call() -> None:
            if generation == self._generation:
                callback()

        return call

    def start(self, mapping: dict[str, Callback]) -> dict[str, str]:
        from pynput import keyboard

        from keyboardmanager.core.backend import darwin_base_chars, start_listener, stop_listener

        self.stop()
        failures: dict[str, str] = {}
        valid: dict[str, Callback] = {}
        for hotkey, cb in mapping.items():
            try:
                spec = keys.to_pynput_hotkey(hotkey)
                keyboard.HotKey.parse(spec)
            except Exception as exc:  # noqa: BLE001
                failures[hotkey] = str(exc) or "unsupported hotkey"
                continue
            valid[spec] = self._guarded(cb)
        if valid:
            # the table is rebuilt on every (re)registration, so a keyboard layout
            # switch is picked up the next time hotkeys are reloaded
            base_chars = darwin_base_chars()
            for _attempt in range(2):  # a listener occasionally fails to start (X11)
                listener = layout_aware_hotkeys_class()(valid, base_chars)
                listener.daemon = True
                start_listener(listener)
                self._listener = listener
                if getattr(listener, "_ready", True):
                    break
                stop_listener(listener)  # discard it; the retry uses the same callbacks
            else:
                failures["*"] = "the global hotkey listener could not be started"
        return failures

    def stop(self) -> None:
        # Not joined: on Linux the listener thread only ends with the next input
        # event, and waiting for it would freeze the window on every re-registration.
        from keyboardmanager.core.backend import stop_listener

        self._generation += 1  # silences the old listener even if pynput cannot stop it
        listener, self._listener = self._listener, None
        if listener is not None:
            stop_listener(listener)


class KeyboardHotkeys:
    name = "keyboard"

    def __init__(self) -> None:
        self._handles: list = []

    def start(self, mapping: dict[str, Callback]) -> dict[str, str]:
        import keyboard

        self.stop()
        failures: dict[str, str] = {}
        for hotkey, cb in mapping.items():
            try:
                self._handles.append(keyboard.add_hotkey(keys.to_keyboard_hotkey(hotkey), cb))
            except Exception as exc:  # noqa: BLE001
                failures[hotkey] = str(exc) or "unsupported hotkey"
        return failures

    def stop(self) -> None:
        if not self._handles:
            return
        import keyboard

        for handle in self._handles:
            try:
                keyboard.remove_hotkey(handle)
            except (KeyError, ValueError):
                pass
        self._handles = []


class FallbackHotkeys:
    """Try the ``keyboard`` package first, switch to pynput if it cannot hook."""

    def __init__(self) -> None:
        self._primary: HotkeyBackend = KeyboardHotkeys()
        self._secondary: HotkeyBackend = PynputHotkeys()
        self._using: HotkeyBackend = self._primary

    @property
    def name(self) -> str:  # type: ignore[override]
        return self._using.name

    def start(self, mapping: dict[str, Callback]) -> dict[str, str]:
        self._secondary.stop()
        try:
            failures = self._primary.start(mapping)
            self._using = self._primary
            # Every hotkey failing means the package cannot hook at all (e.g. no root).
            if mapping and len(failures) == len(mapping):
                raise RuntimeError("keyboard backend unavailable")
            return failures
        except Exception:  # noqa: BLE001
            self._primary.stop()
            self._using = self._secondary
            return self._secondary.start(mapping)

    def stop(self) -> None:
        self._primary.stop()
        self._secondary.stop()


def create_hotkey_backend(kind: str = "auto") -> HotkeyBackend:
    # Importing the `keyboard` package aborts the whole process on macOS (a native
    # assertion Python cannot catch), so it is never used there.
    if kind == "pynput" or sys.platform == "darwin":
        return PynputHotkeys()
    if kind == "keyboard":
        return KeyboardHotkeys()
    return FallbackHotkeys() if sys.platform == "win32" else PynputHotkeys()
