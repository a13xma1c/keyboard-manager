"""Run the real application briefly and exercise what used to crash on macOS.

Started as a separate process by test_native_smoke.py: a native crash (macOS kills
the process for calling main-thread-only APIs from a background thread) must fail
the test instead of taking pytest down. Uses the real pynput backend, hotkey
listeners and Qt event loop; only the permission dialog is skipped.
"""

import faulthandler
import sys
import tempfile
from pathlib import Path

# On a native crash (e.g. a segmentation fault) print every thread's Python stack,
# so a failure says where it happened instead of just "exit code -11".
faulthandler.enable(all_threads=True)


def log(message: str) -> None:
    print(f"[smoke] {message}", file=sys.stderr, flush=True)


from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from keyboardmanager.core import permissions
from keyboardmanager.core.backend import PynputBackend
from keyboardmanager.core.database import Database
from keyboardmanager.core.hotkeys import create_hotkey_backend
from keyboardmanager.core.models import Macro
from keyboardmanager.core.recorder import Recorder, RecordOptions
from keyboardmanager.core.service import MacroService
from keyboardmanager.gui.mainwindow import MainWindow

TOGGLES = 6

permissions.missing_permissions = lambda: []  # no modal dialog in an unattended run
log("creating the application")
app = QApplication(sys.argv[:1])
db = Database(Path(tempfile.mkdtemp()) / "smoke.db")
# a text snippet, so the expander's keyboard + mouse listeners start and stop with the
# "Global hotkeys" toggle as well (no remaps: tests never change the real keyboard)
db.save_macro(Macro("snippet", abbreviation=";smoke"))
service = MacroService(db, PynputBackend(), create_hotkey_backend("auto"))
window = MainWindow(db, service)
app.aboutToQuit.connect(service.shutdown)  # as in keyboardmanager.app.main
window.show()
log("window shown, hotkeys registered")
steps: list[str] = []


def step(n: int = 0) -> None:
    if n < TOGGLES:  # what the user clicked: re-registers the global hotkey listener
        log(f"toggle {n + 1}: global hotkeys")
        window.hotkeys_action.trigger()
        log(f"toggle {n + 1}: auto-run")
        window.autorun_action.trigger()
        steps.append("toggle")
        QTimer.singleShot(150, lambda: step(n + 1))
        return
    log("recording: start")
    recorder = Recorder(RecordOptions())  # recording starts keyboard + mouse listeners too
    recorder.start()
    QTimer.singleShot(300, lambda: stop_recording(recorder))


def stop_recording(recorder: Recorder) -> None:
    log("recording: stop")
    recorder.stop()
    steps.append("record")
    finish()


def finish() -> None:
    log("shutting down")
    window._quitting = True
    service.shutdown()
    # Report success here: on macOS quitting ends the process from inside the event
    # loop (AppKit calls exit()), so nothing after app.exec() is guaranteed to run.
    print(f"SMOKE-OK {len(steps)}", flush=True)
    app.quit()


QTimer.singleShot(300, step)
QTimer.singleShot(20_000, lambda: (print("TIMEOUT", flush=True), app.exit(3)))
code = app.exec()
log(f"event loop finished ({code})")  # not reached on macOS, see finish()
db.close()
sys.exit(code)
