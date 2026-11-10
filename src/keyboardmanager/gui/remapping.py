"""Key remapping tab: make one key act as another while Keyboard Manager runs."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from keyboardmanager.core.remap import Remap
from keyboardmanager.core.service import MacroService
from keyboardmanager.gui.widgets import KeyCaptureEdit
from keyboardmanager.gui.theme import set_tone


class RemapDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Remap a key")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.source = KeyCaptureEdit("key")
        self.target = KeyCaptureEdit("key")
        self.source.edit.setPlaceholderText("e.g. caps_lock")
        self.target.edit.setPlaceholderText("e.g. esc")
        form.addRow("When I press", self.source)
        form.addRow("act as", self.target)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def remap(self) -> Remap:
        """The entered remap; raises ``ValueError`` when a key is not valid."""
        return Remap(self.source.normalized(), self.target.normalized())


class RemapView(QWidget):
    def __init__(self, service: MacroService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        intro = QLabel(
            "Remapped keys act as another key everywhere, as long as Keyboard Manager is "
            "running. Letters and symbols mean the key at that position on a US keyboard."
        )
        intro.setWordWrap(True)
        self.enabled = QCheckBox("Remap keys")
        self.enabled.setChecked(service.remapping_enabled)
        self.enabled.toggled.connect(self._toggle)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(("When I press", "It acts as"))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.add_btn = QPushButton("Add...")
        self.remove_btn = QPushButton("Remove")
        self.add_btn.clicked.connect(self._add)
        self.remove_btn.clicked.connect(self._remove)
        self.status = QLabel()
        self.status.setWordWrap(True)

        buttons = QHBoxLayout()
        buttons.addWidget(self.add_btn)
        buttons.addWidget(self.remove_btn)
        buttons.addStretch(1)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.enabled)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)
        layout.addWidget(self.status)
        self.refresh()

    def refresh(self) -> None:
        remaps = self.service.remaps
        self.table.setRowCount(len(remaps))
        for row, remap in enumerate(remaps):
            self.table.setItem(row, 0, QTableWidgetItem(remap.source))
            self.table.setItem(row, 1, QTableWidgetItem(remap.target))
        self.remove_btn.setEnabled(bool(remaps))
        self.show_status()

    def show_status(self) -> None:
        reason = self.service.remapper.unavailable_reason()
        failures = self.service.remap_failures
        if reason:
            text, color = f"Not available here: {reason}.", "danger"
        elif failures:
            text = "Some remaps are not active: " + "; ".join(
                v if k == "*" else f"{k}: {v}" for k, v in failures.items())
            color = "danger"
        elif not self.service.remapping_enabled:
            text, color = "Remapping is switched off.", "secondary"
        elif self.service.remaps:
            text, color = f"{len(self.service.remaps)} remap(s) active.", "success"
        else:
            text, color = "No keys are remapped.", "secondary"
        self.status.setText(text)
        set_tone(self.status, color)

    # ----------------------------------------------------------------- actions
    def _toggle(self, on: bool) -> None:
        self.service.set_remapping_enabled(on)
        self.show_status()

    def _add(self) -> None:
        dialog = RemapDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.service.set_remaps([*self.service.remaps, dialog.remap()])
        except ValueError as exc:
            QMessageBox.warning(self, "Remap", str(exc))
            return
        self.refresh()

    def _remove(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        if not rows:
            return
        remaps = self.service.remaps
        for row in rows:
            if 0 <= row < len(remaps):
                del remaps[row]
        self.service.set_remaps(remaps)
        self.refresh()
