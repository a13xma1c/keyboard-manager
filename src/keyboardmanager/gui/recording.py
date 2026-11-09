"""Recording UI: options dialog and a session that hides the app while capturing."""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from keyboardmanager.core.models import Action
from keyboardmanager.core.recorder import RecordOptions, Recorder
from keyboardmanager.gui.widgets import KeyCaptureEdit


class RecordOptionsDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Record keyboard and mouse")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "All windows of this application are hidden while recording.\n"
                "Perform the actions you want to capture, then press the stop key."
            )
        )
        form = QFormLayout()
        layout.addLayout(form)
        self.stop_key = KeyCaptureEdit("key")
        self.stop_key.setText("f9")
        form.addRow("Stop key", self.stop_key)
        self.moves = QCheckBox("Record mouse movement")
        form.addRow("", self.moves)
        self.delays = QCheckBox("Record pauses between actions")
        self.delays.setChecked(True)
        form.addRow("", self.delays)
        self.min_delay = QSpinBox()
        self.min_delay.setRange(0, 60000)
        self.min_delay.setValue(50)
        self.min_delay.setSuffix(" ms")
        form.addRow("Ignore pauses shorter than", self.min_delay)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        start = buttons.addButton("Start recording", QDialogButtonBox.ButtonRole.AcceptRole)
        start.setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def options(self) -> RecordOptions:
        try:
            stop = self.stop_key.normalized() or "f9"
        except ValueError:
            stop = "f9"
        return RecordOptions(
            record_moves=self.moves.isChecked(),
            record_delays=self.delays.isChecked(),
            min_delay_ms=self.min_delay.value(),
            stop_key=stop,
        )


class _Overlay(QLabel):
    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setStyleSheet(
            "background:#b3261e;color:white;padding:8px 14px;font-weight:bold;border-radius:4px;"
        )

    def place(self) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        self.adjustSize()
        self.move(screen.right() - self.width() - 16, screen.top() + 16)


class RecordingSession(QObject):
    """Hides every window, records until the stop key and emits the result."""

    finished = Signal(list)  # list[Action]; empty when nothing was captured
    _stop_requested = Signal()
    _count_changed = Signal(int)

    START_DELAY_MS = 400  # lets the click on "Start" finish before listening

    def __init__(self, options: RecordOptions, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.options = options
        self._hidden: list[QWidget] = []
        self._overlay = _Overlay("")
        self._recorder = Recorder(options, on_stopped=self._stop_requested.emit, on_event=self._count_changed.emit)
        self._stop_requested.connect(self._stop)
        self._count_changed.connect(self._update_overlay)
        self._done = False

    def start(self) -> None:
        self._hidden = [w for w in QApplication.topLevelWidgets() if w.isVisible()]
        for w in self._hidden:
            w.hide()
        self._update_overlay(0)
        self._overlay.show()
        QTimer.singleShot(self.START_DELAY_MS, self._begin)

    def _begin(self) -> None:
        try:
            self._recorder.start()
        except Exception:  # noqa: BLE001 - missing OS permission, no display, ...
            self._finish([])
            raise

    def _update_overlay(self, count: int) -> None:
        key = self.options.stop_key.upper()
        self._overlay.setText(f"Recording ({count} events) - press {key} to stop")
        self._overlay.place()

    def _stop(self) -> None:
        if self._done:
            return
        self._finish(self._recorder.stop())

    def _finish(self, actions: list[Action]) -> None:
        self._done = True
        self._overlay.hide()
        for w in self._hidden:
            w.show()
        self.finished.emit(actions)
