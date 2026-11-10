"""Bringing another application's window to the front (best effort, never raises).

The "Focus window" step asks the operating system to activate a window matching an
application name and/or a window title (both: case-insensitive substrings, the
application as in the editor's *Application* filter). Whether it worked is checked
by the caller by looking at the focused window afterwards.

* macOS    - activates the running application via AppKit (titles cannot be chosen:
  the application decides which of its windows comes to the front).
* Windows  - finds a visible top-level window by title and process name, restores
  it when minimised and sets it to the foreground.
* Linux    - asks ``xdotool`` (X11) to activate a window by name / class.
"""

from __future__ import annotations

import os
import subprocess
import sys


def activate(app: str = "", title: str = "") -> bool:
    """Try to focus a window matching ``app`` / ``title``; True when a request was made."""
    app, title = app.strip().lower(), title.strip().lower()
    if not app and not title:
        return False
    try:
        if sys.platform == "darwin":
            return _macos(app)
        if sys.platform == "win32":
            return _windows(app, title)
        return _linux(app, title)
    except Exception:  # noqa: BLE001 - "could not" is an answer, not a crash
        return False


def _macos(app: str) -> bool:
    if not app:
        return False  # macOS activates applications, not single windows
    from AppKit import NSApplicationActivateIgnoringOtherApps, NSWorkspace  # type: ignore[import-not-found]

    for running in NSWorkspace.sharedWorkspace().runningApplications():
        name = str(running.localizedName() or "")
        if app in name.lower():
            running.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
            return True
    return False


def _windows(app: str, title: str) -> bool:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def on_window(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if not buf.value:
            return True
        if title and title not in buf.value.lower():
            return True
        if app and app not in _process_name(hwnd).lower():
            return True
        found.append(hwnd)
        return False  # stop enumerating

    user32.EnumWindows(on_window, 0)
    if not found:
        return False
    user32.ShowWindow(found[0], 9)  # SW_RESTORE: also un-minimises
    user32.SetForegroundWindow(found[0])
    return True


def _process_name(hwnd) -> str:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        path = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
            return os.path.basename(path.value)
    finally:
        kernel32.CloseHandle(handle)
    return ""


def _linux(app: str, title: str) -> bool:
    def run(*args: str) -> bool:
        result = subprocess.run(["xdotool", *args], capture_output=True, text=True, timeout=2, check=False)
        return result.returncode == 0  # non-zero: no match, or no xdotool / no X11

    if title and run("search", "--name", title, "windowactivate"):
        return True
    return bool(app) and run("search", "--class", app, "windowactivate")
