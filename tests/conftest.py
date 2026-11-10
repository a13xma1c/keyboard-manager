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


class FakeRemapper:
    name = "fake"

    def __init__(self):
        self.applied: list = []
        self.cleared = 0

    def unavailable_reason(self):
        return None

    def apply(self, remaps):
        self.applied.append(list(remaps))
        return {}

    def clear(self):
        self.cleared += 1


@pytest.fixture(autouse=True)
def no_real_remapping(monkeypatch):
    """Tests must never remap the real keyboard (hidutil on a developer's Mac)."""
    from keyboardmanager.core import service

    made: list[FakeRemapper] = []
    monkeypatch.setattr(service, "create_remap_backend",
                        lambda *a, **k: made.append(FakeRemapper()) or made[-1])
    return made


@pytest.fixture(autouse=True)
def no_process_exit(monkeypatch):
    """On macOS the main window ends the process when the app quits; tests run that
    code too (also on real Macs), so record the request instead of exiting."""
    from keyboardmanager.gui import mainwindow

    exits: list[int] = []
    monkeypatch.setattr(mainwindow, "EXIT_PROCESS", exits.append)
    return exits


@pytest.fixture(autouse=True)
def release_keyboard_modifiers():
    """Qt keeps the modifiers of the last key event it saw: a test ending with e.g.
    keyClick(..., Ctrl) would leave Ctrl "held" for the next one, which changes what
    clicking / selecting does (Ctrl+click adds to a selection). Reset after each test."""
    yield
    from PySide6.QtWidgets import QApplication

    if QApplication.instance() is None:
        return
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QWidget

    target = QWidget()
    QTest.keyRelease(target, Qt.Key.Key_Shift, Qt.KeyboardModifier.NoModifier)
    target.deleteLater()


@pytest.fixture(autouse=True)
def dispose_windows():
    """Delete the windows a test left behind. Undeleted windows pile up over the
    suite: every later style change, polish and event would pass through all of
    them (a main window also watches every event of the application)."""
    yield
    if "PySide6.QtWidgets" not in sys.modules:
        return
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        return
    app.processEvents()  # callbacks still pending (zero timers) run while their widgets exist
    for widget in app.topLevelWidgets():
        widget.hide()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


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
