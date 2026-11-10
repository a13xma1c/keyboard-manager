"""Profiles tab: manage profiles, switch keymap layers, and the context rules that
switch both automatically with the focused window."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from keyboardmanager.core.context import ContextRule, validate_rule
from keyboardmanager.core.service import MacroService

ALL_LABEL = "All profiles"
RULE_COLUMNS = ("On", "Application", "Window title", "Profile", "Layer")


class ContextRuleDialog(QDialog):
    def __init__(self, rule: ContextRule | None, profiles: list[str], layers: list[str],
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Context rule")
        self.setMinimumWidth(480)
        rule = rule or ContextRule()
        hint = QLabel("While a window matching the filters has focus, the chosen profile and/or "
                      "layer is active; afterwards the previous one comes back.")
        hint.setWordWrap(True)
        self.app_edit = QLineEdit(rule.app)
        self.app_edit.setPlaceholderText("e.g. chrome, code  (comma separated, substring match)")
        self.title_edit = QLineEdit(rule.title)
        self.title_edit.setPlaceholderText("part of the window title")
        self.profile_check = QCheckBox("Switch the profile to")
        self.profile_combo = QComboBox()
        self.profile_combo.addItem(ALL_LABEL, "")
        for name in profiles:
            self.profile_combo.addItem(name, name)
        if rule.profile is not None:
            self.profile_check.setChecked(True)
            self.profile_combo.setCurrentIndex(max(0, self.profile_combo.findData(rule.profile)))
        self.layer_check = QCheckBox("Switch the layer to")
        self.layer_combo = QComboBox()
        self.layer_combo.setEditable(True)
        self.layer_combo.addItems(["", *layers])
        self.layer_combo.lineEdit().setPlaceholderText("Base")
        if rule.layer is not None:
            self.layer_check.setChecked(True)
            self.layer_combo.setCurrentText(rule.layer)
        self.profile_combo.setEnabled(self.profile_check.isChecked())
        self.layer_combo.setEnabled(self.layer_check.isChecked())
        self.profile_check.toggled.connect(self.profile_combo.setEnabled)
        self.layer_check.toggled.connect(self.layer_combo.setEnabled)
        self.enabled_check = QCheckBox("Enabled")
        self.enabled_check.setChecked(rule.enabled)
        form = QFormLayout()
        form.addRow("Application", self.app_edit)
        form.addRow("Window title", self.title_edit)
        form.addRow(self.profile_check, self.profile_combo)
        form.addRow(self.layer_check, self.layer_combo)
        form.addRow("", self.enabled_check)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def rule(self) -> ContextRule:
        return ContextRule(
            app=self.app_edit.text().strip(),
            title=self.title_edit.text().strip(),
            profile=self.profile_combo.currentData() if self.profile_check.isChecked() else None,
            layer=" ".join(self.layer_combo.currentText().split()) if self.layer_check.isChecked() else None,
            enabled=self.enabled_check.isChecked(),
        )

    def _accept(self) -> None:
        try:
            validate_rule(self.rule())
        except ValueError as exc:
            QMessageBox.warning(self, "Context rule", str(exc))
            return
        self.accept()


class ProfilesView(QWidget):
    def __init__(self, service: MacroService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self._rules: list[ContextRule] = []

        # --- profiles
        profiles_box = QGroupBox("Profiles (sets of macros; only the active profile's macros work)")
        self.profile_list = QListWidget()
        self.profile_list.itemSelectionChanged.connect(self._update_buttons)
        self.profile_list.itemDoubleClicked.connect(lambda *_: self._activate())
        self.activate_btn = QPushButton("Activate")
        self.add_profile_btn = QPushButton("New...")
        self.rename_btn = QPushButton("Rename...")
        self.delete_profile_btn = QPushButton("Delete")
        self.activate_btn.clicked.connect(self._activate)
        self.add_profile_btn.clicked.connect(self._add_profile)
        self.rename_btn.clicked.connect(self._rename_profile)
        self.delete_profile_btn.clicked.connect(self._delete_profile)
        profile_buttons = QVBoxLayout()
        for b in (self.activate_btn, self.add_profile_btn, self.rename_btn, self.delete_profile_btn):
            profile_buttons.addWidget(b)
        profile_buttons.addStretch(1)
        profiles_layout = QHBoxLayout(profiles_box)
        profiles_layout.addWidget(self.profile_list, 1)
        profiles_layout.addLayout(profile_buttons)

        # --- layer
        layer_box = QGroupBox("Keymap layer (hotkeys / abbreviations of other layers are off)")
        self.layer_label = QLabel()
        self.layer_combo = QComboBox()
        self.layer_combo.setEditable(True)
        self.layer_combo.lineEdit().setPlaceholderText("Base")
        self.switch_layer_btn = QPushButton("Switch")
        self.switch_layer_btn.clicked.connect(
            lambda: self.service.set_layer(self.layer_combo.currentText()))
        self.base_btn = QPushButton("Back to base")
        self.base_btn.clicked.connect(lambda: self.service.set_layer(""))
        layer_layout = QHBoxLayout(layer_box)
        layer_layout.addWidget(self.layer_label)
        layer_layout.addStretch(1)
        layer_layout.addWidget(self.layer_combo, 1)
        layer_layout.addWidget(self.switch_layer_btn)
        layer_layout.addWidget(self.base_btn)

        # --- context rules
        rules_box = QGroupBox("Context rules (context-aware keymaps)")
        self.watch_check = QCheckBox("Switch the profile / layer automatically with the focused window")
        self.watch_check.setChecked(service.contexts_enabled)
        self.watch_check.toggled.connect(self._toggle_watching)
        self.rules_table = QTableWidget(0, len(RULE_COLUMNS))
        self.rules_table.setHorizontalHeaderLabels(RULE_COLUMNS)
        self.rules_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.rules_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.rules_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.rules_table.verticalHeader().setVisible(False)
        header = self.rules_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.rules_table.itemDoubleClicked.connect(lambda *_: self._edit_rule())
        self.rules_table.itemSelectionChanged.connect(self._update_buttons)
        self.add_rule_btn = QPushButton("New...")
        self.edit_rule_btn = QPushButton("Edit...")
        self.up_rule_btn = QPushButton("Up")
        self.up_rule_btn.setToolTip("Rules are checked top to bottom; the first match wins")
        self.down_rule_btn = QPushButton("Down")
        self.delete_rule_btn = QPushButton("Delete")
        self.add_rule_btn.clicked.connect(self._add_rule)
        self.edit_rule_btn.clicked.connect(self._edit_rule)
        self.up_rule_btn.clicked.connect(lambda: self._move_rule(-1))
        self.down_rule_btn.clicked.connect(lambda: self._move_rule(1))
        self.delete_rule_btn.clicked.connect(self._delete_rule)
        rule_buttons = QVBoxLayout()
        for b in (self.add_rule_btn, self.edit_rule_btn, self.up_rule_btn, self.down_rule_btn,
                  self.delete_rule_btn):
            rule_buttons.addWidget(b)
        rule_buttons.addStretch(1)
        rules_layout = QVBoxLayout(rules_box)
        rules_layout.addWidget(self.watch_check)
        rules_row = QHBoxLayout()
        rules_row.addWidget(self.rules_table, 1)
        rules_row.addLayout(rule_buttons)
        rules_layout.addLayout(rules_row)

        layout = QVBoxLayout(self)
        layout.addWidget(profiles_box, 1)
        layout.addWidget(layer_box)
        layout.addWidget(rules_box, 1)
        self.refresh()

    # ------------------------------------------------------------------ state
    def refresh(self) -> None:
        service = self.service
        active = service.active_profile
        self.profile_list.blockSignals(True)
        self.profile_list.clear()
        for name in ["", *service.all_profiles()]:
            label = name or ALL_LABEL
            if name.lower() == active.lower():
                label += "   \u25c0 active"
            self.profile_list.addItem(label)
            if name.lower() == active.lower():
                self.profile_list.setCurrentRow(self.profile_list.count() - 1)
        self.profile_list.blockSignals(False)
        layers = service.all_layers()
        self.layer_label.setText(f'Active: {service.active_layer or "base"}')
        current = self.layer_combo.currentText()
        self.layer_combo.blockSignals(True)
        self.layer_combo.clear()
        self.layer_combo.addItems(["", *layers])
        self.layer_combo.setCurrentText(current)
        self.layer_combo.blockSignals(False)
        self._rules = service.context_rules
        self.rules_table.setRowCount(len(self._rules))
        for row, rule in enumerate(self._rules):
            values = ("\u2713" if rule.enabled else "", rule.app, rule.title,
                      "" if rule.profile is None else (rule.profile or ALL_LABEL),
                      "" if rule.layer is None else (rule.layer or "base"))
            for col, value in enumerate(values):
                self.rules_table.setItem(row, col, QTableWidgetItem(value))
        self.watch_check.blockSignals(True)
        self.watch_check.setChecked(service.contexts_enabled)
        self.watch_check.blockSignals(False)
        self._update_buttons()

    def _update_buttons(self) -> None:
        name = self._selected_profile()
        self.activate_btn.setEnabled(name is not None)
        self.rename_btn.setEnabled(bool(name))
        self.delete_profile_btn.setEnabled(bool(name))
        row = self._selected_rule_row()
        self.edit_rule_btn.setEnabled(row is not None)
        self.delete_rule_btn.setEnabled(row is not None)
        self.up_rule_btn.setEnabled(row is not None and row > 0)
        self.down_rule_btn.setEnabled(row is not None and row < len(self._rules) - 1)

    # ---------------------------------------------------------------- profiles
    def _selected_profile(self) -> str | None:
        """The selected profile name; "" = All profiles, None = nothing selected."""
        row = self.profile_list.currentRow()
        if row < 0 or not self.profile_list.item(row).isSelected():
            return None
        return ["", *self.service.all_profiles()][row]

    def _activate(self) -> None:
        name = self._selected_profile()
        if name is not None:
            self.service.set_active_profile(name)
            self.refresh()

    def _add_profile(self) -> None:
        name, ok = QInputDialog.getText(self, "New profile", "Name of the profile:")
        if not ok or not name.strip():
            return
        try:
            self.service.profiles.add(name)
        except ValueError as exc:
            QMessageBox.warning(self, "New profile", str(exc))
            return
        self.refresh()

    def _rename_profile(self) -> None:
        old = self._selected_profile()
        if not old:
            return
        new, ok = QInputDialog.getText(self, "Rename profile", f'New name of "{old}":', text=old)
        if not ok or not new.strip() or new == old:
            return
        try:
            self.service.rename_profile(old, new)
        except ValueError as exc:
            QMessageBox.warning(self, "Rename profile", str(exc))
            return
        self.refresh()

    def _delete_profile(self) -> None:
        name = self._selected_profile()
        if not name:
            return
        answer = QMessageBox.question(
            self, "Delete profile",
            f'Delete the profile "{name}"? Macros stay; they are just no longer restricted to it.')
        if answer == QMessageBox.StandardButton.Yes:
            self.service.delete_profile(name)
            self.refresh()

    # ------------------------------------------------------------------- rules
    def _selected_rule_row(self) -> int | None:
        rows = {i.row() for i in self.rules_table.selectedItems()}
        return min(rows) if rows else None

    def _toggle_watching(self, on: bool) -> None:
        self.service.set_contexts_enabled(on)

    def _save_rules(self, rules: list[ContextRule]) -> bool:
        try:
            self.service.set_context_rules(rules)
        except ValueError as exc:
            QMessageBox.warning(self, "Context rules", str(exc))
            return False
        self.refresh()
        return True

    def _open_rule(self, rule: ContextRule | None, row: int | None) -> None:
        dialog = ContextRuleDialog(rule, self.service.all_profiles(), self.service.all_layers(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        rules = list(self._rules)
        if row is None:
            rules.append(dialog.rule())
        else:
            rules[row] = dialog.rule()
        self._save_rules(rules)

    def _add_rule(self) -> None:
        self._open_rule(None, None)

    def _edit_rule(self) -> None:
        row = self._selected_rule_row()
        if row is not None:
            self._open_rule(self._rules[row], row)

    def _move_rule(self, delta: int) -> None:
        row = self._selected_rule_row()
        if row is None or not 0 <= row + delta < len(self._rules):
            return
        rules = list(self._rules)
        rules[row], rules[row + delta] = rules[row + delta], rules[row]
        if self._save_rules(rules):
            self.rules_table.selectRow(row + delta)

    def _delete_rule(self) -> None:
        row = self._selected_rule_row()
        if row is not None:
            self._save_rules([r for i, r in enumerate(self._rules) if i != row])
