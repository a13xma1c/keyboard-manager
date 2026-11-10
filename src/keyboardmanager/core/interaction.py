"""What a running macro may ask of the user interface (no Qt here).

Macros run on a worker thread; the GUI provides an implementation that performs
these calls on the GUI thread (see ``gui.uibridge``). Without a GUI the headless
implementation is used.
"""

from __future__ import annotations

from typing import Protocol

from keyboardmanager.core.vision import Image, Screen


class UserInterface(Protocol):
    def ask(self, prompt: str, default: str) -> str | None:
        """Ask for a text; ``None`` when the user cancels."""

    def get_clipboard(self) -> str: ...

    def set_clipboard(self, text: str) -> None: ...

    def notify(self, title: str, message: str) -> None: ...

    def grab_screen(self) -> Screen | None:
        """A capture of the (primary) screen; ``None`` when there is no screen."""

    def load_image(self, path: str) -> Image:
        """Decode an image file; raises ``ValueError`` when it cannot be read."""


class HeadlessInterface:
    """No windows: questions cannot be asked, the clipboard is kept in memory."""

    def __init__(self) -> None:
        self.clipboard = ""
        self.notifications: list[tuple[str, str]] = []

    def ask(self, prompt: str, default: str) -> str | None:
        raise RuntimeError("cannot ask for input without the user interface")

    def get_clipboard(self) -> str:
        return self.clipboard

    def set_clipboard(self, text: str) -> None:
        self.clipboard = text

    def notify(self, title: str, message: str) -> None:
        self.notifications.append((title, message))

    def grab_screen(self) -> Screen | None:
        return None  # no screen to look at

    def load_image(self, path: str) -> Image:
        raise ValueError("image files can only be read while the application is running")
