"""Visual macro editor: metadata form plus a drag-and-drop tree of steps."""

from __future__ import annotations

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from keyboardmanager.core import actions as catalog
from keyboardmanager.core import keys
from keyboardmanager.core.models import Action, Macro
from keyboardmanager.core.windowinfo import get_active_window
from keyboardmanager.gui.recording import RecordingSession, RecordOptionsDialog
from keyboardmanager.gui.widgets import ActionDialog, KeyCaptureEdit, ScheduleEdit

ROLE_ACTION = Qt.ItemDataRole.UserRole
ROLE_KIND = Qt.ItemDataRole.UserRole + 1  # "action" | "branch"
ROLE_BRANCH = Qt.ItemDataRole.UserRole + 2  # "then" | "else"


class StepTree(QTreeWidget):
    """Tree of actions. Only containers (repeat, if branches) accept nested drops."""

    reordered = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setIndentation(22)
        self.setAlternatingRowColors(True)

    @staticmethod
    def _accepts_children(item: QTreeWidgetItem | None) -> bool:
        if item is None:
            return True
        if item.data(0, ROLE_KIND) == "branch":
            return True
        action: Action = item.data(0, ROLE_ACTION)
        spec = catalog.spec_for(action.type)
        return spec.container and not spec.has_else

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt API
        target = self.itemAt(event.position().toPoint())
        indicator = self.dropIndicatorPosition()
        position = QAbstractItemView.DropIndicatorPosition
        if target is None or indicator == position.OnViewport:
            parent = None
        elif indicator == position.OnItem:
            parent = target
        else:
            parent = target.parent()
        dragged = self.selectedItems()
        ok = self._accepts_children(parent) and all(i.data(0, ROLE_KIND) == "action" for i in dragged)
        node = parent
        while ok and node is not None:  # never drop an item into its own subtree
            ok = node not in dragged
            node = node.parent()
        if not ok:
            event.ignore()
            return
        super().dropEvent(event)
        self.reordered.emit()


class MacroEditor(QWidget):
    modified = Signal()
    saveRequested = Signal()
    runRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._loading = False
        self.dirty = False
        self._macro_id: int | None = None
        self.actions: list[Action] = []
        self._session: RecordingSession | None = None

        # --- details
        details = QGroupBox("Macro")
        form = QFormLayout(details)
        self.name_edit = QLineEdit()
        self.desc_edit = QPlainTextEdit()
        self.desc_edit.setFixedHeight(48)
        self.desc_edit.setPlaceholderText("Optional description")
        self.hotkey_edit = KeyCaptureEdit("hotkey")
        self.enabled_check = QCheckBox("Enabled (hotkey and schedule active)")
        self.schedule_edit = ScheduleEdit()
        self.schedule_status = QLabel()  # next run, filled in by the main window
        self.schedule_status.setStyleSheet("color: gray;")
        self.enabled_check.setChecked(True)
        self.app_edit = QLineEdit()
        self.app_edit.setPlaceholderText("All applications  (e.g. chrome, code)")
        self.app_edit.setToolTip("Comma separated application names; matched as case-insensitive substrings")
        self.window_edit = QLineEdit()
        self.window_edit.setPlaceholderText("Any window title")
        self.detect_btn = QPushButton("Detect")
        self.detect_btn.setToolTip("Switch to the target application within 3 seconds to fill in the filters")
        self.detect_btn.clicked.connect(self._detect_start)
        scope_row = QHBoxLayout()
        scope_row.addWidget(self.app_edit, 1)
        scope_row.addWidget(self.detect_btn)
        form.addRow("Name", self.name_edit)
        form.addRow("Description", self.desc_edit)
        form.addRow("Hotkey", self.hotkey_edit)
        form.addRow("Application", scope_row)
        form.addRow("Window title", self.window_edit)
        form.addRow("Schedule", self.schedule_edit)
        enabled_row = QHBoxLayout()
        enabled_row.addWidget(self.enabled_check)
        enabled_row.addStretch(1)
        enabled_row.addWidget(self.schedule_status)
        form.addRow("", enabled_row)

        # --- steps
        steps = QGroupBox("Steps")
        steps_layout = QVBoxLayout(steps)
        bar = QHBoxLayout()
        self.add_btn = QToolButton()
        self.add_btn.setText("Add step")
        self.add_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.add_btn.setMenu(self._build_add_menu())
        self.record_btn = QPushButton("Record...")
        self.edit_btn = QPushButton("Edit")
        self.dup_btn = QPushButton("Duplicate")
        self.del_btn = QPushButton("Delete")
        self.up_btn = QPushButton("Up")
        self.down_btn = QPushButton("Down")
        for b in (self.add_btn, self.record_btn, self.edit_btn, self.dup_btn, self.del_btn, self.up_btn, self.down_btn):
            bar.addWidget(b)
        bar.addStretch(1)
        steps_layout.addLayout(bar)
        self.tree = StepTree()
        steps_layout.addWidget(self.tree, 1)
        self.hint = QLabel(
            "Drag steps to reorder or into Repeat / Then / Else. "
            "New steps go after the selection, or inside a selected Repeat / Then / Else."
        )
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: gray;")
        steps_layout.addWidget(self.hint)

        # --- buttons
        footer = QHBoxLayout()
        self.run_btn = QPushButton("Test run (3 s)")
        self.run_btn.setToolTip("Runs the macro as shown after a 3 second countdown, so you can focus the target window")
        self.save_btn = QPushButton("Save")
        self.save_btn.setDefault(True)
        footer.addWidget(self.run_btn)
        footer.addStretch(1)
        footer.addWidget(self.save_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(details)
        layout.addWidget(steps, 1)
        layout.addLayout(footer)

        # --- wiring
        self.record_btn.clicked.connect(self._record)
        self.edit_btn.clicked.connect(self._edit_selected)
        self.dup_btn.clicked.connect(self._duplicate)
        self.del_btn.clicked.connect(self._delete)
        self.up_btn.clicked.connect(lambda: self._move(-1))
        self.down_btn.clicked.connect(lambda: self._move(1))
        self.tree.itemDoubleClicked.connect(lambda *_: self._edit_selected())
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.tree.reordered.connect(self._after_drop)
        self.save_btn.clicked.connect(self.saveRequested)
        self.run_btn.clicked.connect(self.runRequested)
        for signal in (
            self.name_edit.textChanged, self.desc_edit.textChanged, self.hotkey_edit.changed,
            self.app_edit.textChanged, self.window_edit.textChanged, self.enabled_check.toggled,
            self.schedule_edit.changed,
        ):
            signal.connect(self._mark_dirty)
        self._update_buttons()

    # ------------------------------------------------------------------- state
    def _mark_dirty(self, *_args) -> None:
        if not self._loading:
            self.dirty = True
            self.modified.emit()

    def set_enabled_editing(self, enabled: bool) -> None:
        self.setEnabled(enabled)

    def load(self, macro: Macro | None) -> None:
        self._loading = True
        macro = macro or Macro(name="")
        self._macro_id = macro.id
        self.name_edit.setText(macro.name)
        self.desc_edit.setPlainText(macro.description)
        self.hotkey_edit.setText(macro.hotkey)
        self.app_edit.setText(macro.app_filter)
        self.window_edit.setText(macro.window_filter)
        self.enabled_check.setChecked(macro.enabled)
        self.schedule_edit.set_schedule(macro.schedule)
        self.actions = [a.clone() for a in macro.actions]
        self._loading = False
        self.dirty = False
        self.render()

    def build_macro(self) -> Macro:
        """Return the edited macro or raise ``ValueError`` describing the first problem."""
        name = self.name_edit.text().strip()
        if not name:
            raise ValueError("Please give the macro a name.")
        hotkey = ""
        if self.hotkey_edit.text():
            try:
                hotkey = self.hotkey_edit.normalized()
            except ValueError as exc:
                raise ValueError(f"Invalid hotkey: {exc}") from exc
        try:
            schedule = self.schedule_edit.schedule()
        except ValueError as exc:
            raise ValueError(f"Invalid schedule: {exc}") from exc
        problems = catalog.validate_actions(self.actions)
        if problems:
            raise ValueError(problems[0])
        return Macro(
            id=self._macro_id,
            name=name,
            description=self.desc_edit.toPlainText().strip(),
            hotkey=hotkey,
            app_filter=self.app_edit.text().strip(),
            window_filter=self.window_edit.text().strip(),
            enabled=self.enabled_check.isChecked(),
            schedule=schedule,
            actions=[a.clone() for a in self.actions],
        )

    # --------------------------------------------------------------- rendering
    def render(self, select: Action | None = None) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        for action in self.actions:
            self.tree.addTopLevelItem(self._make_item(action))
        self.tree.expandAll()
        self.tree.blockSignals(False)
        if select is not None:
            item = self._find_item(select)
            if item is not None:
                self.tree.setCurrentItem(item)
        self._update_buttons()

    def _make_item(self, action: Action) -> QTreeWidgetItem:
        spec = catalog.spec_for(action.type)
        item = QTreeWidgetItem([catalog.describe(action)])
        item.setData(0, ROLE_ACTION, action)
        item.setData(0, ROLE_KIND, "action")
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled
        if spec.container and not spec.has_else:
            flags |= Qt.ItemFlag.ItemIsDropEnabled
        item.setFlags(flags)
        if spec.category == "Flow":
            font = QFont(item.font(0))
            font.setBold(True)
            item.setFont(0, font)
        if spec.has_else:
            for branch, label, children in (
                ("then", "Then", action.children),
                ("else", "Else", action.else_children),
            ):
                head = QTreeWidgetItem([label])
                head.setData(0, ROLE_KIND, "branch")
                head.setData(0, ROLE_BRANCH, branch)
                head.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDropEnabled)
                font = QFont(head.font(0))
                font.setItalic(True)
                head.setFont(0, font)
                for child in children:
                    head.addChild(self._make_item(child))
                item.addChild(head)
        else:
            for child in action.children:
                item.addChild(self._make_item(child))
        return item

    def _find_item(self, action: Action) -> QTreeWidgetItem | None:
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            if item.data(0, ROLE_ACTION) is action:
                return item
            stack.extend(item.child(i) for i in range(item.childCount()))
        return None

    def _collect(self, parent: QTreeWidgetItem | None) -> list[Action]:
        count = self.tree.topLevelItemCount() if parent is None else parent.childCount()
        out: list[Action] = []
        for i in range(count):
            item = self.tree.topLevelItem(i) if parent is None else parent.child(i)
            action: Action = item.data(0, ROLE_ACTION)
            if catalog.spec_for(action.type).has_else:
                action.children, action.else_children = [], []
                for j in range(item.childCount()):
                    head = item.child(j)
                    target = "children" if head.data(0, ROLE_BRANCH) == "then" else "else_children"
                    setattr(action, target, self._collect(head))
            else:
                action.children = self._collect(item)
            out.append(action)
        return out

    def _after_drop(self) -> None:
        self.actions = self._collect(None)
        self._mark_dirty()
        selected = self._selected_action()
        QTimer.singleShot(0, lambda: self.render(selected))

    # --------------------------------------------------------------- selection
    def _selected_item(self) -> QTreeWidgetItem | None:
        items = self.tree.selectedItems()
        return items[0] if items else None

    def _selected_action(self) -> Action | None:
        item = self._selected_item()
        if item is not None and item.data(0, ROLE_KIND) == "action":
            return item.data(0, ROLE_ACTION)
        return None

    def _list_for(self, parent: QTreeWidgetItem | None) -> list[Action]:
        """The model list that holds the children of ``parent`` (``None`` = top level)."""
        if parent is None:
            return self.actions
        if parent.data(0, ROLE_KIND) == "branch":
            owner: Action = parent.parent().data(0, ROLE_ACTION)
            return owner.children if parent.data(0, ROLE_BRANCH) == "then" else owner.else_children
        return parent.data(0, ROLE_ACTION).children

    def _insertion_point(self) -> tuple[list[Action], int]:
        item = self._selected_item()
        if item is None:
            return self.actions, len(self.actions)
        if item.data(0, ROLE_KIND) == "branch":
            target = self._list_for(item)
            return target, len(target)
        action: Action = item.data(0, ROLE_ACTION)
        spec = catalog.spec_for(action.type)
        if spec.container and not spec.has_else and not action.children:
            return action.children, 0
        siblings = self._list_for(item.parent())
        return siblings, self._index_of(siblings, action) + 1

    @staticmethod
    def _index_of(items: list[Action], action: Action) -> int:
        return next(i for i, a in enumerate(items) if a is action)

    def _update_buttons(self) -> None:
        action = self._selected_action()
        has = action is not None
        for b in (self.edit_btn, self.dup_btn, self.del_btn, self.up_btn, self.down_btn):
            b.setEnabled(has)

    # ----------------------------------------------------------------- actions
    def _build_add_menu(self) -> QMenu:
        menu = QMenu(self)
        for category in catalog.CATEGORIES:
            sub = menu.addMenu(category)
            for spec in catalog.SPECS.values():
                if spec.category == category:
                    sub.addAction(spec.label, lambda t=spec.type: self.add_step(t))
        return menu

    def add_step(self, action_type: str, edit: bool = True) -> Action | None:
        action = catalog.new_action(action_type)
        if edit and (catalog.spec_for(action_type).fields) and ActionDialog(action, self).exec() != ActionDialog.DialogCode.Accepted:
            return None
        target, index = self._insertion_point()
        target.insert(index, action)
        self._mark_dirty()
        self.render(action)
        return action

    def _edit_selected(self) -> None:
        action = self._selected_action()
        if action is None or not catalog.spec_for(action.type).fields:
            return
        if ActionDialog(action, self).exec() == ActionDialog.DialogCode.Accepted:
            self._mark_dirty()
            self.render(action)

    def _duplicate(self) -> None:
        item = self._selected_item()
        action = self._selected_action()
        if item is None or action is None:
            return
        siblings = self._list_for(item.parent())
        copy = action.clone()
        siblings.insert(self._index_of(siblings, action) + 1, copy)
        self._mark_dirty()
        self.render(copy)

    def _delete(self) -> None:
        item = self._selected_item()
        action = self._selected_action()
        if item is None or action is None:
            return
        siblings = self._list_for(item.parent())
        siblings.pop(self._index_of(siblings, action))
        self._mark_dirty()
        self.render()

    def _move(self, delta: int) -> None:
        item = self._selected_item()
        action = self._selected_action()
        if item is None or action is None:
            return
        siblings = self._list_for(item.parent())
        i = self._index_of(siblings, action)
        j = i + delta
        if 0 <= j < len(siblings):
            siblings[i], siblings[j] = siblings[j], siblings[i]
            self._mark_dirty()
            self.render(action)

    # --------------------------------------------------------------- recording
    def _record(self) -> None:
        dialog = RecordOptionsDialog(self)
        if dialog.exec() != RecordOptionsDialog.DialogCode.Accepted:
            return
        self._session = RecordingSession(dialog.options(), self)
        self._session.finished.connect(self._recorded)
        try:
            self._session.start()
        except Exception as exc:  # noqa: BLE001
            self._session = None
            QMessageBox.critical(self, "Recording failed", str(exc))

    def _recorded(self, recorded: list[Action]) -> None:
        self._session = None
        self.window().raise_()
        self.window().activateWindow()
        if not recorded:
            QMessageBox.information(
                self, "Nothing recorded",
                "No input was captured. On macOS, grant Accessibility and Input Monitoring "
                "permission to the terminal / application running Keyboard Manager.",
            )
            return
        target, index = self._insertion_point()
        target[index:index] = recorded
        self._mark_dirty()
        self.render(recorded[-1])

    # ------------------------------------------------------------ scope detect
    def _detect_start(self) -> None:
        self._detect_left = 3
        self.detect_btn.setEnabled(False)
        self._detect_tick()

    def _detect_tick(self) -> None:
        if self._detect_left > 0:
            self.detect_btn.setText(f"Switch window... {self._detect_left}")
            self._detect_left -= 1
            QTimer.singleShot(1000, self._detect_tick)
            return
        window = get_active_window()
        if window.app:
            self.app_edit.setText(window.app)
        self.detect_btn.setText("Detect")
        self.detect_btn.setEnabled(True)
        self.window().raise_()
        self.window().activateWindow()
        if not window.app:
            QMessageBox.information(self, "Detect", "Could not detect the active window on this system.")


def hotkey_conflict(macro: Macro, others: list[Macro], reserved: dict[str, str]) -> str | None:
    """Explain why ``macro.hotkey`` cannot be used, or return ``None`` if it is fine.

    ``reserved`` maps the application's own hotkeys to their purpose (``stop``, ``pause``).
    """
    from keyboardmanager.core.io import scopes_conflict

    if not macro.hotkey:
        return None
    for hotkey, purpose in reserved.items():
        if hotkey and macro.hotkey == keys.normalize_hotkey(hotkey):
            return f"{macro.hotkey} is reserved as the {purpose} hotkey."
    for other in others:
        if other.id != macro.id and other.hotkey == macro.hotkey and scopes_conflict(other, macro):
            return f'{macro.hotkey} is already used by "{other.name}" for the same scope.'
    return None
