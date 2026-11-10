"""Execution history tab."""

from __future__ import annotations

from datetime import datetime
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QAbstractItemView,
    QHeaderView,
)

from keyboardmanager.core.database import Database
from keyboardmanager.gui.theme import set_tone, tone_color

COLUMNS = ("Started", "Macro", "Trigger", "Status", "Duration", "Steps", "Message")
STATUS_TONES = {"success": "success", "error": "danger", "stopped": "warning", "running": "info", "skipped": "secondary"}


def format_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return iso


def format_duration(ms: int) -> str:
    return f"{ms} ms" if ms < 1000 else f"{ms / 1000:.1f} s"


STEP_COLUMNS = ("At", "Step", "Took", "Result")
STEP_TONES = {"ok": "success", "error": "danger", "stopped": "warning"}
ROLE_HISTORY_ID = Qt.ItemDataRole.UserRole


class HistoryView(QWidget):
    def __init__(self, db: Database, parent: QWidget | None = None,
                 log_steps: tuple[Callable[[], bool], Callable[[bool], None]] | None = None) -> None:
        """``log_steps``: (get, set) of the "log each step" setting, if it can be changed."""
        super().__init__(parent)
        self.db = db
        self.status_filter = QComboBox()
        self.status_filter.addItems(["All", "success", "stopped", "error", "skipped", "running"])
        self.status_filter.currentIndexChanged.connect(self.refresh)
        self.summary = QLabel()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        clear = QPushButton("Clear history")
        clear.clicked.connect(self._clear)

        top = QHBoxLayout()
        top.addWidget(QLabel("Status:"))
        top.addWidget(self.status_filter)
        top.addWidget(self.summary, 1)
        self.log_check = QCheckBox("Log each step")
        self.log_check.setToolTip("Record every step of a run (at most 2000 per run), shown below a selected run")
        if log_steps is not None:
            self.log_check.setChecked(log_steps[0]())
            self.log_check.toggled.connect(log_steps[1])
            top.addWidget(self.log_check)
        top.addWidget(refresh)
        top.addWidget(clear)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(len(COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)

        self.steps = QTreeWidget()
        self.steps.setHeaderLabels(STEP_COLUMNS)
        self.steps.setRootIsDecorated(True)
        self.steps.setAlternatingRowColors(True)
        self.steps.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.steps.header().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.steps_label = QLabel("Select a run to see its steps.")
        set_tone(self.steps_label, "secondary")
        below = QWidget()
        below_layout = QVBoxLayout(below)
        below_layout.setContentsMargins(0, 0, 0, 0)
        below_layout.addWidget(self.steps_label)
        below_layout.addWidget(self.steps, 1)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.table)
        split.addWidget(below)
        split.setSizes([300, 200])
        self.table.itemSelectionChanged.connect(self._show_steps)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(split, 1)
        self.refresh()

    def refresh(self) -> None:
        status = self.status_filter.currentText()
        entries = self.db.list_history(500, status=None if status == "All" else status)
        self.table.setRowCount(len(entries))
        for row, e in enumerate(entries):
            values = (
                format_time(e.started_at), e.macro_name, e.trigger, e.status,
                format_duration(e.duration_ms), str(e.actions_run), e.message,
            )
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setData(ROLE_HISTORY_ID, e.id)
                if col == 3:
                    item.setForeground(QBrush(tone_color(STATUS_TONES.get(e.status, "secondary"))))
                self.table.setItem(row, col, item)
        ok = sum(1 for e in entries if e.status == "success")
        self.summary.setText(f"  {len(entries)} runs shown, {ok} successful")

    def _show_steps(self) -> None:
        """The step log of the selected run, nested like the macro's steps."""
        rows = {i.row() for i in self.table.selectedItems()}
        self.steps.clear()
        if len(rows) != 1:
            self.steps_label.setText("Select a run to see its steps.")
            return
        history_id = self.table.item(rows.pop(), 0).data(ROLE_HISTORY_ID)
        logged = self.db.history_steps(history_id)
        if not logged:
            self.steps_label.setText("No steps were logged for this run.")
            return
        self.steps_label.setText(f"{len(logged)} logged step(s):")
        parents: list[QTreeWidgetItem] = []
        for step in logged:
            item = QTreeWidgetItem([
                f"+{format_duration(step['offset_ms'])}", step["description"],
                format_duration(step["duration_ms"]),
                step["status"] + (f" - {step['detail']}" if step["detail"] else ""),
            ])
            item.setForeground(3, QBrush(tone_color(STEP_TONES.get(step["status"], "secondary"))))
            del parents[step["depth"]:]
            (parents[-1].addChild(item) if parents else self.steps.addTopLevelItem(item))
            parents.append(item)
        self.steps.expandAll()

    def _clear(self) -> None:
        answer = QMessageBox.question(self, "Clear history", "Delete the entire execution history?")
        if answer == QMessageBox.StandardButton.Yes:
            self.db.clear_history()
            self.refresh()
