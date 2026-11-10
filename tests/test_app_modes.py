"""Start modes: one instance only, --background, --headless, and the ``ctl`` command."""

import json
import socket

import pytest

from keyboardmanager import app as app_mod
from keyboardmanager.core import hotkeys
from keyboardmanager.core.database import Database
from keyboardmanager.core.models import Macro
from keyboardmanager.core import actions as catalog
from keyboardmanager.core.service import MacroService
from keyboardmanager.gui.instance import SingleInstance, server_name
from tests_support import FakeBackend, FakeHotkeys, wait_until


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def typing(name):
    a = catalog.new_action("type_text")
    a.params["text"] = name
    return Macro(name, [a])


# ------------------------------------------------------------ single instance
def test_second_start_reaches_the_first(qapp, tmp_path):
    name = server_name(str(tmp_path / "a.db"))
    assert name != server_name(str(tmp_path / "b.db"))  # one instance per database
    first = SingleInstance(name)
    assert not first.notify_running()  # nobody there yet
    assert first.listen()
    received = []
    first.message.connect(received.append)
    assert SingleInstance(name).notify_running("show")
    assert wait_until(lambda: (qapp.processEvents(), received)[1])
    assert received == ["show"]
    first.close()
    assert not SingleInstance(name).notify_running()


class _AppProxy:
    def __init__(self, real):
        self.real = real
        self.quit_handlers = []
        self.aboutToQuit = type("Signal", (), {"connect": lambda _, fn: self.quit_handlers.append(fn)})()

    def __call__(self, argv):
        return self

    def setApplicationName(self, name): pass  # noqa: N802
    def setQuitOnLastWindowClosed(self, on): pass  # noqa: N802
    def exec(self): return 0


@pytest.fixture
def started(qapp, monkeypatch, tmp_path, backend):
    created = {}
    monkeypatch.setattr(app_mod, "QApplication", _AppProxy(qapp))
    monkeypatch.setattr("keyboardmanager.core.backend.PynputBackend", lambda: backend)
    monkeypatch.setattr(hotkeys, "create_hotkey_backend", lambda kind: FakeHotkeys())
    real_window = __import__("keyboardmanager.gui.mainwindow", fromlist=["MainWindow"]).MainWindow

    def window(db, service):
        created["window"] = w = real_window(db, service)
        created["service"] = service
        return w

    monkeypatch.setattr("keyboardmanager.gui.mainwindow.MainWindow", window)
    created["db"] = tmp_path / "km.db"
    yield created
    if "window" in created:
        created["window"]._quitting = True
        created["window"].close()


def test_already_running_instance_is_shown_instead(started, qapp):
    other = SingleInstance(server_name(str(started["db"])))
    other.listen()
    got = []
    other.message.connect(got.append)
    assert app_mod.main(["--db", str(started["db"])]) == 0
    assert "window" not in started  # no second window, no second set of hotkeys
    assert wait_until(lambda: (qapp.processEvents(), got)[1]) and got == ["show"]
    other.close()


def test_background_start_keeps_the_window_hidden(started, monkeypatch):
    monkeypatch.setattr(app_mod.QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    assert app_mod.main(["--db", str(started["db"]), "--background"]) == 0
    assert not started["window"].isVisible()


def test_background_start_without_a_tray_shows_the_window(started, monkeypatch):
    monkeypatch.setattr(app_mod.QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: False))
    app_mod.main(["--db", str(started["db"]), "--background"])
    assert started["window"].isVisible()


def test_main_starts_remote_control_when_enabled(started):
    db = Database(started["db"])
    port = free_port()
    db.set_setting("remote_enabled", "1")
    db.set_setting("remote_port", str(port))
    db.close()
    app_mod.main(["--db", str(started["db"])])
    assert started["service"].remote is None  # started, then stopped again by the shutdown
    assert started["service"].closed


# ------------------------------------------------------------------ headless
def test_headless_runs_and_stops(qapp, tmp_path):
    lines = []
    code = app_mod.run_headless(str(tmp_path / "h.db"), backend=FakeBackend(), hotkeys=FakeHotkeys(),
                                stop_after_ms=300, out=lines.append)
    assert code == 0
    assert lines[0].startswith("Keyboard Manager") and "running headless" in lines[0]
    assert any(line == "  remote control: off" for line in lines)
    assert lines[-1] == "Stopped."


def test_headless_with_remote_control_and_hotkeys(qapp, tmp_path):
    path = str(tmp_path / "h.db")
    db = Database(path)
    port = free_port()
    for key, value in (("remote_enabled", "1"), ("remote_port", str(port))):
        db.set_setting(key, value)
    m = typing("hello")
    m.hotkey = "ctrl+1"
    db.save_macro(m)
    db.close()
    lines = []
    hk = FakeHotkeys()
    app_mod.run_headless(path, backend=FakeBackend(), hotkeys=hk, stop_after_ms=300, out=lines.append)
    assert any(f":{port}/" in line for line in lines)
    assert any("1 active" in line for line in lines)
    assert "ctrl+1" in hk.mapping and hk.stopped


def test_headless_refuses_a_second_instance(qapp, tmp_path):
    path = str(tmp_path / "h.db")
    other = SingleInstance(server_name(path))
    other.listen()
    lines = []
    assert app_mod.run_headless(path, backend=FakeBackend(), hotkeys=FakeHotkeys(), out=lines.append) == 1
    assert "already running" in lines[0]
    other.close()


def test_main_dispatches_headless(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(app_mod, "run_headless", lambda path, backend: seen.update(path=path, kind=backend) or 5)
    assert app_mod.main(["--headless", "--db", str(tmp_path / "x.db"), "--hotkey-backend", "pynput"]) == 5
    assert seen == {"path": str(tmp_path / "x.db"), "kind": "pynput"}


# ----------------------------------------------------------------------- ctl
@pytest.fixture
def running(tmp_path):
    """A running instance (service + remote control) on a database file."""
    path = tmp_path / "km.db"
    db = Database(path)
    service = MacroService(db, FakeBackend(), FakeHotkeys(), scheduler_thread=False)
    port = free_port()
    assert service.set_remote_settings(True, port, False) == ""
    db.save_macro(typing("Hello"))
    yield str(path), service
    service.shutdown()
    db.close()


def ctl(*argv):
    out, err = [], []
    code = app_mod.ctl_main(list(argv), out=out.append, err=err.append)
    return code, "\n".join(out), "\n".join(err)


def test_ctl_status_list_run_and_history(running):
    path, service = running
    code, text, _ = ctl("--db", path, "status")
    assert code == 0 and text.splitlines() == ["Idle", "Profile: All profiles", "Layer: base"]
    code, text, _ = ctl("--db", path, "list")
    assert text == "  Hello"
    code, text, _ = ctl("--db", path, "run", "hello")
    assert code == 0 and text == 'Started "Hello"'
    assert wait_until(lambda: service.running_macro is None and service.db.list_history())
    code, text, _ = ctl("--db", path, "history", "--limit", "1")
    assert "Hello  success  (remote)" in text
    code, text, _ = ctl("--db", path, "--json", "status")
    assert json.loads(text)["app"] == "Keyboard Manager"


def test_ctl_control_profile_layer_sync_and_url(running):
    path, service = running
    assert ctl("--db", path, "pause") == (0, "Nothing to do", "")
    assert ctl("--db", path, "profile", "Work")[1] == "Profile: Work"
    assert service.active_profile == "Work"
    assert ctl("--db", path, "layer", "nav", "--one-shot")[1] == "Layer: nav"
    assert ctl("--db", path, "layer")[1] == "Layer: base"
    assert ctl("--db", path, "sync")[1].startswith("Nothing to sync")
    code, text, _ = ctl("--db", path, "url")
    assert code == 0 and text == f"http://127.0.0.1:{service.remote.port}/?token={service.remote_token()}"


def test_ctl_errors(running, tmp_path):
    path, service = running
    code, _, err = ctl("--db", path, "run", "Missing")
    assert code == 1 and err.startswith("Error: no macro 'Missing'")
    assert ctl("--db", str(tmp_path / "none.db"), "status")[0] == 2
    service.set_remote_settings(False, service.remote_settings()["port"], False)
    code, _, err = ctl("--db", path, "status")
    assert code == 2 and "Remote control is off" in err


def test_ctl_when_the_instance_is_not_running(tmp_path):
    path = tmp_path / "km.db"
    db = Database(path)
    db.set_setting("remote_enabled", "1")
    db.set_setting("remote_port", str(free_port()))
    db.set_setting("remote_token", "t")
    db.close()
    code, _, err = ctl("--db", str(path), "status")
    assert code == 1 and "not reachable" in err


def test_main_routes_ctl(monkeypatch):
    monkeypatch.setattr(app_mod, "ctl_main", lambda argv: len(argv))
    assert app_mod.main(["ctl", "status", "--json"]) == 2
