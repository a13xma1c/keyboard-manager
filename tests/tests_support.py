"""Test doubles and helpers shared by the test modules (importable, unlike conftest)."""

from __future__ import annotations

import time

from keyboardmanager.core.models import ActiveWindow


class FakeBackend:
    """Records every call instead of touching the OS."""

    def __init__(self, window: ActiveWindow | None = None) -> None:
        self.calls: list[tuple] = []
        self.window = window or ActiveWindow("Terminal", "bash")
        self.pos = (100, 100)

    def key_down(self, key): self.calls.append(("down", key))
    def key_up(self, key): self.calls.append(("up", key))
    def type_text(self, text): self.calls.append(("type", text))
    def mouse_position(self): return self.pos
    def mouse_move(self, x, y): self.calls.append(("move", x, y)); self.pos = (x, y)
    def mouse_down(self, button): self.calls.append(("mdown", button))
    def mouse_up(self, button): self.calls.append(("mup", button))
    def mouse_click(self, button, count): self.calls.append(("click", button, count))
    def mouse_scroll(self, dx, dy): self.calls.append(("scroll", dx, dy))
    def active_window(self): return self.window


class FakeHotkeys:
    name = "fake"

    def __init__(self, failures=None):
        self.mapping = {}
        self.stopped = False
        self.failures = failures or {}

    def start(self, mapping):
        self.mapping = dict(mapping)
        return {k: v for k, v in self.failures.items() if k in mapping}

    def stop(self):
        self.stopped = True


def wait_until(predicate, timeout: float = 3.0, interval: float = 0.005) -> bool:
    """Poll ``predicate`` until true or ``timeout``; returns its final value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())
