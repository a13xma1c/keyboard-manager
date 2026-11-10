"""Check tab: the pre-flight check of every macro, with a way to jump to each problem."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from keyboardmanager.core.backend import key_available
from keyboardmanager.core.models import Macro
from keyboardmanager.core.preflight import ERROR, INFO, WARNING, CheckContext, Issue, check_all
from keyboardmanager.core.service import MacroService
from keyboardmanager.gui.theme import set_tone, tone_color

TONES = {ERROR: "danger", WARNING: "warning", INFO: "secondary"}
ROLE_ISSUE = Qt.ItemDataRole.UserRole


class PreflightView(QWidget):
    navigate = Signal(int, tuple)  # macro id, step path

    def __init__(self, service: MacroService, macros: Callable[[], list[Macro]],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self._macros = macros
        self.issues: list[Issue] = []
        intro = QLabel("What would go wrong (or surprise you) when the macros run. "
                       "Double-click a line to open the macro at that step.")
        intro.setWordWrap(True)
        self.show_info = QCheckBox("Show information (run times, disabled macros)")
        self.show_info.toggled.connect(self.refresh)
        refresh = QPushButton("Check again")
        refresh.clicked.connect(self.refresh)
        self.summary = QLabel()
        top = QHBoxLayout()
        top.addWidget(self.summary, 1)
        top.addWidget(self.show_info)
        top.addWidget(refresh)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(("", "Macro", "Step", "Problem"))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.itemDoubleClicked.connect(self._open)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(top)
        layout.addWidget(self.table, 1)
        self.refresh()

    def context(self, macros: list[Macro] | None = None) -> CheckContext:
        return CheckContext(
            macros=macros if macros is not None else self._macros(),
            reserved_hotkeys=self.service.reserved_hotkeys(),
            saved_variables=set(self.service.saved_variables.all()),
            key_supported=key_available,
            playback_speed=self.service.playback_speed,
            now=self.service.clock,
            triggers=self.service.db.list_triggers(),
        )

    def refresh(self, *_args) -> None:
        macros = self._macros()
        everything = check_all(macros, self.context(macros))
        self.issues = [i for i in everything if i.severity != INFO or self.show_info.isChecked()]
        icons = {ERROR: QStyle.StandardPixmap.SP_MessageBoxCritical,
                 WARNING: QStyle.StandardPixmap.SP_MessageBoxWarning,
                 INFO: QStyle.StandardPixmap.SP_MessageBoxInformation}
        self.table.setRowCount(len(self.issues))
        for row, issue in enumerate(self.issues):
            mark = QTableWidgetItem(self.style().standardIcon(icons[issue.severity]), issue.severity)
            mark.setData(ROLE_ISSUE, issue)
            mark.setForeground(QBrush(tone_color(TONES[issue.severity])))
            step = f"{_step_number(issue.step)}  {issue.step_text}" if issue.step else ""
            for col, item in enumerate((mark, QTableWidgetItem(issue.macro_name), QTableWidgetItem(step),
                                        QTableWidgetItem(issue.message))):
                self.table.setItem(row, col, item)
        errors = sum(1 for i in everything if i.severity == ERROR)
        warnings = sum(1 for i in everything if i.severity == WARNING)
        affected = len({i.macro_id for i in everything if i.severity in (ERROR, WARNING)})
        if errors or warnings:
            self.summary.setText(f"{errors} problem(s) and {warnings} warning(s) in {affected} macro(s)")
            set_tone(self.summary, "danger" if errors else "warning")
        else:
            self.summary.setText(f"No problems found in {len(macros)} macro(s)")
            set_tone(self.summary, "success")

    def _open(self, item: QTableWidgetItem) -> None:
        issue: Issue = self.table.item(item.row(), 0).data(ROLE_ISSUE)
        if issue.macro_id is not None:
            self.navigate.emit(issue.macro_id, tuple(issue.step))


def _step_number(path: tuple) -> str:
    """(2, "children", 0) -> "3.1"."""
    return ".".join(str(p + 1) for p in path if isinstance(p, int))
