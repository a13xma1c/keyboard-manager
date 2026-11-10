"""Opening apps, files and URLs on each platform (commands checked, nothing launched)."""

import pytest

from keyboardmanager.core import launcher


@pytest.mark.parametrize("platform,target,exists,expected", [
    ("darwin", "https://example.com", False, ["open", "https://example.com"]),
    ("darwin", "Safari", False, ["open", "-a", "Safari"]),          # an application name
    ("darwin", "/tmp/report.pdf", True, ["open", "/tmp/report.pdf"]),
    ("linux", "https://example.com", False, ["xdg-open", "https://example.com"]),
    ("linux", "mailto:a@b.c", False, ["xdg-open", "mailto:a@b.c"]),
    ("linux", "gedit notes.txt", False, ["gedit", "notes.txt"]),     # a command line
    ("linux", "/tmp/x.txt", True, ["xdg-open", "/tmp/x.txt"]),
    ("win32", "notepad", False, None),                               # os.startfile
])
def test_launch_command(platform, target, exists, expected):
    assert launcher.launch_command(target, platform, exists=lambda p: exists) == expected


def test_launch_command_expands_home_and_refuses_empty(monkeypatch):
    monkeypatch.setenv("HOME", "/home/ada")
    assert launcher.launch_command("~/notes.txt", "linux", exists=lambda p: p == "/home/ada/notes.txt") == [
        "xdg-open", "/home/ada/notes.txt"]
    with pytest.raises(ValueError):
        launcher.launch_command("  ", "linux")


def test_launch_starts_without_waiting(monkeypatch):
    started = []
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda cmd, **kw: started.append((cmd, kw)))
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    launcher.launch("https://example.com")
    cmd, kw = started[0]
    assert cmd == ["xdg-open", "https://example.com"] and kw["start_new_session"] is True


def test_launch_on_windows_uses_startfile(monkeypatch):
    opened = []
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    monkeypatch.setattr(launcher.os, "startfile", opened.append, raising=False)
    launcher.launch(" notepad ")
    assert opened == ["notepad"]
