"""The real application, real input listeners and a real event loop, in a child process."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

has_display = sys.platform in ("darwin", "win32") or bool(os.environ.get("DISPLAY"))


@pytest.mark.native
@pytest.mark.skipif(not has_display, reason="needs a desktop session (use xvfb-run on Linux)")
def test_toggling_hotkeys_and_schedules_and_recording_do_not_crash():
    env = dict(os.environ)
    if sys.platform == "darwin":
        env["QT_QPA_PLATFORM"] = "cocoa"  # the native platform, as when the app really runs
    result = subprocess.run(
        [sys.executable, "-X", "faulthandler", str(Path(__file__).with_name("native_smoke.py"))],
        capture_output=True, text=True, timeout=90, env=env,
    )
    output = result.stdout + result.stderr
    # a negative code is a signal: -11 (SIGSEGV), -5 (SIGTRAP) / -6 (SIGABRT) when macOS
    # kills it; the output then ends with the Python stack of every thread
    assert result.returncode == 0, f"exit code {result.returncode}\n{output[-6000:]}"
    # every step ran and shutdown completed (printed just before quitting, because on
    # macOS the process ends inside app.exec() and code after it never runs)
    assert "SMOKE-OK 7" in result.stdout, output[-3000:]
    assert "Traceback" not in output, output[-3000:]


@pytest.mark.native
@pytest.mark.skipif(not has_display, reason="needs a desktop session (use xvfb-run on Linux)")
def test_rapid_reregistration_with_real_listeners():
    """Real pynput listeners restarted back to back, as when toggles are clicked quickly:
    no hang, nothing raised to the caller, and the final registration is active."""
    import threading
    import time

    from keyboardmanager.core.hotkeys import create_hotkey_backend

    errors = []
    previous = threading.excepthook
    threading.excepthook = lambda args: errors.append(args.exc_value)
    try:
        hk = create_hotkey_backend("pynput")
        started = time.monotonic()
        for _ in range(25):
            failures = hk.start({"ctrl+alt+f12": lambda: None})
        assert failures == {} and hk._listener._ready
        assert time.monotonic() - started < 60
        hk.stop()
    finally:
        threading.excepthook = previous
    if sys.platform.startswith("linux"):
        # pynput's X11 listener occasionally loses its X connection while starting
        # (it is then retried); nothing else may go wrong in its threads
        errors = [e for e in errors if type(e).__name__ != "ConnectionClosedError"]
    assert errors == []
