"""The user interface a running macro may use, executed on the GUI thread.

Macros run on a worker thread, but dialogs and the clipboard belong to the GUI
thread. Each call is therefore sent to the GUI thread with a blocking queued
connection and the worker waits for the answer; calls made on the GUI thread
itself run directly (a blocking call to the own thread would deadlock).
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import QApplication, QInputDialog, QSystemTrayIcon, QWidget


class QtUserInterface(QObject):
    _call = Signal(object)

    def __init__(self, window: QWidget, notify: Callable[[str, str], None] | None = None) -> None:
        super().__init__(window)
        self._window = window
        self._notify = notify
        self._call.connect(self._run, Qt.ConnectionType.BlockingQueuedConnection)

    # ----------------------------------------------------------- dispatch
    @Slot(object)
    def _run(self, job: list) -> None:
        function, args, box = job
        try:
            box["result"] = function(*args)
        except Exception as exc:  # noqa: BLE001 - re-raised on the calling thread
            box["error"] = exc

    def _on_gui(self, function: Callable[..., Any], *args: Any) -> Any:
        if threading.current_thread() is threading.main_thread():
            return function(*args)
        box: dict[str, Any] = {}
        self._call.emit([function, args, box])  # blocks until the GUI thread has run it
        if "error" in box:
            raise box["error"]
        return box.get("result")

    # ------------------------------------------------------ the interface
    def ask(self, prompt: str, default: str) -> str | None:
        return self._on_gui(self._ask, prompt, default)

    def get_clipboard(self) -> str:
        return self._on_gui(lambda: QApplication.clipboard().text())

    def set_clipboard(self, text: str) -> None:
        self._on_gui(lambda: QApplication.clipboard().setText(text))

    def notify(self, title: str, message: str) -> None:
        self._on_gui(self._show_notification, title, message)

    def grab_screen(self):
        from keyboardmanager.gui import capture

        return self._on_gui(capture.grab_screen)

    def load_image(self, path: str):
        from keyboardmanager.gui import capture

        return self._on_gui(capture.load_image, path)

    # ----------------------------------------------------------- GUI side
    def _ask(self, prompt: str, default: str) -> str | None:
        text, ok = QInputDialog.getText(self._window, "Keyboard Manager", prompt, text=default)
        return text if ok else None

    def _show_notification(self, title: str, message: str) -> None:
        if self._notify is not None:
            self._notify(title, message)
        else:  # pragma: no cover - the main window always passes one
            QSystemTrayIcon().showMessage(title, message)
