"""Active-window detection for application-specific shortcuts and conditions."""

from __future__ import annotations

import os
import subprocess
import sys

from keyboardmanager.core.models import ActiveWindow


def get_active_window() -> ActiveWindow:
    """Best-effort lookup of the focused window; never raises."""
    try:
        if sys.platform == "darwin":
            return _macos()
        if sys.platform == "win32":
            return _windows()
        return _linux()
    except Exception:
        return ActiveWindow()


def _macos() -> ActiveWindow:
    from AppKit import NSWorkspace  # type: ignore[import-not-found]

    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    if app is None:
        return ActiveWindow()
    name = str(app.localizedName() or "")
    pid = app.processIdentifier()
    title = ""
    try:
        import Quartz  # type: ignore[import-not-found]

        options = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
        for win in Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []:
            if win.get("kCGWindowOwnerPID") == pid and win.get("kCGWindowLayer") == 0:
                # Titles need the Screen Recording permission; empty otherwise.
                title = str(win.get("kCGWindowName") or "")
                if title:
                    break
    except Exception:
        pass
    return ActiveWindow(app=name, title=title)


def _windows() -> ActiveWindow:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ActiveWindow()
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    app = ""
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if handle:
        try:
            size = wintypes.DWORD(1024)
            path = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                app = os.path.basename(path.value)
        finally:
            kernel32.CloseHandle(handle)
    return ActiveWindow(app=app, title=buf.value)


def _run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=1, check=False).stdout.strip()


def _linux() -> ActiveWindow:
    title = _run(["xdotool", "getactivewindow", "getwindowname"])
    pid = _run(["xdotool", "getactivewindow", "getwindowpid"])
    app = ""
    if pid.isdigit():
        try:
            with open(f"/proc/{pid}/comm", encoding="utf-8") as fh:
                app = fh.read().strip()
        except OSError:
            pass
    return ActiveWindow(app=app, title=title)
