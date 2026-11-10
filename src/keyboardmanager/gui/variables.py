"""Variables tab: the saved variables (kept across runs, shared by all macros)."""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QLineEdit,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from keyboardmanager.core.script import COMMAND_HELP, FUNCTION_HELP, to_text
from keyboardmanager.core.variables import BUILTINS, SavedVariables, valid_name
from keyboardmanager.gui.theme import set_tone


class VariablesView(QWidget):
    def __init__(self, store: SavedVariables, parent: QWidget | None = None,
                 evaluate: Callable[[str], Any] | None = None) -> None:
        """``evaluate`` (optional) evaluates an expression for the expression tester."""
        super().__init__(parent)
        self.store = store
        self._evaluate = evaluate
        intro = QLabel(
            "Saved variables keep their value between runs and are shared by all macros "
            '(Set variable with Keep = "saved"). Use them in texts as {name}.'
        )
        intro.setWordWrap(True)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(("Name", "Value"))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.itemDoubleClicked.connect(lambda *_: self._edit())
        self.add_btn, self.edit_btn, self.delete_btn = QPushButton("Add..."), QPushButton("Edit..."), QPushButton("Delete")
        self.add_btn.clicked.connect(self._add)
        self.edit_btn.clicked.connect(self._edit)
        self.delete_btn.clicked.connect(self._delete)
        buttons = QHBoxLayout()
        for b in (self.add_btn, self.edit_btn, self.delete_btn):
            buttons.addWidget(b)
        buttons.addStretch(1)
        builtins = QLabel(
            "Always available: " + ", ".join(f"<b>{{{name}}}</b> {text}" for name, text in BUILTINS.items())
            + ". Dates take a format, e.g. {date:%d.%m.%Y}; {{ and }} type a literal brace."
        )
        builtins.setWordWrap(True)
        set_tone(builtins, "secondary")
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)
        layout.addWidget(builtins)
        layout.addWidget(self._expressions_box())
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.refresh()

    def _expressions_box(self) -> QGroupBox:
        box = QGroupBox("Expressions and scripts")
        box_layout = QVBoxLayout(box)
        reference = QLabel(
            "Expressions use Python syntax: <code>len(clipboard) &gt; 10 and \"Mail\" in app</code>, "
            "<code>round(price * 1.2, 2)</code>, <code>\"yes\" if n &gt; 3 else \"no\"</code>. "
            "Use them in If / While (condition kind \"expression\"), in Set variable (mode "
            "\"expression\") and inside texts as <code>{= expression}</code>. "
            f"<br><b>Functions:</b> {FUNCTION_HELP}."
            "<br><b>Run script</b> steps add assignments, if / elif / else, while, for, break and "
            f"these commands: {COMMAND_HELP}."
        )
        reference.setWordWrap(True)
        box_layout.addWidget(reference)
        if self._evaluate is not None:
            row = QHBoxLayout()
            self.expression_edit = QLineEdit()
            self.expression_edit.setPlaceholderText("Try an expression, e.g. upper(app) or time >= \"09:00\"")
            self.expression_edit.returnPressed.connect(self.evaluate_expression)
            evaluate_btn = QPushButton("Evaluate")
            evaluate_btn.clicked.connect(self.evaluate_expression)
            row.addWidget(self.expression_edit, 1)
            row.addWidget(evaluate_btn)
            box_layout.addLayout(row)
            self.result_label = QLabel()
            self.result_label.setWordWrap(True)
            self.result_label.setTextInteractionFlags(self.result_label.textInteractionFlags()
                                                      | Qt.TextInteractionFlag.TextSelectableByMouse)
            box_layout.addWidget(self.result_label)
        return box

    def evaluate_expression(self) -> None:
        expression = self.expression_edit.text().strip()
        if not expression:
            self.result_label.setText("")
            return
        try:
            value = self._evaluate(expression)  # type: ignore[misc]
        except KeyError as exc:
            self.result_label.setText(f"\u26a0 Unknown variable {exc}")
        except Exception as exc:  # noqa: BLE001 - show any problem to the user
            self.result_label.setText(f"\u26a0 {exc}")
        else:
            self.result_label.setText(f"= {to_text(value)}")

    def refresh(self) -> None:
        data = self.store.all()
        self.table.setRowCount(len(data))
        for row, (name, value) in enumerate(sorted(data.items())):
            self.table.setItem(row, 0, QTableWidgetItem(name))
            self.table.setItem(row, 1, QTableWidgetItem(value))
        self._update_buttons()

    def _update_buttons(self) -> None:
        selected = bool(self.table.selectedItems())
        self.edit_btn.setEnabled(selected)
        self.delete_btn.setEnabled(selected)

    def _selected_name(self) -> str | None:
        rows = {i.row() for i in self.table.selectedItems()}
        return self.table.item(min(rows), 0).text() if rows else None

    def _add(self) -> None:
        name, ok = QInputDialog.getText(self, "Add variable", "Name (letters, digits and _):")
        if not ok:
            return
        name = name.strip()
        if not valid_name(name):
            QMessageBox.warning(self, "Add variable", f'"{name}" is not a valid variable name.')
            return
        value, ok = QInputDialog.getText(self, "Add variable", f"Value of {name}:")
        if ok:
            self.store.set(name, value)
            self.refresh()

    def _edit(self) -> None:
        name = self._selected_name()
        if name is None:
            return
        value, ok = QInputDialog.getText(self, "Edit variable", f"Value of {name}:", text=self.store.get(name) or "")
        if ok:
            self.store.set(name, value)
            self.refresh()

    def _delete(self) -> None:
        names = sorted({self.table.item(i.row(), 0).text() for i in self.table.selectedItems()})
        if not names:
            return
        answer = QMessageBox.question(self, "Delete variables", f"Delete {', '.join(names)}?")
        if answer == QMessageBox.StandardButton.Yes:
            for name in names:
                self.store.delete(name)
            self.refresh()
