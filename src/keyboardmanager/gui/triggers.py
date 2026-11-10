"""Triggers tab: rules that run a macro when something happens."""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from keyboardmanager.core.service import MacroService
from keyboardmanager.core.triggers import FINISH_RESULTS, KINDS, TriggerRule, validate_rule
from keyboardmanager.gui.theme import set_tone

COLUMNS = ("On", "Name", "When", "Run", "Condition", "Last")


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    set_tone(label, "secondary")
    return label


class TriggerDialog(QDialog):
    def __init__(self, rule: TriggerRule | None, macro_names: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Trigger")
        self.setMinimumWidth(520)
        self._id = rule.id if rule is not None else None
        rule = rule or TriggerRule(name="", kind="macro_finished", macro="")
        p = rule.params

        self.name_edit = QLineEdit(rule.name)
        self.kind_combo = QComboBox()
        for kind, label in KINDS.items():
            self.kind_combo.addItem(label, kind)
        self.kind_combo.setCurrentIndex(max(0, self.kind_combo.findData(rule.kind)))

        # one page of parameters per kind
        self.pages = QStackedWidget()
        self.after_combo = self._macro_combo(macro_names, str(p.get("macro", "")))
        self.result_combo = QComboBox()
        self.result_combo.addItems(FINISH_RESULTS)
        self.result_combo.setCurrentText(str(p.get("result", "any")))
        self.contains_edit = QLineEdit(str(p.get("contains", "")))
        self.contains_edit.setPlaceholderText("optional: only when the new text contains this")
        self.path_edit = QLineEdit(str(p.get("path", "")))
        self.path_edit.setPlaceholderText("e.g. ~/Downloads/report.csv")
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        self.expression_edit = QLineEdit(str(p.get("expression", "")))
        self.expression_edit.setPlaceholderText('e.g. "Invoice" in window_title   or   counter >= 10')
        for kind in KINDS:
            page = QWidget()
            form = QFormLayout(page)
            form.setContentsMargins(0, 0, 0, 0)
            if kind == "macro_finished":
                form.addRow("Macro", self.after_combo)
                form.addRow("Result", self.result_combo)
            elif kind == "clipboard":
                form.addRow("Contains", self.contains_edit)
            elif kind == "file":
                row = QHBoxLayout()
                row.addWidget(self.path_edit, 1)
                row.addWidget(browse)
                form.addRow("File", row)
                form.addRow("", _hint("Fires when the file is created, changed or deleted."))
            else:
                form.addRow("Expression", self.expression_edit)
                form.addRow("", _hint("Checked every second; fires when it changes from false to true. "
                                      "Variables: the saved ones and the built-ins."))
            self.pages.addWidget(page)
        self.kind_combo.currentIndexChanged.connect(self.pages.setCurrentIndex)
        self.pages.setCurrentIndex(self.kind_combo.currentIndex())

        self.condition_edit = QLineEdit(rule.condition)
        self.condition_edit.setPlaceholderText('optional expression, e.g. time >= "09:00" and time < "17:00"')
        self.condition_edit.setToolTip("Checked when the trigger fires; the macro only runs when it is true. "
                                       "Variables: the saved ones and the built-ins.")
        self.macro_combo = self._macro_combo(macro_names, rule.macro)
        self.queue_check = QCheckBox("Queue it when another macro is running (otherwise it does not run)")
        self.queue_check.setChecked(rule.queue)
        self.enabled_check = QCheckBox("Enabled")
        self.enabled_check.setChecked(rule.enabled)

        form = QFormLayout()
        form.addRow("Name", self.name_edit)
        form.addRow("When", self.kind_combo)
        form.addRow("", self.pages)
        form.addRow("Only if", self.condition_edit)
        form.addRow("Run macro", self.macro_combo)
        form.addRow("", self.queue_check)
        form.addRow("", self.enabled_check)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    @staticmethod
    def _macro_combo(names: list[str], current: str) -> QComboBox:
        combo = QComboBox()
        combo.setEditable(True)
        combo.addItems(names)
        combo.setCurrentText(current)
        return combo

    def _browse(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "File to watch", self.path_edit.text(),
                                              options=QFileDialog.Option.DontConfirmOverwrite)
        if path:
            self.path_edit.setText(path)

    def rule(self) -> TriggerRule:
        kind = self.kind_combo.currentData()
        if kind == "macro_finished":
            params = {"macro": self.after_combo.currentText().strip(), "result": self.result_combo.currentText()}
        elif kind == "clipboard":
            params = {"contains": self.contains_edit.text()} if self.contains_edit.text() else {}
        elif kind == "file":
            params = {"path": self.path_edit.text().strip()}
        else:
            params = {"expression": self.expression_edit.text().strip()}
        return TriggerRule(
            name=self.name_edit.text().strip(), kind=kind, macro=self.macro_combo.currentText().strip(),
            params=params, condition=self.condition_edit.text().strip(), queue=self.queue_check.isChecked(),
            enabled=self.enabled_check.isChecked(), id=self._id,
        )

    def _accept(self) -> None:
        try:
            validate_rule(self.rule())
        except ValueError as exc:
            QMessageBox.warning(self, "Trigger", str(exc))
            return
        self.accept()


class TriggersView(QWidget):
    def __init__(self, service: MacroService, macro_names: Callable[[], list[str]],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self.macro_names = macro_names
        self._rules: list[TriggerRule] = []
        intro = QLabel(
            "Triggers run a macro when something happens: another macro finished, the clipboard "
            "changed, a file changed, or an expression became true. They are on while Auto-run "
            "(toolbar) is on."
        )
        intro.setWordWrap(True)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(len(COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        self.table.itemDoubleClicked.connect(lambda *_: self._edit())
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.add_btn, self.edit_btn = QPushButton("New..."), QPushButton("Edit...")
        self.toggle_btn, self.delete_btn = QPushButton("Disable"), QPushButton("Delete")
        self.add_btn.clicked.connect(self._add)
        self.edit_btn.clicked.connect(self._edit)
        self.toggle_btn.clicked.connect(self._toggle)
        self.delete_btn.clicked.connect(self._delete)
        buttons = QHBoxLayout()
        for b in (self.add_btn, self.edit_btn, self.toggle_btn, self.delete_btn):
            buttons.addWidget(b)
        buttons.addStretch(1)
        self.state_label = QLabel()
        set_tone(self.state_label, "secondary")
        buttons.addWidget(self.state_label)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)
        # the "Last" column changes as triggers fire
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.show_status)
        self.refresh()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        self.refresh()
        self.timer.start()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt API
        self.timer.stop()
        super().hideEvent(event)

    def refresh(self) -> None:
        selected = self._selected()
        self._rules = self.service.db.list_triggers()
        self.table.setRowCount(len(self._rules))
        names = {n.lower() for n in self.macro_names()}
        for row, rule in enumerate(self._rules):
            run = rule.macro + ("" if rule.macro.lower() in names else "  (no such macro)")
            values = ("\u2713" if rule.enabled else "", rule.name, rule.describe(), run,
                      rule.condition, "")
            for col, value in enumerate(values):
                self.table.setItem(row, col, QTableWidgetItem(value))
            if selected is not None and rule.id == selected.id:
                self.table.selectRow(row)
        self.show_status()
        self._update_buttons()

    def show_status(self) -> None:
        status = self.service.triggers.status
        for row, rule in enumerate(self._rules):
            item = self.table.item(row, len(COLUMNS) - 1)
            text = status.get(rule.id, "") if rule.enabled else "disabled"
            if item is not None and item.text() != text:
                item.setText(text)
        self.state_label.setText("" if self.service.autorun_enabled else "Auto-run is off: no trigger fires")

    def _selected(self) -> TriggerRule | None:
        rows = {i.row() for i in self.table.selectedItems()}
        return self._rules[min(rows)] if rows and min(rows) < len(self._rules) else None

    def _update_buttons(self) -> None:
        rule = self._selected()
        for b in (self.edit_btn, self.toggle_btn, self.delete_btn):
            b.setEnabled(rule is not None)
        self.toggle_btn.setText("Enable" if rule is not None and not rule.enabled else "Disable")

    def _add(self) -> None:
        self._open(None)

    def _edit(self) -> None:
        rule = self._selected()
        if rule is not None:
            self._open(rule)

    def _open(self, rule: TriggerRule | None) -> None:
        dialog = TriggerDialog(rule, self.macro_names(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            self.service.save_trigger(dialog.rule())
        except ValueError as exc:  # normally caught by the dialog already
            QMessageBox.warning(self, "Trigger", str(exc))
        self.refresh()

    def _toggle(self) -> None:
        rule = self._selected()
        if rule is None:
            return
        rule.enabled = not rule.enabled
        self.service.save_trigger(rule)
        self.refresh()

    def _delete(self) -> None:
        rule = self._selected()
        if rule is None:
            return
        if QMessageBox.question(self, "Delete trigger", f'Delete the trigger "{rule.name}"?') \
                == QMessageBox.StandardButton.Yes:
            self.service.delete_trigger(rule.id)  # type: ignore[arg-type]
            self.refresh()
