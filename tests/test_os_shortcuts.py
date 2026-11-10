"""Warnings for hotkeys that clash with well-known system / application shortcuts."""

import pytest
from PySide6.QtWidgets import QInputDialog, QMessageBox

from keyboardmanager.core import keys, os_shortcuts
from keyboardmanager.core.models import Macro
from keyboardmanager.core.preflight import CheckContext, check_macro
from keyboardmanager.core.service import MacroService
from keyboardmanager.gui.mainwindow import MainWindow
from tests_support import FakeHotkeys


@pytest.mark.parametrize("hotkey,platform,kind,action", [
    ("cmd+space", "darwin", os_shortcuts.SYSTEM, "Spotlight"),
    ("cmd+shift+4", "darwin", os_shortcuts.SYSTEM, "a screenshot of an area"),
    ("cmd+c", "darwin", os_shortcuts.APP, "Copy"),
    ("ctrl+c", "win32", os_shortcuts.APP, "Copy"),
    ("alt+tab", "win32", os_shortcuts.SYSTEM, "switching windows"),
    ("cmd+l", "win32", os_shortcuts.SYSTEM, "locking the PC"),   # cmd = the Windows key
    ("cmd+v", "win32", os_shortcuts.SYSTEM, "clipboard history"),  # system beats app
    ("ctrl+alt+t", "linux", os_shortcuts.SYSTEM, "opening a terminal"),
    ("ctrl+s", "linux", os_shortcuts.APP, "Save"),
    ("ctrl+s ctrl+k", "linux", os_shortcuts.APP, "Save"),         # the first chord counts
])
def test_known_shortcuts(hotkey, platform, kind, action):
    found = os_shortcuts.find(hotkey, platform)
    assert (found.kind, found.action) == (kind, action)


@pytest.mark.parametrize("hotkey,platform", [
    ("ctrl+alt+k", "darwin"), ("ctrl+c", "darwin"),   # ctrl+c is not Copy on a Mac
    ("ctrl+k ctrl+s", "linux"), ("f13", "win32"), ("nonsense+", "linux"),
])
def test_free_hotkeys(hotkey, platform):
    assert os_shortcuts.find(hotkey, platform) is None and os_shortcuts.warning(hotkey, platform) is None


def test_every_table_entry_is_a_valid_hotkey():
    for table in (os_shortcuts._MAC, os_shortcuts._WINDOWS, os_shortcuts._LINUX):
        for hotkey in table:
            keys.normalize_hotkey(hotkey)


def test_warnings_explain_what_happens():
    assert "may act on it first" in os_shortcuts.warning("cmd+tab", "darwin")
    assert "would do that and also run the macro" in os_shortcuts.warning("ctrl+v", "linux")
    assert os_shortcuts.system_name("darwin") == "macOS" and os_shortcuts.system_name("linux") == "the desktop"


def test_preflight_warns():
    m = Macro("m", hotkey="ctrl+s", id=1)
    found = check_macro(m, CheckContext(macros=[m], platform="linux"))
    assert any(i.severity == "warning" and i.message.startswith("hotkey: ctrl+s is Save") for i in found)


@pytest.fixture
def win(qapp, db, backend, monkeypatch):
    monkeypatch.setattr(os_shortcuts.sys, "platform", "linux")
    service = MacroService(db, backend, FakeHotkeys(), scheduler_thread=False)
    window = MainWindow(db, service)
    yield window
    window.editor.dirty = False
    window._quitting = True
    window.close()
    service.shutdown()


def test_saving_asks_before_taking_a_known_shortcut(win, db, monkeypatch):
    questions = []

    def answer(button):
        monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: questions.append(a[2]) or button)

    win._new_macro()
    win.editor.hotkey_edit.setText("ctrl+c")
    answer(QMessageBox.StandardButton.No)
    assert not win.save_current() and "Copy" in questions[-1] and db.list_macros()[0].hotkey == ""
    answer(QMessageBox.StandardButton.Yes)
    assert win.save_current() and db.list_macros()[0].hotkey == "ctrl+c"
    asked = len(questions)
    win.editor.name_edit.setText("renamed")
    assert win.save_current() and len(questions) == asked  # not asked again for the same hotkey


def test_stop_and_pause_hotkeys_warn_too(win, monkeypatch):
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("alt+tab", True)))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    win._change_stop_hotkey()
    win._change_pause_hotkey()
    assert win.service.stop_hotkey == "ctrl+alt+f12" and win.service.pause_hotkey == "ctrl+alt+f11"
