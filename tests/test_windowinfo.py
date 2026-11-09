"""Active-window detection on Linux and Windows (simulated; macOS is in test_macos.py)."""

import ctypes
import subprocess
import sys
import types

import pytest

from keyboardmanager.core import windowinfo
from keyboardmanager.core.models import ActiveWindow


def test_linux_uses_xdotool_and_proc(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    answers = {"getwindowname": "notes.txt - gedit", "getwindowpid": "4242"}
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **kw: types.SimpleNamespace(stdout=answers[cmd[-1]] + "\n"),
    )
    real_open = open
    monkeypatch.setattr("builtins.open", lambda path, *a, **k: real_open(tmp_path / "comm", *a, **k)
                        if path == "/proc/4242/comm" else real_open(path, *a, **k))
    (tmp_path / "comm").write_text("gedit\n")
    assert windowinfo.get_active_window() == ActiveWindow("gedit", "notes.txt - gedit")


def test_linux_without_xdotool_never_raises(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    def missing(*a, **k):
        raise FileNotFoundError("xdotool")

    monkeypatch.setattr(subprocess, "run", missing)
    assert windowinfo.get_active_window() == ActiveWindow()


def test_linux_unknown_pid(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(
        stdout="Title" if cmd[-1] == "getwindowname" else "999999999"))
    assert windowinfo.get_active_window() == ActiveWindow("", "Title")


class _User32:
    def __init__(self, hwnd=7):
        self.hwnd = hwnd

    def GetForegroundWindow(self): return self.hwnd  # noqa: N802 - Win32 API names
    def GetWindowTextLengthW(self, hwnd): return 20  # noqa: N802

    def GetWindowTextW(self, hwnd, buf, n):  # noqa: N802
        buf.value = "Inbox - Outlook"

    def GetWindowThreadProcessId(self, hwnd, pid_ref):  # noqa: N802
        pid_ref._obj.value = 321


class _Kernel32:
    def __init__(self):
        self.closed = []

    def OpenProcess(self, access, inherit, pid): return 99 if pid == 321 else 0  # noqa: N802

    def QueryFullProcessImageNameW(self, handle, flags, path, size_ref):  # noqa: N802
        path.value = r"C:\Program Files\Microsoft Office\OUTLOOK.EXE"
        return 1

    def CloseHandle(self, handle): self.closed.append(handle)  # noqa: N802


@pytest.fixture
def windows(monkeypatch):
    pytest.importorskip("ctypes.wintypes")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(windowinfo.os.path, "basename", lambda p: p.replace("\\", "/").rsplit("/", 1)[-1])
    dlls = types.SimpleNamespace(user32=_User32(), kernel32=_Kernel32())
    monkeypatch.setattr(ctypes, "windll", dlls, raising=False)
    return dlls


def test_windows_foreground_window(windows):
    assert windowinfo.get_active_window() == ActiveWindow("OUTLOOK.EXE", "Inbox - Outlook")
    assert windows.kernel32.closed == [99]  # the process handle is always released


def test_windows_no_foreground_window(windows):
    windows.user32.hwnd = 0
    assert windowinfo.get_active_window() == ActiveWindow()


@pytest.mark.native
@pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")
def test_native_windows_lookup():
    assert isinstance(windowinfo.get_active_window(), ActiveWindow)
