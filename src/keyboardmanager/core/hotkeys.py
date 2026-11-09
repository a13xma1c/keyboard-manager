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
from typing import Callable, Protocol

from keyboardmanager.core import keys

Callback = Callable[[], None]


class HotkeyBackend(Protocol):
    name: str

    def start(self, mapping: dict[str, Callback]) -> dict[str, str]:
        """Register ``mapping`` (canonical hotkey -> callback).

        Returns ``{hotkey: error}`` for hotkeys that could not be registered.
        """

    def stop(self) -> None: ...


class PynputHotkeys:
    name = "pynput"

    def __init__(self) -> None:
        self._listener = None

    def start(self, mapping: dict[str, Callback]) -> dict[str, str]:
        from pynput import keyboard

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
            valid[spec] = cb
        if valid:
            self._listener = keyboard.GlobalHotKeys(valid)
            self._listener.daemon = True
            self._listener.start()
        return failures

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None


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
