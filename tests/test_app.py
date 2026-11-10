"""Application entry point: argument handling, start-up, shutdown and start-up failures."""

import runpy
import sys

import pytest

from keyboardmanager import app as app_mod
from keyboardmanager.core import hotkeys
from tests_support import FakeHotkeys


class _AppProxy:
    """Reuses the test QApplication; exec() returns immediately."""

    def __init__(self, real, code=0):
        self.real, self.code, self.name = real, code, None
        self.quit_handlers = []
        self.aboutToQuit = type("Signal", (), {"connect": lambda _, fn: self.quit_handlers.append(fn)})()

    def __call__(self, argv):
        self.argv = argv
        return self

    def setApplicationName(self, name): self.name = name  # noqa: N802
    def setQuitOnLastWindowClosed(self, on): self.quit_on_close = on  # noqa: N802
    def exec(self): return self.code


@pytest.fixture
def started(qapp, monkeypatch, tmp_path, backend):
    """Run main() with fakes; returns what it created."""
    created = {}
    proxy = _AppProxy(qapp)
    monkeypatch.setattr(app_mod, "QApplication", proxy)
    monkeypatch.setattr("keyboardmanager.core.backend.PynputBackend", lambda: backend)

    def make_hotkeys(kind):
        created["kind"] = kind
        created["hotkeys"] = FakeHotkeys()
        return created["hotkeys"]

    monkeypatch.setattr(hotkeys, "create_hotkey_backend", make_hotkeys)
    real_window = __import__("keyboardmanager.gui.mainwindow", fromlist=["MainWindow"]).MainWindow

    def window(db, service):
        created["window"] = w = real_window(db, service)
        created["service"] = service
        return w

    monkeypatch.setattr("keyboardmanager.gui.mainwindow.MainWindow", window)
    created["proxy"], created["db"] = proxy, tmp_path / "data" / "km.db"
    return created


def test_main_starts_and_shuts_down(started):
    code = app_mod.main(["--db", str(started["db"]), "--hotkey-backend", "pynput", "-style", "fusion"])
    assert code == 0
    assert started["db"].exists() and started["kind"] == "pynput"
    assert started["proxy"].name == "Keyboard Manager" and started["proxy"].quit_on_close is False
    assert started["proxy"].argv[1:] == ["-style", "fusion"]  # unknown args go to Qt
    assert started["hotkeys"].stopped  # service.shutdown() ran
    # macOS may end the process inside exec(), so cleanup must also hang off aboutToQuit
    assert started["proxy"].quit_handlers == [started["service"].shutdown]
    assert "ctrl+alt+f12" in started["hotkeys"].mapping
    started["window"].close()


def test_main_reports_start_up_failure(started, monkeypatch, no_modal_boxes):
    def broken(path):
        raise PermissionError("read-only disk")

    monkeypatch.setattr(app_mod, "Database", broken)
    assert app_mod.main(["--db", str(started["db"])]) == 1
    kind, args = no_modal_boxes[0]
    assert kind == "critical" and "read-only disk" in args[2]


def test_main_rejects_unknown_backend(started):
    with pytest.raises(SystemExit):
        app_mod.main(["--hotkey-backend", "magic"])


def test_python_dash_m_entry_point(monkeypatch):
    monkeypatch.setattr(app_mod, "main", lambda: 7)
    monkeypatch.delitem(sys.modules, "keyboardmanager.__main__", raising=False)
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("keyboardmanager", run_name="__main__")
    assert exit_info.value.code == 7
