"""Remote control (real HTTP on a free local port) and start at login (per platform)."""

import json
import plistlib
import socket
import sys
import urllib.request

import pytest

from keyboardmanager.core import actions as catalog
from keyboardmanager.core import autostart as autostart_mod
from keyboardmanager.core.autostart import Autostart, launch_command
from keyboardmanager.core.models import Macro
from keyboardmanager.core.remote import RemoteClient, RemoteError, RemoteServer
from keyboardmanager.core.service import MacroService
from tests_support import FakeHotkeys, wait_until

TOKEN = "secret-token"


def A(t, **params):
    a = catalog.new_action(t)
    a.params.update(params)
    return a


def typing(name, **kw):
    return Macro(name, [A("type_text", text=name)], **kw)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def svc(db, backend):
    service = MacroService(db, backend, FakeHotkeys(), scheduler_thread=False)
    yield service
    service.shutdown()


@pytest.fixture
def server(svc):
    srv = RemoteServer(svc, TOKEN, port=0)
    yield srv
    srv.stop()


@pytest.fixture
def client(server):
    return RemoteClient(server.port, TOKEN)


def raw(server, method, path, body=None, headers=None):
    """A request without the client (to test what the client would never send)."""
    request = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", data=body, method=method,
                                     headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers


def typed(backend):
    return [c[1] for c in backend.calls if c[0] == "type"]


# ------------------------------------------------------------------ security
def test_requests_need_the_token(server):
    status, body, _ = raw(server, "GET", "/api/status")
    assert status == 401 and json.loads(body)["error"] == "missing or wrong access token"
    status, _, _ = raw(server, "GET", "/api/status", headers={"Authorization": "Bearer wrong"})
    assert status == 401
    status, _, _ = raw(server, "GET", "/")  # the panel too
    assert status == 401
    status, body, headers = raw(server, "GET", f"/?token={TOKEN}")
    assert status == 200 and b"<title>Keyboard Manager</title>" in body
    assert headers["Content-Type"].startswith("text/html") and headers["Cache-Control"] == "no-store"


def test_bad_requests(server):
    auth = {"Authorization": f"Bearer {TOKEN}"}
    status, body, _ = raw(server, "POST", "/api/run", b"{not json", auth)
    assert status == 400 and "JSON" in json.loads(body)["error"]
    status, _, _ = raw(server, "POST", "/api/run", b"[1]", auth)
    assert status == 400
    status, _, _ = raw(server, "POST", "/api/run", b"{}", auth)
    assert status == 400
    status, _, _ = raw(server, "POST", "/api/run", b"x" * (64 * 1024 + 1), auth)
    assert status == 413
    status, _, _ = raw(server, "GET", "/api/nothing", None, auth)
    assert status == 404
    status, _, _ = raw(server, "GET", "/api/history?limit=abc", None, auth)
    assert status == 400


def test_listens_only_locally_by_default(svc):
    srv = RemoteServer(svc, TOKEN, port=0)
    try:
        assert srv.host == "127.0.0.1" and not srv.lan
    finally:
        srv.stop()


# ----------------------------------------------------------------------- api
def test_status_macros_and_history(client, svc, db):
    db.save_macro(typing("beta"))
    db.save_macro(typing("Alpha", favorite=True, hotkey="ctrl+1"))
    status = client.call("GET", "/api/status")
    assert status["running"] is None and status["queue"] == [] and status["profile"] == ""
    macros = client.call("GET", "/api/macros")
    assert [m["name"] for m in macros] == ["Alpha", "beta"]  # favorites first
    assert macros[0]["hotkey"] == "ctrl+1" and macros[0]["active"]
    svc.run(svc.find_macro("beta"), blocking=True)
    history = client.call("GET", "/api/history?limit=5")
    assert history[0]["macro"] == "beta" and history[0]["status"] == "success"


def test_run_by_name_or_id(client, svc, db, backend):
    m = db.save_macro(typing("Hello"))
    assert client.call("POST", "/api/run", {"macro": "hello"}) == {"ok": True, "macro": "Hello", "queued": False}
    assert wait_until(lambda: svc.running_macro is None and typed(backend) == ["Hello"])
    client.call("POST", "/api/run", {"macro": m.id})
    assert wait_until(lambda: typed(backend) == ["Hello", "Hello"])
    assert db.list_history()[0].trigger == "remote"
    with pytest.raises(RemoteError, match="no macro 'Nope'"):
        client.call("POST", "/api/run", {"macro": "Nope"})


def test_run_refusals(client, svc, db):
    db.save_macro(typing("Off", enabled=False))
    db.save_macro(typing("Work only", profiles=["Work"]))
    svc.set_active_profile("Home")
    with pytest.raises(RemoteError, match="disabled"):
        client.call("POST", "/api/run", {"macro": "Off"})
    with pytest.raises(RemoteError, match="not in the active profile"):
        client.call("POST", "/api/run", {"macro": "Work only"})


def test_run_while_busy_and_queueing(client, svc, db, backend):
    db.save_macro(Macro("Long", [A("delay", ms=300)]))
    db.save_macro(typing("Next"))
    client.call("POST", "/api/run", {"macro": "Long"})
    with pytest.raises(RemoteError, match="another macro is running"):
        client.call("POST", "/api/run", {"macro": "Next"})
    assert client.call("POST", "/api/run", {"macro": "Next", "queue": True})["queued"] is True
    assert client.call("GET", "/api/status")["queue"] == ["Next"]
    assert wait_until(lambda: typed(backend) == ["Next"])


def test_remote_runs_respect_limits(client, svc, db):
    from keyboardmanager.core.limits import Limits

    db.save_macro(typing("Once", limits=Limits(max_runs_per_day=1)))
    client.call("POST", "/api/run", {"macro": "Once"})
    assert wait_until(lambda: svc.running_macro is None)
    client.call("POST", "/api/run", {"macro": "Once"})  # handled, but skipped
    assert db.list_history()[0].status == "skipped"


def test_favorites_only_scope(client, svc, db):
    db.save_macro(typing("Fav", favorite=True))
    db.save_macro(typing("Other"))
    db.set_setting("remote_scope", "favorites")
    assert [m["name"] for m in client.call("GET", "/api/macros")] == ["Fav"]
    with pytest.raises(RemoteError, match="may be run remotely"):
        client.call("POST", "/api/run", {"macro": "Other"})


def test_stop_pause_resume(client, svc, db):
    db.save_macro(Macro("Long", [A("delay", ms=5000)]))
    client.call("POST", "/api/run", {"macro": "Long"})
    assert wait_until(lambda: svc.live_state() is not None)
    assert client.call("POST", "/api/pause")["paused"] is True
    assert client.call("GET", "/api/status")["paused"] is True
    assert client.call("POST", "/api/resume")["paused"] is False
    assert client.call("POST", "/api/toggle-pause")["paused"] is True
    client.call("POST", "/api/stop")
    assert wait_until(lambda: svc.running_macro is None)
    assert db.list_history()[0].status == "stopped"
    assert client.call("POST", "/api/pause")["changed"] is False  # nothing to pause


def test_profile_layer_and_sync(client, svc, tmp_path):
    assert client.call("POST", "/api/profile", {"name": "Work"}) == {"ok": True, "profile": "Work"}
    assert svc.active_profile == "Work"
    with pytest.raises(RemoteError, match="comma"):
        client.call("POST", "/api/profile", {"name": "a,b"})
    with pytest.raises(RemoteError, match="must be text"):
        client.call("POST", "/api/profile", {"name": 3})
    assert client.call("POST", "/api/layer", {"name": "nav", "one_shot": True})["layer"] == "nav"
    with pytest.raises(RemoteError, match="must be text"):
        client.call("POST", "/api/layer", {})
    result = client.call("POST", "/api/sync")
    assert result["ok"] and result["summary"].startswith("Nothing to sync")
    folder = tmp_path / "f"
    folder.mkdir()
    svc.set_sync_options(str(folder), "pc")
    assert client.call("POST", "/api/sync")["summary"].startswith("Sync:")


def test_unreachable_server():
    with pytest.raises(RemoteError, match="not reachable"):
        RemoteClient(free_port(), TOKEN, timeout=1).call("GET", "/api/status")


# ------------------------------------------------------- service settings
def test_service_starts_and_stops_the_server(svc):
    port = free_port()
    assert svc.remote is None and svc.remote_settings()["enabled"] is False
    token = svc.remote_token()
    assert token and svc.remote_token() == token  # made once, then kept
    assert svc.set_remote_settings(True, port, False, "all") == ""
    assert svc.remote is not None and svc.remote.port == port
    assert RemoteClient(port, token).call("GET", "/api/status")["app"] == "Keyboard Manager"
    assert svc.remote_url() == f"http://127.0.0.1:{port}/?token={token}"
    new = svc.regenerate_remote_token()
    assert new != token
    with pytest.raises(RemoteError, match="token"):
        RemoteClient(port, token).call("GET", "/api/status")
    assert RemoteClient(port, new).call("GET", "/api/status")["app"]
    svc.set_remote_settings(False, port, False)
    assert svc.remote is None
    with pytest.raises(RemoteError):
        RemoteClient(port, new, timeout=1).call("GET", "/api/status")


def test_busy_port_is_reported_not_raised(svc):
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        error = svc.set_remote_settings(True, port, False)
    assert error.startswith(f"cannot listen on port {port}") and svc.remote is None


def test_remote_settings_validation(svc):
    with pytest.raises(ValueError, match="port"):
        svc.set_remote_settings(True, 0, False)
    with pytest.raises(ValueError, match="scope"):
        svc.set_remote_settings(True, 8000, False, "everything")


def test_shutdown_stops_the_server(db, backend):
    service = MacroService(db, backend, FakeHotkeys(), scheduler_thread=False)
    port = free_port()
    service.set_remote_settings(True, port, False)
    service.shutdown()
    assert service.remote is None
    with pytest.raises(RemoteError):
        RemoteClient(port, service.remote_token(), timeout=1).call("GET", "/api/status")
    assert service.apply_remote() == "" and service.remote is None  # nothing starts after shutdown


# ------------------------------------------------------------- autostart
def test_launch_command(monkeypatch):
    assert launch_command() == [sys.executable, "-m", "keyboardmanager", "--background"]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert launch_command(["--background", "--db", "x"]) == [sys.executable, "--background", "--db", "x"]


def test_autostart_macos(tmp_path):
    auto = Autostart(platform="darwin", home=tmp_path)
    assert not auto.is_enabled() and auto.command() is None
    auto.enable(["/Applications/KeyboardManager.app/Contents/MacOS/KeyboardManager", "--background"])
    assert auto.is_enabled()
    path = tmp_path / "Library" / "LaunchAgents" / "com.keyboardmanager.app.plist"
    data = plistlib.loads(path.read_bytes())
    assert data["RunAtLoad"] is True and data["ProgramArguments"][-1] == "--background"
    assert auto.command()[-1] == "--background" and auto.location == str(path)
    auto.disable()
    assert not auto.is_enabled()
    auto.disable()  # already off: fine


def test_autostart_linux(tmp_path):
    auto = Autostart(platform="linux", home=tmp_path, env={"XDG_CONFIG_HOME": str(tmp_path / "cfg")})
    auto.enable(["/usr/bin/python3", "-m", "keyboardmanager", "--background", "--db", "/my data/km.db"])
    path = tmp_path / "cfg" / "autostart" / "keyboardmanager.desktop"
    assert path.exists() and "Exec=/usr/bin/python3 -m keyboardmanager --background --db '/my data/km.db'" \
        in path.read_text()
    assert auto.command() == ["/usr/bin/python3", "-m", "keyboardmanager", "--background", "--db", "/my data/km.db"]
    auto.disable()
    assert not auto.is_enabled()
    default = Autostart(platform="linux", home=tmp_path, env={})
    assert default.location == str(tmp_path / ".config" / "autostart" / "keyboardmanager.desktop")


class FakeRegistry:
    HKEY_CURRENT_USER, REG_SZ, KEY_SET_VALUE = "HKCU", 1, 2

    def __init__(self):
        self.values = {}

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def CreateKey(self, root, path):  # noqa: N802 - winreg API
        return self._Key()

    def OpenKey(self, root, path, reserved=0, access=0):  # noqa: N802
        return self._Key()

    def SetValueEx(self, key, name, reserved, kind, value):  # noqa: N802
        self.values[name] = value

    def QueryValueEx(self, key, name):  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def DeleteValue(self, key, name):  # noqa: N802
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


def test_autostart_windows():
    registry = FakeRegistry()
    auto = Autostart(platform="win32", registry=registry)
    assert not auto.is_enabled()
    auto.enable([r"C:\Program Files\KeyboardManager\KeyboardManager.exe", "--background"])
    assert registry.values["KeyboardManager"] == '"C:\\Program Files\\KeyboardManager\\KeyboardManager.exe" --background'
    assert auto.is_enabled()
    assert auto.command() == [r"C:\Program Files\KeyboardManager\KeyboardManager.exe", "--background"]
    assert auto.location.endswith(r"CurrentVersion\Run\KeyboardManager")
    auto.disable()
    assert not auto.is_enabled()
    auto.disable()


def test_autostart_uses_winreg_on_windows(monkeypatch, fake_modules):
    registry = FakeRegistry()
    fake_modules(winreg={name: getattr(registry, name) for name in (
        "HKEY_CURRENT_USER", "REG_SZ", "KEY_SET_VALUE", "CreateKey", "OpenKey", "SetValueEx",
        "QueryValueEx", "DeleteValue")})
    auto = Autostart(platform="win32")
    auto.enable(["km.exe"])
    assert registry.values == {"KeyboardManager": "km.exe"}
    assert autostart_mod.RUN_VALUE == "KeyboardManager"
