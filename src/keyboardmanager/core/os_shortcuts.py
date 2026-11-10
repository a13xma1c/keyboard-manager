"""Well-known shortcuts of the operating systems, to warn before a hotkey clashes.

Two kinds:

* ``system`` - the operating system (or desktop) acts on it, often before any
  application sees it: the macro may never run, or both happen (Cmd+Tab, Win+L).
* ``app``    - a standard shortcut in almost every application (Copy, Save...).
  Keyboard Manager observes hotkeys rather than intercepting them, so pressing it
  would do both: the usual action *and* run the macro.

Only the first chord of a sequence matters: that is what reaches the system first.
Key names are canonical: on macOS ``cmd`` is Command and ``alt`` Option, on Windows
``cmd`` is the Windows key. The lists cover the usual defaults, not every setup.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from keyboardmanager.core import keys

SYSTEM, APP = "system", "app"


@dataclass(frozen=True)
class OsShortcut:
    hotkey: str
    action: str
    kind: str  # SYSTEM or APP

    def explain(self, system_name: str) -> str:
        if self.kind == SYSTEM:
            return (f"{self.hotkey} is {system_name}'s shortcut for {self.action}: the system may "
                    "act on it first, so the macro may not run (or both happen)")
        return (f"{self.hotkey} is {self.action} in most applications: pressing it would do that "
                "and also run the macro")


_COMMON_APP = {  # ctrl on Windows / Linux, cmd on macOS
    "c": "Copy", "v": "Paste", "x": "Cut", "z": "Undo", "y": "Redo", "a": "Select all",
    "s": "Save", "o": "Open", "n": "New", "p": "Print", "f": "Find", "w": "Close window / tab",
    "t": "New tab", "r": "Reload", "l": "Go to the address bar", "q": "Quit",
}

_MAC = {
    "cmd+space": "Spotlight", "cmd+tab": "switching applications", "cmd+shift+tab": "switching applications",
    "cmd+`": "switching windows", "cmd+h": "Hide", "cmd+alt+h": "Hide others", "cmd+m": "Minimize",
    "cmd+q": "Quit", "cmd+alt+esc": "Force Quit", "ctrl+cmd+q": "Lock Screen", "ctrl+cmd+f": "full screen",
    "cmd+shift+3": "a screenshot", "cmd+shift+4": "a screenshot of an area", "cmd+shift+5": "the screenshot tool",
    "ctrl+up": "Mission Control", "ctrl+down": "Application windows", "ctrl+left": "the previous space",
    "ctrl+right": "the next space", "cmd+shift+q": "Log Out", "ctrl+space": "switching input sources",
    "cmd+,": "Settings", "cmd+shift+z": "Redo", "cmd+alt+d": "hiding the Dock", "ctrl+cmd+space": "Emoji & Symbols",
}
_WINDOWS = {
    "ctrl+alt+delete": "the security screen", "ctrl+shift+esc": "Task Manager", "alt+tab": "switching windows",
    "alt+shift+tab": "switching windows", "alt+f4": "closing the window", "cmd+d": "showing the desktop",
    "cmd+e": "File Explorer", "cmd+l": "locking the PC", "cmd+r": "Run", "cmd+tab": "Task View",
    "cmd+i": "Settings", "cmd+a": "Quick Settings", "cmd+s": "Search", "cmd+x": "the quick link menu",
    "cmd+v": "clipboard history", "cmd+shift+s": "a screenshot", "cmd+.": "the emoji panel",
    "cmd+up": "maximizing", "cmd+down": "minimizing", "cmd+left": "snapping left", "cmd+right": "snapping right",
    "cmd+m": "minimizing all windows", "cmd+p": "display mode", "cmd+k": "Cast", "cmd+h": "dictation",
    "cmd+u": "Accessibility", "cmd+space": "switching keyboard layouts", "alt+space": "the window menu",
    "print_screen": "a screenshot", "ctrl+alt+tab": "switching windows",
}
_LINUX = {
    "ctrl+alt+delete": "logging out", "alt+tab": "switching windows", "alt+shift+tab": "switching windows",
    "alt+f4": "closing the window", "alt+f2": "running a command", "ctrl+alt+t": "opening a terminal",
    "cmd+l": "locking the screen", "ctrl+alt+l": "locking the screen", "cmd+d": "showing the desktop",
    "cmd+a": "showing applications", "cmd+tab": "switching applications", "cmd+up": "maximizing",
    "cmd+down": "restoring the window", "cmd+left": "tiling left", "cmd+right": "tiling right",
    "ctrl+alt+left": "the previous workspace", "ctrl+alt+right": "the next workspace",
    "ctrl+alt+up": "the previous workspace", "ctrl+alt+down": "the next workspace",
    "cmd+space": "switching input sources", "print_screen": "a screenshot", "alt+space": "the window menu",
}
_SYSTEM_NAMES = {"darwin": "macOS", "win32": "Windows"}


def _table(platform: str) -> dict[str, OsShortcut]:
    system = {"darwin": _MAC, "win32": _WINDOWS}.get(platform, _LINUX)
    modifier = "cmd" if platform == "darwin" else "ctrl"
    table = {f"{modifier}+{key}": OsShortcut(f"{modifier}+{key}", action, APP) for key, action in _COMMON_APP.items()}
    for hotkey, action in system.items():  # system shortcuts take precedence
        canonical = keys.normalize_hotkey(hotkey)
        table[canonical] = OsShortcut(canonical, action, SYSTEM)
    return table


def system_name(platform: str | None = None) -> str:
    return _SYSTEM_NAMES.get(platform or sys.platform, "the desktop")


def find(hotkey: str, platform: str | None = None) -> OsShortcut | None:
    """The well-known shortcut a hotkey (or the first chord of a sequence) clashes with."""
    try:
        first = keys.sequence_chords(hotkey)[0]
    except (ValueError, IndexError):
        return None
    return _table(platform or sys.platform).get(first)


def warning(hotkey: str, platform: str | None = None) -> str | None:
    """A sentence explaining the clash, or ``None``."""
    shortcut = find(hotkey, platform)
    return shortcut.explain(system_name(platform)) if shortcut else None
