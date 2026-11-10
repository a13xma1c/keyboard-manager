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
    QListWidget,
    QListWidgetItem,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from keyboardmanager.core.actions import describe
from keyboardmanager.core.models import Action
from keyboardmanager.core.preflight import estimate_duration
from keyboardmanager.core.recorder import RecordOptions, Recorder, RefineOptions, refine
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
        self.pause_key = KeyCaptureEdit("key")
        self.pause_key.setText("f8")
        self.pause_key.setToolTip("Pauses and resumes capturing; the paused time is not recorded")
        form.addRow("Pause key", self.pause_key)
        self.keyboard = QCheckBox("Record the keyboard")
        self.keyboard.setChecked(True)
        form.addRow("", self.keyboard)
        self.clicks = QCheckBox("Record mouse clicks and the wheel")
        self.clicks.setChecked(True)
        form.addRow("", self.clicks)
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
        def key(edit: KeyCaptureEdit, default: str) -> str:
            try:
                return edit.normalized() or default
            except ValueError:
                return default

        stop, pause = key(self.stop_key, "f9"), key(self.pause_key, "f8")
        return RecordOptions(
            record_moves=self.moves.isChecked(),
            record_delays=self.delays.isChecked(),
            min_delay_ms=self.min_delay.value(),
            stop_key=stop,
            pause_key=pause if pause != stop else "",  # the same key cannot do both
            record_keyboard=self.keyboard.isChecked(),
            record_clicks=self.clicks.isChecked(),
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
    _pause_changed = Signal(bool)

    START_DELAY_MS = 400  # lets the click on "Start" finish before listening

    def __init__(self, options: RecordOptions, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.options = options
        self._hidden: list[QWidget] = []
        self._overlay = _Overlay("")
        self._recorder = Recorder(options, on_stopped=self._stop_requested.emit, on_event=self._count_changed.emit)
        self._recorder.on_pause_changed = self._pause_changed.emit  # from the listener thread
        self._stop_requested.connect(self._stop)
        self._count_changed.connect(self._update_overlay)
        self._pause_changed.connect(self._on_pause_changed)
        self._count = 0
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

    def _on_pause_changed(self, _paused: bool) -> None:
        self._update_overlay(self._count)

    def _update_overlay(self, count: int) -> None:
        self._count = count
        stop, pause = self.options.stop_key.upper(), self.options.pause_key.upper()
        if self._recorder.paused:
            self._overlay.setText(f"Paused ({count} events) - {pause} resumes, {stop} stops")
        else:
            hint = f", {pause} pauses" if pause else ""
            self._overlay.setText(f"Recording ({count} events) - {stop} stops{hint}")
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


class RecordReviewDialog(QDialog):
    """Look over a recording before it is added: drop steps, tidy pauses and text."""

    def __init__(self, actions: list[Action], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Review the recording")
        self.setMinimumSize(520, 440)
        self._recorded = [a.clone() for a in actions]
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Untick the steps you do not want, and tidy up the rest:"))
        form = QFormLayout()
        self.max_pause = QSpinBox()
        self.max_pause.setRange(0, 600_000)
        self.max_pause.setSingleStep(250)
        self.max_pause.setSuffix(" ms")
        self.max_pause.setSpecialValueText("keep as recorded")
        form.addRow("Shorten pauses to at most", self.max_pause)
        self.no_pauses = QCheckBox("Remove all pauses")
        self.no_moves = QCheckBox("Remove mouse movement")
        self.join = QCheckBox("Join typed lines into one text (Enter becomes a line break)")
        for box in (self.no_pauses, self.no_moves, self.join):
            form.addRow("", box)
        layout.addLayout(form)
        self.list = QListWidget()
        layout.addWidget(self.list, 1)
        self.summary = QLabel()
        layout.addWidget(self.summary)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.add_btn = buttons.addButton("Add steps", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("Discard recording", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        for widget in (self.max_pause, self.no_pauses, self.no_moves, self.join):
            signal = widget.valueChanged if isinstance(widget, QSpinBox) else widget.toggled
            signal.connect(self._refresh)
        self.list.itemChanged.connect(self._update_summary)
        self._refresh()

    def refine_options(self) -> RefineOptions:
        return RefineOptions(
            max_pause_ms=self.max_pause.value(), remove_pauses=self.no_pauses.isChecked(),
            remove_moves=self.no_moves.isChecked(), join_lines=self.join.isChecked(),
        )

    def _refresh(self, *_args) -> None:
        self.max_pause.setEnabled(not self.no_pauses.isChecked())
        self._steps = refine(self._recorded, self.refine_options())
        self.list.blockSignals(True)
        self.list.clear()
        for step in self._steps:
            item = QListWidgetItem(describe(step))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.list.addItem(item)
        self.list.blockSignals(False)
        self._update_summary()

    def _update_summary(self, *_args) -> None:
        kept = self.actions()
        seconds, _ = estimate_duration(kept)
        took = f", about {seconds:.1f} s of pauses" if seconds else ""
        self.summary.setText(f"{len(kept)} of {len(self._steps)} steps will be added{took}.")
        self.add_btn.setEnabled(bool(kept))

    def actions(self) -> list[Action]:
        """The steps to add: refined, and only those still ticked."""
        return [step for i, step in enumerate(self._steps)
                if self.list.item(i).checkState() == Qt.CheckState.Checked]
