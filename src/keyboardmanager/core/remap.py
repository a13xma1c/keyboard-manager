"""Key remapping: make one physical key act as another while Keyboard Manager runs.

Remapping has to *suppress* the original key, which our input listeners cannot do,
so each platform uses what it offers:

* macOS   - ``hidutil`` (Apple TN2450): remaps in the keyboard driver, needs no
  permission and also handles Caps Lock and modifiers. It is system-wide until
  reboot, so the previous mapping is saved first and restored on quit, and also
  on the next start if Keyboard Manager ever crashed.
* Windows - the ``keyboard`` package (``remap_key``), a low-level hook.
* Linux   - the ``keyboard`` package too, which needs root; otherwise remapping
  is reported as unavailable (X11 offers no per-key suppression to pynput).

Keys are named as everywhere else (``caps_lock``, ``esc``, ``a``); letters and
symbols mean the key at that position on a US keyboard.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Protocol

from keyboardmanager.core import keys

SETTING = "key_remaps"                  # the remaps, as JSON
ENABLED_SETTING = "key_remaps_enabled"  # "1" / "0"
PREVIOUS_SETTING = "key_remaps_previous_hidutil"  # macOS crash recovery


@dataclass(frozen=True)
class Remap:
    source: str
    target: str

    def to_dict(self) -> dict[str, str]:
        return {"from": self.source, "to": self.target}


def validate_remaps(remaps: list[Remap]) -> list[Remap]:
    """Normalise; raise ``ValueError`` for anything ambiguous.

    A key that is remapped may not also be a target: chains and swaps behave
    differently per platform (a hook would feed its own output back in).
    """
    out: list[Remap] = []
    for remap in remaps:
        source, target = keys.normalize_key(remap.source), keys.normalize_key(remap.target)
        for name in (source, target):
            if name == "plus" or name.startswith("vk:"):
                raise ValueError(f"{name!r} cannot be remapped")
        if source == target:
            raise ValueError(f"{source} is remapped to itself")
        out.append(Remap(source, target))
    sources = [r.source for r in out]
    for source in sources:
        if sources.count(source) > 1:
            raise ValueError(f"{source} is remapped more than once")
    for r in out:
        if r.target in sources:
            raise ValueError(
                f"{r.target} is both remapped and a target; chains and swaps are not supported"
            )
    return out


def remaps_from_json(text: str) -> list[Remap]:
    if not text:
        return []
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("remaps must be a list")
    remaps = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("from"), str) or not isinstance(item.get("to"), str):
            raise ValueError("each remap needs 'from' and 'to'")
        remaps.append(Remap(item["from"], item["to"]))
    return validate_remaps(remaps)


def remaps_to_json(remaps: list[Remap]) -> str:
    return json.dumps([r.to_dict() for r in remaps])


class RemapBackend(Protocol):
    name: str

    def unavailable_reason(self) -> str | None:
        """Why remapping cannot work here, or ``None``."""

    def apply(self, remaps: list[Remap]) -> dict[str, str]:
        """Replace the active remaps; returns ``{source: error}`` for failures."""

    def clear(self) -> None:
        """Undo every remap (called on quit)."""


# --------------------------------------------------------------------- macOS
# USB HID keyboard usage IDs (usage page 7), as used by hidutil.
HID_USAGES: dict[str, int] = {
    **{chr(ord("a") + i): 0x04 + i for i in range(26)},
    **{str(d): 0x1E + d - 1 for d in range(1, 10)}, "0": 0x27,
    "enter": 0x28, "esc": 0x29, "backspace": 0x2A, "tab": 0x2B, "space": 0x2C,
    "-": 0x2D, "=": 0x2E, "[": 0x2F, "]": 0x30, "\\": 0x31, ";": 0x33, "'": 0x34,
    "`": 0x35, ",": 0x36, ".": 0x37, "/": 0x38, "caps_lock": 0x39,
    **{f"f{i}": 0x3A + i - 1 for i in range(1, 13)},
    "print_screen": 0x46, "scroll_lock": 0x47, "pause": 0x48, "insert": 0x49,
    "home": 0x4A, "page_up": 0x4B, "delete": 0x4C, "end": 0x4D, "page_down": 0x4E,
    "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52, "num_lock": 0x53,
    "menu": 0x65, **{f"f{i}": 0x68 + i - 13 for i in range(13, 25)},
    "ctrl": 0xE0, "shift": 0xE1, "alt": 0xE2, "cmd": 0xE3,
}
_RIGHT_MODIFIERS = {"ctrl": 0xE4, "shift": 0xE5, "alt": 0xE6, "cmd": 0xE7}
_HID_BASE = 0x700000000
_PAIR = re.compile(r"HIDKeyboardModifierMapping(Src|Dst)\s*=\s*(\d+)")


def hidutil_pairs(remaps: list[Remap]) -> list[tuple[int, int]]:
    """(source, destination) HID codes; a modifier source covers both sides."""
    pairs = []
    for r in remaps:
        for name in (r.source, r.target):
            if name not in HID_USAGES:
                raise ValueError(f"{name} cannot be remapped on macOS")
        target = _HID_BASE | HID_USAGES[r.target]
        pairs.append((_HID_BASE | HID_USAGES[r.source], target))
        if r.source in _RIGHT_MODIFIERS:
            pairs.append((_HID_BASE | _RIGHT_MODIFIERS[r.source], target))
    return pairs


def parse_hidutil_mapping(output: str) -> list[tuple[int, int]]:
    """Pairs from ``hidutil property --get UserKeyMapping`` (``(null)`` = none)."""
    pairs = []
    for block in output.split("{")[1:]:
        values = dict(_PAIR.findall(block))
        if "Src" in values and "Dst" in values:
            pairs.append((int(values["Src"]), int(values["Dst"])))
    return pairs


def hidutil_set_argument(pairs: list[tuple[int, int]]) -> str:
    """The ``--set`` argument, written like Apple's TN2450 (hexadecimal codes)."""
    items = ",".join(
        f'{{"HIDKeyboardModifierMappingSrc":{src:#x},"HIDKeyboardModifierMappingDst":{dst:#x}}}'
        for src, dst in pairs
    )
    return f'{{"UserKeyMapping":[{items}]}}'


Runner = Callable[[list[str]], str]


def _run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=True).stdout


class HidutilRemapper:
    name = "hidutil"

    def __init__(self, settings_get: Callable[[str, str], str], settings_set: Callable[[str, str], None],
                 run: Runner = _run) -> None:
        self._get, self._set, self._run = settings_get, settings_set, run
        self._active = False

    def unavailable_reason(self) -> str | None:
        return None

    def recover(self) -> None:
        """After a crash our mapping may still be active: put the saved one back."""
        saved = self._get(PREVIOUS_SETTING, "")
        if saved:
            self._write([tuple(p) for p in json.loads(saved)])
            self._set(PREVIOUS_SETTING, "")

    def apply(self, remaps: list[Remap]) -> dict[str, str]:
        try:
            ours = hidutil_pairs(remaps)
        except ValueError as exc:
            return {"*": str(exc)}
        if not ours:
            self.clear()
            return {}
        try:
            if not self._get(PREVIOUS_SETTING, ""):
                # remember what was there before us (another tool's mapping, usually none)
                previous = parse_hidutil_mapping(self._run(["hidutil", "property", "--get", "UserKeyMapping"]))
                self._set(PREVIOUS_SETTING, json.dumps(previous))  # "[]" when there was none
            previous = [tuple(p) for p in json.loads(self._get(PREVIOUS_SETTING, "[]"))]
            mine = {src for src, _ in ours}
            self._write([p for p in previous if p[0] not in mine] + ours)
            self._active = True
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            return {"*": f"hidutil failed: {exc}"}
        return {}

    def clear(self) -> None:
        saved = self._get(PREVIOUS_SETTING, "")
        if not saved and not self._active:
            return
        previous = [tuple(p) for p in json.loads(saved or "[]")]
        try:
            self._write(previous)
        except (OSError, subprocess.SubprocessError):
            return  # keep the saved mapping so the next start restores it
        self._set(PREVIOUS_SETTING, "")
        self._active = False

    def _write(self, pairs: list[tuple[int, int]]) -> None:
        self._run(["hidutil", "property", "--set", hidutil_set_argument(pairs)])


# ---------------------------------------------------------- Windows / Linux
class KeyboardPackageRemapper:
    name = "keyboard"

    def __init__(self) -> None:
        self._sources: list[str] = []

    def unavailable_reason(self) -> str | None:
        if sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() != 0:
            return ("Key remapping on Linux needs root (the keyboard package); as an "
                    "alternative, use a system tool such as keyd or xmodmap")
        try:
            import keyboard  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            return f"the keyboard package is not available: {exc}"
        return None

    def apply(self, remaps: list[Remap]) -> dict[str, str]:
        import keyboard

        self.clear()
        failures: dict[str, str] = {}
        for r in remaps:
            try:
                keyboard.remap_key(keys.to_keyboard_hotkey(r.source), keys.to_keyboard_hotkey(r.target))
                self._sources.append(r.source)
            except Exception as exc:  # noqa: BLE001
                failures[r.source] = str(exc) or "cannot be remapped"
        return failures

    def clear(self) -> None:
        if not self._sources:
            return
        import keyboard

        for source in self._sources:
            try:
                keyboard.unremap_key(keys.to_keyboard_hotkey(source))
            except (KeyError, ValueError):
                pass
        self._sources = []


class UnsupportedRemapper:
    name = "none"

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def unavailable_reason(self) -> str | None:
        return self._reason

    def apply(self, remaps: list[Remap]) -> dict[str, str]:
        return {"*": self._reason} if remaps else {}

    def clear(self) -> None:
        pass


def create_remap_backend(settings_get, settings_set, platform: str | None = None) -> RemapBackend:
    platform = platform or sys.platform
    if platform == "darwin":
        return HidutilRemapper(settings_get, settings_set)
    backend = KeyboardPackageRemapper()
    reason = backend.unavailable_reason()
    return UnsupportedRemapper(reason) if reason else backend
