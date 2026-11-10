"""One running Keyboard Manager per user and database.

Two instances would register every hotkey twice and both run schedules. A second
start therefore asks the running one to show its window and exits. Uses a
``QLocalServer`` (a named pipe on Windows, a socket file elsewhere); works with a
``QCoreApplication`` too, so the headless mode takes part.
"""

from __future__ import annotations

import getpass
import hashlib
import os

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

TIMEOUT_MS = 500


def server_name(db_path: str) -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - no user name available
        user = "user"
    key = f"{user}:{os.path.abspath(db_path)}"
    return "keyboardmanager-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


class SingleInstance(QObject):
    message = Signal(str)  # what another start sent ("show")

    def __init__(self, name: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.name = name
        self._server: QLocalServer | None = None

    def notify_running(self, message: str = "show") -> bool:
        """True when another instance is running (it received ``message``)."""
        socket = QLocalSocket()
        socket.connectToServer(self.name)
        if not socket.waitForConnected(TIMEOUT_MS):
            return False
        socket.write(message.encode("utf-8"))
        socket.flush()
        socket.waitForBytesWritten(TIMEOUT_MS)
        socket.disconnectFromServer()
        return True

    def listen(self) -> bool:
        """Become the running instance (after ``notify_running`` said there is none)."""
        QLocalServer.removeServer(self.name)  # a socket file left behind by a crash
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_connection)
        return self._server.listen(self.name)

    def close(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None

    def _on_connection(self) -> None:
        # Read synchronously: the other start writes one short message right away.
        # (No per-socket signal connections: the socket is deleted right after.)
        while self._server is not None and self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if not socket.bytesAvailable():
                socket.waitForReadyRead(TIMEOUT_MS)
            data = bytes(socket.readAll()).decode("utf-8", "replace").strip()
            socket.disconnectFromServer()
            socket.deleteLater()
            if data:
                self.message.emit(data)
