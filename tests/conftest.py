"""Shared fixtures.

* Everything runs headless: Qt uses the offscreen platform and no test touches real
  input devices (``FakeBackend`` records calls; pynput objects are never started).
* Platform-specific code (macOS, Windows, Linux) is exercised on any OS by
  simulating ``sys.platform`` and the native modules (see ``fake_modules``); tests
  marked ``native`` additionally run against the real OS APIs where available.
"""

from __future__ import annotations

import os
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from keyboardmanager.core.database import Database  # noqa: E402
from tests_support import FakeBackend, FakeHotkeys, wait_until  # noqa: E402,F401


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def db():
    database = Database(":memory:")
    yield database
    database.close()


@pytest.fixture
def fake_modules(monkeypatch):
    """Install stand-in modules: ``fake_modules(AppKit={...}, Quartz={...})``."""

    def install(**modules: dict) -> dict[str, types.ModuleType]:
        created = {}
        for name, attrs in modules.items():
            module = types.ModuleType(name)
            for key, value in attrs.items():
                setattr(module, key, value)
            monkeypatch.setitem(sys.modules, name, module)
            created[name] = module
        return created

    return install


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def no_modal_boxes(monkeypatch):
    """No test may block on a dialog: message boxes are recorded and answer Yes."""
    from PySide6.QtWidgets import QMessageBox

    shown: list[tuple] = []
    for name in ("warning", "information", "critical", "question"):
        monkeypatch.setattr(
            QMessageBox, name,
            lambda *a, _n=name, **k: shown.append((_n, a)) or QMessageBox.StandardButton.Yes,
        )
    return shown
