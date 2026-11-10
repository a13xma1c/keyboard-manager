"""Text expansion: typing a macro's abbreviation (e.g. ``;sig``) runs the macro,
after deleting the abbreviation that was just typed.

The typed text is observed (never intercepted): the abbreviation reaches the
application, then the service sends one Backspace per character and runs the
macro, typically a single "Type text" step. Matching is exact (case-sensitive)
and happens as soon as the last character is typed, so abbreviations usually
start with a character rarely typed otherwise (``;``, ``/``, ``,,``).

What was typed is tracked from key presses, so anything that may move the cursor
forgets it: non-character keys (arrows, Enter, Tab, Escape...), whitespace,
shortcuts with Ctrl / Cmd, and mouse clicks. Backspace removes the last
character. Keys sent by Keyboard Manager itself are ignored.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

MIN_LENGTH, MAX_LENGTH = 2, 32
BUFFER_SIZE = 64
_SHORTCUT_MODIFIERS = ("ctrl", "cmd")


def normalize_abbreviation(text: str) -> str:
    """The abbreviation to match, or ``""``; raises ``ValueError`` when unusable."""
    abbreviation = text.strip()
    if not abbreviation:
        return ""
    if not MIN_LENGTH <= len(abbreviation) <= MAX_LENGTH:
        raise ValueError(f"an abbreviation needs {MIN_LENGTH} to {MAX_LENGTH} characters")
    if any(ch.isspace() or not ch.isprintable() for ch in abbreviation):
        raise ValueError("an abbreviation cannot contain spaces or special characters")
    return abbreviation


def abbreviations_conflict(a: str, b: str) -> bool:
    """Typing one would always trigger the other first (equal, or one starts the other)."""
    return a.startswith(b) or b.startswith(a)


class TextExpander:
    """Watches typing and reports an abbreviation as soon as it has been typed."""

    def __init__(self, on_match: Callable[[str], None]) -> None:
        self._on_match = on_match
        self._lock = threading.Lock()
        self._abbreviations: list[str] = []  # longest first: ";sig2" before "sig2"
        self._typed = ""
        self._held: set[str] = set()
        self._listeners: list[Any] = []

    @property
    def abbreviations(self) -> list[str]:
        return list(self._abbreviations)

    def update(self, abbreviations: set[str], listen: bool = True) -> None:
        """Set what to watch for; input listeners run only while there is something."""
        with self._lock:
            self._abbreviations = sorted(abbreviations, key=len, reverse=True)
            self._typed = ""
        if abbreviations and listen:
            self.start()
        elif not abbreviations:
            self.stop()

    # ------------------------------------------------------------- typing
    def key(self, name: str | None, char: str | None, pressed: bool, injected: bool = False) -> None:
        """One key event: ``name`` is the canonical key name, ``char`` what it typed."""
        if injected:  # our own keystrokes (e.g. the expansion being typed)
            return
        if name in _SHORTCUT_MODIFIERS:
            (self._held.add if pressed else self._held.discard)(name)
            return
        if not pressed or name in ("shift", "alt"):
            return
        match = None
        with self._lock:
            if self._held:  # a shortcut such as Ctrl+V, not typing
                self._typed = ""
            elif name == "backspace":
                self._typed = self._typed[:-1]
            elif char and len(char) == 1 and char.isprintable() and not char.isspace():
                self._typed = (self._typed + char)[-BUFFER_SIZE:]
                match = next((a for a in self._abbreviations if self._typed.endswith(a)), None)
                if match:
                    self._typed = ""
            else:  # Enter, Tab, arrows, Space...: the cursor may be elsewhere now
                self._typed = ""
        if match:
            self._on_match(match)

    def click(self) -> None:
        """A mouse click may move the cursor: forget what was typed."""
        with self._lock:
            self._typed = ""

    # ------------------------------------------------------------ listeners
    def start(self) -> None:
        if self._listeners:
            return
        from pynput import keyboard, mouse

        from keyboardmanager.core.backend import pynput_key_to_name, safe_listener, start_listener

        def on_key(pressed: bool):
            def handler(key, injected=False):
                char = getattr(key, "char", None)
                self.key(pynput_key_to_name(key), char if isinstance(char, str) else None, pressed, injected)
            return handler

        def on_click(x, y, button, pressed, injected=False):
            if pressed and not injected:
                self.click()

        self._listeners = [
            safe_listener(keyboard.Listener)(on_press=on_key(True), on_release=on_key(False)),
            safe_listener(mouse.Listener)(on_click=on_click),
        ]
        for listener in self._listeners:
            listener.daemon = True
            start_listener(listener)

    def stop(self) -> None:
        from keyboardmanager.core.backend import stop_listener

        listeners, self._listeners = self._listeners, []
        for listener in listeners:
            stop_listener(listener)
        self._held.clear()
