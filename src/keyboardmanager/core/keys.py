"""Key-name normalisation shared by the editor, the executor and the hotkey backends.

Canonical key names follow pynput's ``Key`` member names (``enter``, ``page_up``,
``f5`` ...) plus single printable characters. Modifiers are always one of
``ctrl``, ``alt``, ``shift`` and ``cmd`` (Win / Super / Command key). Keys that
cannot be named are stored as ``vk:<code>``.
"""

from __future__ import annotations

import re

MODIFIERS: tuple[str, ...] = ("ctrl", "alt", "shift", "cmd")

_MODIFIER_ALIASES = {
    "control": "ctrl",
    "ctl": "ctrl",
    "option": "alt",
    "opt": "alt",
    "alt_gr": "alt",
    "altgr": "alt",
    "command": "cmd",
    "win": "cmd",
    "windows": "cmd",
    "super": "cmd",
    "meta": "cmd",
}

_KEY_ALIASES = {
    "return": "enter",
    "escape": "esc",
    "del": "delete",
    "ins": "insert",
    "pgup": "page_up",
    "pageup": "page_up",
    "pgdn": "page_down",
    "pagedown": "page_down",
    "spacebar": "space",
    "capslock": "caps_lock",
    "printscreen": "print_screen",
    "prtsc": "print_screen",
}

FUNCTION_KEYS = frozenset(f"f{i}" for i in range(1, 25))

NAMED_KEYS = frozenset(
    {
        "enter", "esc", "tab", "space", "backspace", "delete", "insert",
        "home", "end", "page_up", "page_down", "up", "down", "left", "right",
        "caps_lock", "num_lock", "scroll_lock", "pause", "print_screen",
        "menu", "plus",
    }
) | FUNCTION_KEYS

_VK_RE = re.compile(r"^vk:\d+$")


def normalize_key(name: str) -> str:
    """Return the canonical name for a single key or raise ``ValueError``."""
    if name == " ":
        return "space"
    raw = name.strip()
    if not raw:
        raise ValueError("empty key name")
    if raw == "+":
        return "plus"
    low = raw.lower()
    if low in MODIFIERS:
        return low
    if low in _MODIFIER_ALIASES:
        return _MODIFIER_ALIASES[low]
    if low in _KEY_ALIASES:
        return _KEY_ALIASES[low]
    if low in NAMED_KEYS or _VK_RE.match(low):
        return low
    if len(raw) == 1 and raw.isprintable():
        return low
    raise ValueError(f"unknown key {name!r}")


def split_combo(text: str) -> list[str]:
    """Split ``"ctrl+shift+k"`` into raw parts (``"ctrl++"`` means ctrl and plus)."""
    s = text.strip()
    if not s:
        raise ValueError("empty key combination")
    if s == "+":
        return ["+"]
    s = s.replace("++", "+plus")
    parts = [p.strip() for p in s.split("+")]
    if any(not p for p in parts):
        raise ValueError(f"malformed key combination {text!r}")
    return parts


def combo_keys(text: str) -> list[str]:
    """Canonical key names of a combination: modifiers first, then other keys."""
    names = [normalize_key(p) for p in split_combo(text)]
    mods = [m for m in MODIFIERS if m in names]
    others = [n for n in names if n not in MODIFIERS]
    return mods + others


def normalize_combo(text: str) -> str:
    return "+".join(combo_keys(text))


MAX_SEQUENCE_CHORDS = 3


def normalize_hotkey(text: str) -> str:
    """Canonical single hotkey chord: optional modifiers plus exactly one regular key."""
    keys = combo_keys(text)
    others = [k for k in keys if k not in MODIFIERS]
    if len(others) != 1:
        raise ValueError("a hotkey needs exactly one non-modifier key")
    if others[0] in ("plus",) or others[0].startswith("vk:"):
        raise ValueError(f"{others[0]!r} cannot be used in a hotkey")
    return "+".join(keys)


def normalize_hotkey_sequence(text: str) -> str:
    """Canonical hotkey or sequence of chords pressed one after another.

    ``"Ctrl+K Ctrl+S"`` -> ``"ctrl+k ctrl+s"``; chords are separated by spaces.
    """
    chords = [normalize_hotkey(part) for part in text.split()]
    if not chords:
        raise ValueError("empty hotkey")
    if len(chords) > MAX_SEQUENCE_CHORDS:
        raise ValueError(f"a hotkey sequence may have at most {MAX_SEQUENCE_CHORDS} chords")
    return " ".join(chords)


def sequence_chords(hotkey: str) -> list[str]:
    """The chords of a (possibly single-chord) hotkey sequence, canonical."""
    return normalize_hotkey_sequence(hotkey).split()


def sequence_prefix_conflict(a: str, b: str) -> bool:
    """True when pressing one hotkey inevitably involves the other: they are equal or
    one is how the other starts (``ctrl+k`` vs ``ctrl+k ctrl+s``)."""
    chords_a, chords_b = a.split(), b.split()
    shorter = min(len(chords_a), len(chords_b))
    return chords_a[:shorter] == chords_b[:shorter]


def to_pynput_hotkey(hotkey: str) -> str:
    """``ctrl+k`` -> ``<ctrl>+k`` as expected by ``pynput.keyboard.GlobalHotKeys``."""
    parts = []
    for key in combo_keys(hotkey):
        parts.append(key if len(key) == 1 else f"<{key}>")
    return "+".join(parts)


_KEYBOARD_LIB_NAMES = {
    "cmd": "windows",
    "page_up": "page up",
    "page_down": "page down",
    "caps_lock": "caps lock",
    "num_lock": "num lock",
    "scroll_lock": "scroll lock",
    "print_screen": "print screen",
}


def to_keyboard_hotkey(hotkey: str) -> str:
    """Convert to the naming used by the ``keyboard`` package."""
    return "+".join(_KEYBOARD_LIB_NAMES.get(k, k) for k in combo_keys(hotkey))
