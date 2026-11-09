"""Execution history tab."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QComboBox,
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

COLUMNS = ("Started", "Macro", "Trigger", "Status", "Duration", "Steps", "Message")
STATUS_COLORS = {"success": "#2e7d32", "error": "#c62828", "stopped": "#ef6c00", "running": "#1565c0", "skipped": "#757575"}


def format_time(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return iso


def format_duration(ms: int) -> str:
    return f"{ms} ms" if ms < 1000 else f"{ms / 1000:.1f} s"


class HistoryView(QWidget):
    def __init__(self, db: Database, parent: QWidget | None = None) -> None:
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

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.table, 1)
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
                if col == 3:
                    item.setForeground(QBrush(QColor(STATUS_COLORS.get(e.status, "#000000"))))
                self.table.setItem(row, col, item)
        ok = sum(1 for e in entries if e.status == "success")
        self.summary.setText(f"  {len(entries)} runs shown, {ok} successful")

    def _clear(self) -> None:
        answer = QMessageBox.question(self, "Clear history", "Delete the entire execution history?")
        if answer == QMessageBox.StandardButton.Yes:
            self.db.clear_history()
            self.refresh()
