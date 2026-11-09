"""Input backends: the only place that talks to the operating system for synthetic input.

``InputBackend`` is a small protocol so the executor can be tested with a fake.
``PynputBackend`` is the real implementation; pynput is imported lazily so that
importing this module never requires a display server.
"""

from __future__ import annotations

import sys
from typing import Any, Protocol

from keyboardmanager.core import keys
from keyboardmanager.core.models import ActiveWindow
from keyboardmanager.core.windowinfo import get_active_window


class InputBackend(Protocol):
    def key_down(self, key: str) -> None: ...
    def key_up(self, key: str) -> None: ...
    def type_text(self, text: str) -> None: ...
    def mouse_position(self) -> tuple[int, int]: ...
    def mouse_move(self, x: int, y: int) -> None: ...
    def mouse_down(self, button: str) -> None: ...
    def mouse_up(self, button: str) -> None: ...
    def mouse_click(self, button: str, count: int) -> None: ...
    def mouse_scroll(self, dx: int, dy: int) -> None: ...
    def active_window(self) -> ActiveWindow: ...


# ----------------------------------------------------------------- key mapping
_MODIFIER_NAMES = {
    "ctrl": "ctrl", "ctrl_l": "ctrl", "ctrl_r": "ctrl",
    "alt": "alt", "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt",
    "shift": "shift", "shift_l": "shift", "shift_r": "shift",
    "cmd": "cmd", "cmd_l": "cmd", "cmd_r": "cmd",
}


def darwin_base_chars() -> dict[int, str]:
    """macOS: key code -> character the key types *without* modifiers, for the
    current keyboard layout. ``{}`` on other platforms or if unavailable.

    On macOS pynput reports the composed character, so Option+S arrives as ``ß``
    and Option+K as ``˚``; this table maps such keys back to ``s`` / ``k``. Uses the
    macOS text input APIs, so call it on the main thread (hotkeys are registered
    and recordings started from the GUI thread).
    """
    if sys.platform != "darwin":
        return {}
    try:
        from pynput._util.darwin import keycode_context, keycode_to_string

        table: dict[int, str] = {}
        with keycode_context() as context:
            for vk in range(128):
                char = keycode_to_string(context, vk)
                if len(char) == 1 and char.isprintable() and not char.isspace():
                    table[vk] = char.lower()
        return table
    except Exception:  # noqa: BLE001 - fall back to pynput's characters
        return {}


def base_key(key: Any, base_chars: dict[int, str]) -> Any:
    """Replace a character key by its unmodified character (see ``darwin_base_chars``)."""
    vk = getattr(key, "vk", None)
    if getattr(key, "char", None) is None or vk not in base_chars:
        return key
    from pynput.keyboard import KeyCode

    return KeyCode.from_char(base_chars[vk], vk=vk)


def pynput_key_to_name(
    key: Any, ctrl_held: bool = False, base_chars: dict[int, str] | None = None
) -> str | None:
    """Translate a key object received from a pynput listener to a canonical name.

    Returns ``None`` for keys we cannot represent. When ``ctrl_held`` is true a
    control character (what Windows reports for ctrl+letter) is mapped back to
    its letter. ``base_chars`` (pass it while ctrl / alt / cmd are held) maps keys
    to their unmodified character, so macOS Option+S is ``s`` rather than ``ß``.
    """
    if base_chars:
        key = base_key(key, base_chars)
    name = getattr(key, "name", None)
    if name is not None:  # pynput.keyboard.Key member
        name = _MODIFIER_NAMES.get(name, name)
        try:
            return keys.normalize_key(name)
        except ValueError:
            return None
    char = getattr(key, "char", None)
    if char:
        if not char.isprintable() and len(char) == 1 and 1 <= ord(char) <= 26:
            char = chr(ord(char) + 96)
        if char.isprintable():
            return {" ": "space", "+": "plus"}.get(char, char.lower())
    vk = getattr(key, "vk", None)
    if vk is not None:
        return f"vk:{vk}"
    return None


def name_to_pynput_key(name: str) -> Any:
    """Canonical key name -> object accepted by ``pynput.keyboard.Controller``."""
    from pynput.keyboard import Key, KeyCode

    name = keys.normalize_key(name)
    if name == "plus":
        return KeyCode.from_char("+")
    if name.startswith("vk:"):
        return KeyCode.from_vk(int(name[3:]))
    member = getattr(Key, name, None)
    if member is not None:
        return member
    if len(name) == 1:
        return KeyCode.from_char(name)
    raise ValueError(f"key {name!r} is not supported on this platform")


class PynputBackend:
    def __init__(self) -> None:
        from pynput import keyboard, mouse

        self._kb = keyboard.Controller()
        self._mouse = mouse.Controller()
        self._buttons = {
            "left": mouse.Button.left,
            "right": mouse.Button.right,
            "middle": mouse.Button.middle,
        }

    def key_down(self, key: str) -> None:
        self._kb.press(name_to_pynput_key(key))

    def key_up(self, key: str) -> None:
        self._kb.release(name_to_pynput_key(key))

    def type_text(self, text: str) -> None:
        self._kb.type(text)

    def mouse_position(self) -> tuple[int, int]:
        x, y = self._mouse.position
        return int(x), int(y)

    def mouse_move(self, x: int, y: int) -> None:
        self._mouse.position = (x, y)

    def mouse_down(self, button: str) -> None:
        self._mouse.press(self._buttons[button])

    def mouse_up(self, button: str) -> None:
        self._mouse.release(self._buttons[button])

    def mouse_click(self, button: str, count: int) -> None:
        self._mouse.click(self._buttons[button], count)

    def mouse_scroll(self, dx: int, dy: int) -> None:
        self._mouse.scroll(dx, dy)

    def active_window(self) -> ActiveWindow:
        return get_active_window()
