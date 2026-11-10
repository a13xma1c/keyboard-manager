"""Visual macro editor: metadata form plus a drag-and-drop tree of steps."""

from __future__ import annotations

from typing import Callable

import json

from PySide6.QtCore import QMargins, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QAction, QColor, QFont, QKeySequence, QUndoCommand, QUndoStack
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QSpinBox,
    QFormLayout,
    QGridLayout,
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
from keyboardmanager.core.events import EVENTS, validate_event
from keyboardmanager.core.conflicts import abbreviation_conflict, hotkey_conflict  # noqa: F401 (used by the main window)
from keyboardmanager.core.expander import normalize_abbreviation
from keyboardmanager.core.limits import Limits
from keyboardmanager.core.organize import MAX_SPEED, MIN_SPEED, normalize_folder, normalize_tags
from keyboardmanager.core.profiles import normalize_profiles
from keyboardmanager.core.models import Action, Macro
from keyboardmanager.gui.runview import BREAKPOINT_MARK
from keyboardmanager.core.windowinfo import get_active_window
from keyboardmanager.gui.recording import RecordingSession, RecordOptionsDialog, RecordReviewDialog
from keyboardmanager.gui.widgets import ActionDialog, KeyCaptureEdit, ScheduleEdit
from keyboardmanager.gui.theme import set_tone

ROLE_ACTION = Qt.ItemDataRole.UserRole
ROLE_KIND = Qt.ItemDataRole.UserRole + 1  # "action" | "branch"
ROLE_BRANCH = Qt.ItemDataRole.UserRole + 2  # "then" | "else"
CLIPBOARD_FORMAT = "keyboardmanager-steps"  # steps copied to the clipboard, as JSON


class StepTree(QTreeWidget):
    """Tree of actions. Only containers (repeat, if branches) accept nested drops."""

    reordered = Signal()
    about_to_drop = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
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
        self.about_to_drop.emit()  # lets the editor remember the state for undo
        super().dropEvent(event)
        self.reordered.emit()


class _StepsChange(QUndoCommand):
    """One undoable change of the steps: the state before and after it."""

    def __init__(self, editor: "MacroEditor", before: dict, after: dict, text: str) -> None:
        super().__init__(text)
        self._editor, self._before, self._after = editor, before, after
        self._done = True  # pushed after the change was already made

    def redo(self) -> None:
        if self._done:
            self._done = False
            return
        self._editor._restore(self._after)

    def undo(self) -> None:
        self._editor._restore(self._before)


class _FittingScrollArea(QScrollArea):
    """Asks for the full height of its content (it only scrolls when there is less)."""

    def sizeHint(self):  # noqa: N802 - Qt API
        import shiboken6

        if not shiboken6.isValid(self):  # asked while being destroyed
            return QSize()
        content = self.widget()
        if content is None:
            return super().sizeHint()
        hint = content.sizeHint()
        bar = self.verticalScrollBar().sizeHint().width()
        return hint.grownBy(QMargins(0, 0, bar, 0))


class MacroEditor(QWidget):
    modified = Signal()
    saveRequested = Signal()
    runRequested = Signal()
    debugRequested = Signal()
    detachRequested = Signal()  # "Edit locally" on a read-only team macro

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._loading = False
        self.dirty = False
        self._macro_id: int | None = None
        self._uid, self._source = "", ""
        self.actions: list[Action] = []
        self._session: RecordingSession | None = None
        self._breakpoints: list[Action] = []  # steps (by identity) the debugger stops before
        self.macro_names: Callable[[], list[str]] = list  # for "Run another macro"; set by the main window

        # --- details
        details = QGroupBox("Macro")
        form = QFormLayout(details)
        self.name_edit = QLineEdit()
        self.desc_edit = QPlainTextEdit()
        self.desc_edit.setFixedHeight(48)
        self.desc_edit.setPlaceholderText("Optional description")
        self.hotkey_edit = KeyCaptureEdit("hotkey")
        self.abbreviation_edit = QLineEdit()
        self.abbreviation_edit.setPlaceholderText("e.g. ;sig  (typing it runs the macro, replacing what was typed)")
        self.abbreviation_edit.setToolTip(
            "Text expansion: when you type this anywhere, it is deleted and the macro runs "
            "(usually a Type text step). Start it with a rarely typed character such as ; or /."
        )
        self.enabled_check = QCheckBox("Enabled")
        self.enabled_check.setToolTip("Its hotkey, abbreviation, schedule and event are active")
        self.schedule_edit = ScheduleEdit()
        self.event_combo = QComboBox()
        for value, label in EVENTS.items():
            self.event_combo.addItem(label, value)
        self.folder_combo = QComboBox()
        self.folder_combo.setEditable(True)
        self.folder_combo.setToolTip("The folder in the macro list; type a new one (a/b for nested folders)")
        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("e.g. email, work  (comma separated)")
        self.favorite_check = QCheckBox("\u2605 Favorite")
        self.favorite_check.setToolTip("Favorites are listed first and can be run from the tray menu")
        self.speed_spin = QDoubleSpinBox()
        self.speed_spin.setRange(MIN_SPEED, MAX_SPEED)
        self.speed_spin.setSingleStep(0.25)
        self.speed_spin.setDecimals(2)
        self.speed_spin.setSuffix("x")
        self.speed_spin.setToolTip("How fast this macro's delays and typing play: 2x takes half the time. "
                                   "Multiplied by the speed in the toolbar.")
        self.event_combo.setToolTip(
            "Run the macro when something happens. The focus events use the Application /"
            " Window title filters above to know which application to watch."
        )
        self.schedule_status = QLabel()  # next run, filled in by the main window
        set_tone(self.schedule_status, "secondary")
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
        form.addRow("Abbreviation", self.abbreviation_edit)
        form.addRow("Application", scope_row)
        form.addRow("Window title", self.window_edit)
        form.addRow("Schedule", self.schedule_edit)
        form.addRow("Run on event", self.event_combo)
        organize = QHBoxLayout()
        organize.addWidget(self.folder_combo, 1)
        organize.addWidget(QLabel("Tags"))
        organize.addWidget(self.tags_edit, 1)
        organize.addWidget(QLabel("Speed"))
        organize.addWidget(self.speed_spin)
        form.addRow("Folder", organize)
        self.profiles_edit = QLineEdit()
        self.profiles_edit.setPlaceholderText("All profiles  (comma separated to restrict, e.g. Work, Gaming)")
        self.profiles_edit.setToolTip("The macro is only active while one of these profiles is active "
                                      "(toolbar); empty = in every profile")
        self.layer_combo = QComboBox()
        self.layer_combo.setEditable(True)
        self.layer_combo.setToolTip("Its hotkey and abbreviation only work while this keymap layer is "
                                    "active (Switch layer step); empty = the base layer, always active")
        self.layer_combo.lineEdit().setPlaceholderText("Base")
        keymap_row = QHBoxLayout()
        keymap_row.addWidget(self.profiles_edit, 2)
        keymap_row.addWidget(QLabel("Layer"))
        keymap_row.addWidget(self.layer_combo, 1)
        form.addRow("Profiles", keymap_row)
        self.limit_spins: dict[str, QSpinBox] = {}
        limits_row = QGridLayout()  # two pairs per line, so the form stays narrow
        limits_row.setContentsMargins(0, 0, 0, 0)
        for index, (name, label, suffix, maximum, tip) in enumerate((
            ("max_seconds", "Time", " s", 86_400, "Stop a run after this much running time (paused time excluded)"),
            ("max_steps", "Steps", "", 1_000_000, "Stop a run after this many steps"),
            ("cooldown_s", "Cooldown", " s", 86_400,
             "Hotkeys, abbreviations, schedules and events cannot start it again sooner"),
            ("max_runs_per_day", "Per day", "", 100_000,
             "Hotkeys, abbreviations, schedules and events start it at most this often per day"),
        )):
            spin = QSpinBox()
            spin.setRange(0, maximum)
            spin.setSuffix(suffix)
            spin.setSpecialValueText("none")
            spin.setToolTip(tip + ". Running it by hand is never limited.")
            limits_row.addWidget(QLabel(label), index // 2, (index % 2) * 2)
            limits_row.addWidget(spin, index // 2, (index % 2) * 2 + 1)
            self.limit_spins[name] = spin
        self.busy_combo = QComboBox()
        self.busy_combo.addItem("skip the run", "skip")
        self.busy_combo.addItem("queue it", "queue")
        self.busy_combo.setToolTip("When a hotkey, schedule, event or trigger starts this macro while another "
                                   "macro is running: skip this run, or queue it to run afterwards")
        limits_row.setColumnStretch(4, 1)
        form.addRow("Limits", limits_row)
        enabled_row = QHBoxLayout()
        enabled_row.addWidget(self.enabled_check)
        enabled_row.addWidget(self.favorite_check)
        enabled_row.addStretch(1)
        enabled_row.addWidget(QLabel("When busy"))
        enabled_row.addWidget(self.busy_combo)
        form.addRow("", enabled_row)
        form.addRow("", self.schedule_status)

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
        self.breakpoint_btn = QPushButton("Breakpoint")
        self.breakpoint_btn.setToolTip("Debugging stops before the selected steps (F9)")
        # undo / redo of step changes (text fields keep their own Ctrl+Z)
        self.undo_stack = QUndoStack(self)
        self.undo_action = self.undo_stack.createUndoAction(self, "Undo")
        self.redo_action = self.undo_stack.createRedoAction(self, "Redo")
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        # fixed labels (the full "Undo Delete step" text is the tooltip), so the bar
        # does not change width after every edit
        self.undo_btn, self.redo_btn = QToolButton(), QToolButton()
        self.undo_btn.setText("Undo")
        self.redo_btn.setText("Redo")
        self.undo_btn.clicked.connect(self.undo_stack.undo)
        self.redo_btn.clicked.connect(self.undo_stack.redo)
        self.undo_stack.canUndoChanged.connect(self.undo_btn.setEnabled)
        self.undo_stack.canRedoChanged.connect(self.redo_btn.setEnabled)
        self.undo_stack.undoTextChanged.connect(self._undo_tooltip)
        self.undo_stack.redoTextChanged.connect(self._redo_tooltip)
        self.undo_btn.setEnabled(False)
        self.redo_btn.setEnabled(False)
        for b in (self.add_btn, self.record_btn, self.edit_btn, self.dup_btn, self.del_btn, self.up_btn,
                  self.down_btn, self.breakpoint_btn, self.undo_btn, self.redo_btn):
            bar.addWidget(b)
        bar.addStretch(1)
        steps_layout.addLayout(bar)
        self.tree = StepTree()
        steps_layout.addWidget(self.tree, 1)
        self.hint = QLabel(
            "Drag steps to reorder or into Repeat / Then / Else; Ctrl/Shift+click selects several. "
            "New steps go after the selection, or inside a selected Repeat / Then / Else. "
            "Copy, cut, paste and Delete work on the selected steps."
        )
        self.hint.setWordWrap(True)
        set_tone(self.hint, "secondary")
        steps_layout.addWidget(self.hint)

        # --- buttons
        footer = QHBoxLayout()
        self.run_btn = QPushButton("Test run (3 s)")
        self.run_btn.setToolTip("Runs the macro as shown after a 3 second countdown, so you can focus the target window")
        self.debug_btn = QToolButton()
        self.debug_btn.setText("Debug")
        self.debug_btn.setToolTip("Run the macro as shown step by step, with breakpoints, in the Run tab")
        self.debug_btn.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        debug_menu = QMenu(self.debug_btn)
        self.stop_at_start_action = debug_menu.addAction("Stop at the first step")
        self.stop_at_start_action.setCheckable(True)
        self.stop_at_start_action.setChecked(True)
        self.simulate_action = debug_menu.addAction("Simulate input (nothing is typed or clicked)")
        self.simulate_action.setCheckable(True)
        self.simulate_action.setChecked(True)
        self.simulate_action.setToolTip("Otherwise the debug run sends real input after a 3 second countdown")
        debug_menu.addSeparator()
        debug_menu.addAction("Clear all breakpoints", self.clear_breakpoints)
        self.debug_btn.setMenu(debug_menu)
        self.save_btn = QPushButton("Save")
        self.save_btn.setDefault(True)
        self.issues_btn = QPushButton()  # "2 problems" - opens the Check tab
        self.issues_btn.setFlat(True)
        self.issues_btn.setVisible(False)
        footer.addWidget(self.run_btn)
        footer.addWidget(self.debug_btn)
        footer.addStretch(1)
        footer.addWidget(self.issues_btn)
        footer.addWidget(self.save_btn)

        # read-only team macros: a banner instead of editing
        self.team_banner = QWidget()
        self.team_banner.setObjectName("teamBanner")  # styled by the theme
        self.team_banner.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        banner_layout = QHBoxLayout(self.team_banner)
        self.team_label = QLabel()
        self.team_label.setWordWrap(True)
        self.detach_btn = QPushButton("Edit locally")
        self.detach_btn.setToolTip("Make it your own macro (it stops following the team library; "
                                   "you can publish your version back)")
        self.detach_btn.clicked.connect(self.detachRequested)
        banner_layout.addWidget(self.team_label, 1)
        banner_layout.addWidget(self.detach_btn)
        self.team_banner.setVisible(False)
        self._details_box, self._steps_box = details, steps
        self.read_only = False

        # the details scroll when the window is small, so the steps always get room
        self.details_scroll = _FittingScrollArea()
        self.details_scroll.setWidget(details)
        self.details_scroll.setWidgetResizable(True)
        self.details_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.details_scroll.setMinimumHeight(160)
        self.details_scroll.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

        # details above, steps below; drag the gap between them to give either more room
        steps.setMinimumHeight(240)
        self.body_splitter = QSplitter(Qt.Orientation.Vertical)
        self.body_splitter.setChildrenCollapsible(False)
        self.body_splitter.addWidget(self.details_scroll)
        self.body_splitter.addWidget(steps)
        self.body_splitter.setStretchFactor(0, 0)
        self.body_splitter.setStretchFactor(1, 1)
        self.body_splitter.setHandleWidth(8)

        layout = QVBoxLayout(self)
        layout.addWidget(self.team_banner)
        layout.addWidget(self.body_splitter, 1)
        layout.addLayout(footer)

        # --- wiring
        self.record_btn.clicked.connect(self._record)
        self.edit_btn.clicked.connect(self._edit_selected)
        self.dup_btn.clicked.connect(self._duplicate)
        self.del_btn.clicked.connect(self._delete)
        self.up_btn.clicked.connect(lambda: self._move(-1))
        self.down_btn.clicked.connect(lambda: self._move(1))
        self.breakpoint_btn.clicked.connect(self.toggle_breakpoint)
        self.tree.itemDoubleClicked.connect(lambda *_: self._edit_selected())
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        self.tree.reordered.connect(self._after_drop)
        self.tree.about_to_drop.connect(self._before_drop)
        self._drop_state: dict | None = None
        for shortcut, handler in (
            (QKeySequence.StandardKey.Copy, self.copy_steps),
            (QKeySequence.StandardKey.Cut, self.cut_steps),
            (QKeySequence.StandardKey.Paste, self.paste_steps),
            (QKeySequence.StandardKey.Delete, self._delete),
            (QKeySequence(Qt.Key.Key_F9), self.toggle_breakpoint),
        ):  # only while the step list has the focus, so text fields keep theirs
            action = QAction(self.tree)
            action.setShortcut(shortcut)
            action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
            action.triggered.connect(handler)
            self.tree.addAction(action)
        for action in (self.undo_action, self.redo_action):
            action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            self.addAction(action)
        self.save_btn.clicked.connect(self.saveRequested)
        self.run_btn.clicked.connect(self.runRequested)
        self.debug_btn.clicked.connect(self.debugRequested)
        for signal in (
            self.name_edit.textChanged, self.desc_edit.textChanged, self.hotkey_edit.changed,
            self.abbreviation_edit.textChanged,
            self.app_edit.textChanged, self.window_edit.textChanged, self.enabled_check.toggled,
            self.schedule_edit.changed, self.event_combo.currentIndexChanged,
            self.folder_combo.currentTextChanged, self.tags_edit.textChanged,
            self.favorite_check.toggled, self.speed_spin.valueChanged, self.busy_combo.currentIndexChanged,
            self.profiles_edit.textChanged, self.layer_combo.currentTextChanged,
            *(spin.valueChanged for spin in self.limit_spins.values()),
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

    def load(self, macro: Macro | None, keep_history: bool = False) -> None:
        """Show ``macro``. ``keep_history`` (after saving) keeps the undo history."""
        self._loading = True
        macro = macro or Macro(name="")
        self._macro_id = macro.id
        self._uid, self._source = macro.uid, macro.source  # kept through editing
        self.name_edit.setText(macro.name)
        self.desc_edit.setPlainText(macro.description)
        self.hotkey_edit.setText(macro.hotkey)
        self.abbreviation_edit.setText(macro.abbreviation)
        self.app_edit.setText(macro.app_filter)
        self.window_edit.setText(macro.window_filter)
        self.enabled_check.setChecked(macro.enabled)
        self.schedule_edit.set_schedule(macro.schedule)
        self.event_combo.setCurrentIndex(max(0, self.event_combo.findData(macro.event)))
        self.folder_combo.setCurrentText(macro.folder)
        self.tags_edit.setText(", ".join(macro.tags))
        self.favorite_check.setChecked(macro.favorite)
        self.speed_spin.setValue(macro.speed)
        for name, spin in self.limit_spins.items():
            spin.setValue(getattr(macro.limits, name))
        self.busy_combo.setCurrentIndex(max(0, self.busy_combo.findData(macro.when_busy)))
        self.profiles_edit.setText(", ".join(macro.profiles))
        self.layer_combo.setCurrentText(macro.layer)
        kept = self._breakpoint_list_paths() if keep_history else []
        self.actions = [a.clone() for a in macro.actions]
        self._breakpoints = [a for a in (self._at(p) for p in kept) if a is not None]
        self._loading = False
        self.dirty = False
        if not keep_history:
            self.undo_stack.clear()
        self.undo_stack.setClean()
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
            abbreviation = normalize_abbreviation(self.abbreviation_edit.text())
        except ValueError as exc:
            raise ValueError(f"Invalid abbreviation: {exc}") from exc
        try:
            schedule = self.schedule_edit.schedule()
        except ValueError as exc:
            raise ValueError(f"Invalid schedule: {exc}") from exc
        try:
            folder = normalize_folder(self.folder_combo.currentText())
            tags = normalize_tags(self.tags_edit.text())
        except ValueError as exc:
            raise ValueError(f"Invalid folder or tags: {exc}") from exc
        try:
            profiles = normalize_profiles(self.profiles_edit.text())
        except ValueError as exc:
            raise ValueError(f"Invalid profiles: {exc}") from exc
        problems = catalog.validate_actions(self.actions)
        if problems:
            raise ValueError(problems[0])
        macro = Macro(
            id=self._macro_id,
            name=name,
            description=self.desc_edit.toPlainText().strip(),
            hotkey=hotkey,
            app_filter=self.app_edit.text().strip(),
            window_filter=self.window_edit.text().strip(),
            enabled=self.enabled_check.isChecked(),
            schedule=schedule,
            event=self.event_combo.currentData(),
            abbreviation=abbreviation,
            folder=folder,
            tags=tags,
            favorite=self.favorite_check.isChecked(),
            speed=round(self.speed_spin.value(), 2),
            limits=Limits(**{name: spin.value() for name, spin in self.limit_spins.items()}),
            when_busy=self.busy_combo.currentData(),
            profiles=profiles,
            layer=" ".join(self.layer_combo.currentText().split()),
            uid=self._uid,
            source=self._source,
            actions=[a.clone() for a in self.actions],
        )
        try:
            validate_event(macro.event, macro)
        except ValueError as exc:
            raise ValueError(f"Invalid event: {exc}") from exc
        return macro

    def set_read_only(self, reason: str) -> None:
        """Show the macro without letting it be changed ("" = editable again)."""
        self.read_only = bool(reason)
        self.team_banner.setVisible(self.read_only)
        self.team_label.setText(reason)
        for widget in (self._details_box, self._steps_box, self.save_btn):
            widget.setEnabled(not self.read_only)

    def set_folders(self, folders: list[str]) -> None:
        """The folders offered in the Folder field (the text itself is kept)."""
        text = self.folder_combo.currentText()
        self.folder_combo.blockSignals(True)
        self.folder_combo.clear()
        self.folder_combo.addItems(["", *folders])
        self.folder_combo.setCurrentText(text)
        self.folder_combo.blockSignals(False)

    def set_layers(self, layers: list[str]) -> None:
        """The layers offered in the Layer field (the text itself is kept)."""
        text = self.layer_combo.currentText()
        self.layer_combo.blockSignals(True)
        self.layer_combo.clear()
        self.layer_combo.addItems(["", *layers])
        self.layer_combo.setCurrentText(text)
        self.layer_combo.blockSignals(False)

    def show_issues(self, issues: list) -> None:
        """The pre-flight result for this macro, next to the Save button."""
        errors = sum(1 for i in issues if i.severity == "error")
        warnings = sum(1 for i in issues if i.severity == "warning")
        parts = [f"{errors} problem{'s' if errors != 1 else ''}"] if errors else []
        if warnings:
            parts.append(f"{warnings} warning{'s' if warnings != 1 else ''}")
        self.issues_btn.setVisible(bool(parts))
        self.issues_btn.setText("\u26a0 " + ", ".join(parts) + " - Check" if parts else "")
        set_tone(self.issues_btn, "danger" if errors else "warning")
        self.issues_btn.setToolTip("\n".join(i.message for i in issues if i.severity != "info"))

    def select_step(self, path) -> None:
        """Select the step at ``path`` (as produced by the pre-flight check)."""
        action = self._at(list(path)) if path else None
        self.render(action)
        if action is not None:
            self.tree.scrollToItem(self._find_item(action))

    # --------------------------------------------------------------- rendering
    def _render_if_alive(self, select) -> None:
        """A deferred render: the editor may have been closed meanwhile."""
        import shiboken6

        if shiboken6.isValid(self) and shiboken6.isValid(self.tree):
            self.render(select)

    def render(self, select: Action | list[Action] | None = None) -> None:
        """Rebuild the step list; ``select`` is the step (or steps) to select."""
        self.tree.blockSignals(True)
        self.tree.clear()
        for action in self.actions:
            self.tree.addTopLevelItem(self._make_item(action))
        self.tree.expandAll()
        wanted = [select] if isinstance(select, Action) else list(select or [])
        items = [i for i in (self._find_item(a) for a in wanted) if i is not None]
        if items:
            self.tree.setCurrentItem(items[-1])
            for item in items:
                item.setSelected(True)
        self.tree.blockSignals(False)
        self._update_buttons()

    def _make_item(self, action: Action) -> QTreeWidgetItem:
        spec = catalog.spec_for(action.type)
        breakpoint = self.has_breakpoint(action)
        item = QTreeWidgetItem([(BREAKPOINT_MARK if breakpoint else "") + catalog.describe(action)])
        item.setData(0, ROLE_ACTION, action)
        if breakpoint:
            item.setToolTip(0, "Breakpoint: debugging stops before this step")
        item.setData(0, ROLE_KIND, "action")
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled
        if spec.container and not spec.has_else:
            flags |= Qt.ItemFlag.ItemIsDropEnabled
        item.setFlags(flags)
        if action.type == "comment":
            font = QFont(item.font(0))
            font.setItalic(True)
            item.setFont(0, font)
            item.setForeground(0, Qt.GlobalColor.gray)
        elif spec.category == "Flow":
            font = QFont(item.font(0))
            font.setBold(True)
            item.setFont(0, font)
        if breakpoint:
            item.setForeground(0, QColor("#c62828"))
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

    def _undo_tooltip(self, text: str) -> None:
        self.undo_btn.setToolTip(f"Undo {text} (Ctrl+Z)" if text else "Nothing to undo")

    def _redo_tooltip(self, text: str) -> None:
        self.redo_btn.setToolTip(f"Redo {text}" if text else "Nothing to redo")

    def _before_drop(self) -> None:
        self._drop_state = self._state()

    def _after_drop(self) -> None:
        before = self._drop_state or self._state()
        self._drop_state = None
        self.actions = self._collect(None)
        selected = self._selected_actions()
        self._record_change("Move steps", before)
        QTimer.singleShot(0, lambda: self._render_if_alive(selected))

    # ------------------------------------------------------------------ undo
    def _path(self, action: Action) -> list[tuple[str, int]] | None:
        """Where ``action`` is: e.g. [("actions", 2), ("children", 0)]."""
        def search(items: list[Action], prefix: list) -> list | None:
            for i, a in enumerate(items):
                here = [*prefix, i]
                if a is action:
                    return here
                for key in ("children", "else_children"):
                    found = search(getattr(a, key), [*here, key])
                    if found:
                        return found
            return None

        return search(self.actions, [])

    def _at(self, path: list) -> Action | None:
        items, action = self.actions, None
        try:
            for step in path:
                if isinstance(step, str):
                    items = getattr(action, step)
                else:
                    action = items[step]
        except (IndexError, AttributeError):
            return None
        return action

    def _state(self) -> dict:
        return {
            "actions": [a.to_dict() for a in self.actions],
            "selected": [p for p in (self._path(a) for a in self._selected_actions()) if p],
        }

    def _restore(self, state: dict) -> None:
        kept = self._breakpoint_list_paths()  # breakpoints stay where they were
        self.actions = [Action.from_dict(d) for d in state["actions"]]
        self._breakpoints = [a for a in (self._at(p) for p in kept) if a is not None]
        self.render([a for a in (self._at(p) for p in state["selected"]) if a is not None])
        self._mark_dirty()

    def _record_change(self, text: str, before: dict) -> None:
        after = self._state()
        if after["actions"] != before["actions"]:
            self.undo_stack.push(_StepsChange(self, before, after, text))
            self._mark_dirty()

    def _change(self, text: str, mutate) -> object:
        """Run ``mutate`` (which changes the steps) as one undoable change."""
        before = self._state()
        result = mutate()
        self._record_change(text, before)
        return result

    # --------------------------------------------------------------- selection
    def _selected_item(self) -> QTreeWidgetItem | None:
        current = self.tree.currentItem()
        if current is not None and current.isSelected():
            return current
        items = self.tree.selectedItems()
        return items[-1] if items else None

    def _selected_actions(self) -> list[Action]:
        """The selected steps in list order, without those inside another selected one."""
        chosen = {id(i) for i in self.tree.selectedItems() if i.data(0, ROLE_KIND) == "action"}
        out: list[Action] = []

        def walk(item: QTreeWidgetItem, inside: bool) -> None:
            picked = id(item) in chosen and not inside
            if picked:
                out.append(item.data(0, ROLE_ACTION))
            for k in range(item.childCount()):
                walk(item.child(k), inside or picked)

        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i), False)
        return out

    def _selected_action(self) -> Action | None:
        """The one selected step (None when none or several are selected)."""
        selected = self._selected_actions()
        return selected[0] if len(selected) == 1 else None

    def _siblings_of(self, action: Action) -> list[Action]:
        return self._list_for(self._find_item(action).parent())  # type: ignore[union-attr]

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
        if spec.container and not spec.has_else and not action.children and len(self._selected_actions()) <= 1:
            return action.children, 0
        siblings = self._list_for(item.parent())
        return siblings, self._index_of(siblings, action) + 1

    @staticmethod
    def _index_of(items: list[Action], action: Action) -> int:
        return next(i for i, a in enumerate(items) if a is action)

    def _movable(self) -> bool:
        """Several steps move together only when they are in the same list."""
        selected = self._selected_actions()
        return bool(selected) and len({id(self._siblings_of(a)) for a in selected}) == 1

    def _update_buttons(self) -> None:
        selected = self._selected_actions()
        self.edit_btn.setEnabled(len(selected) == 1)
        for b in (self.dup_btn, self.del_btn):
            b.setEnabled(bool(selected))
        movable = self._movable()
        self.up_btn.setEnabled(movable)
        self.down_btn.setEnabled(movable)
        self.breakpoint_btn.setEnabled(bool(selected))

    # ------------------------------------------------------------- breakpoints
    def _breakpoint_list_paths(self) -> list[list]:
        return [p for p in (self._path(a) for a in self._breakpoints) if p]

    def breakpoint_paths(self) -> set[tuple]:
        """Where the breakpoints are, as the executor names steps: (2, "children", 0)."""
        return {tuple(p) for p in self._breakpoint_list_paths()}

    def has_breakpoint(self, action: Action) -> bool:
        return any(a is action for a in self._breakpoints)

    def toggle_breakpoint(self) -> None:
        """Set breakpoints on the selected steps, or remove them if all have one."""
        selected = self._selected_actions()
        if not selected:
            return
        if all(self.has_breakpoint(a) for a in selected):
            self._breakpoints = [b for b in self._breakpoints if not any(b is a for a in selected)]
        else:
            self._breakpoints += [a for a in selected if not self.has_breakpoint(a)]
        self.render(selected)

    def clear_breakpoints(self) -> None:
        self._breakpoints = []
        self.render(self._selected_actions())

    @property
    def debug_stop_at_start(self) -> bool:
        return self.stop_at_start_action.isChecked()

    @property
    def debug_simulate(self) -> bool:
        return self.simulate_action.isChecked()

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
        if edit and (catalog.spec_for(action_type).fields) and ActionDialog(
            action, self, self.macro_names()
        ).exec() != ActionDialog.DialogCode.Accepted:
            return None

        def insert() -> None:
            target, index = self._insertion_point()
            target.insert(index, action)

        self._change(f"Add {catalog.spec_for(action_type).label}", insert)
        self.render(action)
        return action

    def _edit_selected(self) -> None:
        action = self._selected_action()
        if action is None or not catalog.spec_for(action.type).fields:
            return
        before = self._state()
        if ActionDialog(action, self, self.macro_names()).exec() == ActionDialog.DialogCode.Accepted:
            self._record_change(f"Edit {catalog.spec_for(action.type).label}", before)
            self.render(action)

    def _duplicate(self) -> None:
        selected = self._selected_actions()
        if not selected:
            return

        def duplicate() -> list[Action]:
            copies = []
            for action in reversed(selected):  # from the end, so indices stay valid
                siblings = self._siblings_of(action)
                copy = action.clone()
                siblings.insert(self._index_of(siblings, action) + 1, copy)
                copies.insert(0, copy)
            return copies

        copies = self._change("Duplicate steps" if len(selected) > 1 else "Duplicate step", duplicate)
        self.render(copies)

    def _delete(self) -> None:
        selected = self._selected_actions()
        if not selected:
            return

        def delete() -> None:
            for action in selected:
                siblings = self._siblings_of(action)
                siblings.pop(self._index_of(siblings, action))

        self._change("Delete steps" if len(selected) > 1 else "Delete step", delete)
        self.render()

    def _move(self, delta: int) -> None:
        if not self._movable():
            return
        selected = self._selected_actions()
        siblings = self._siblings_of(selected[0])
        indices = sorted(self._index_of(siblings, a) for a in selected)
        if (delta < 0 and indices[0] == 0) or (delta > 0 and indices[-1] == len(siblings) - 1):
            return  # already at the edge: the block keeps its shape

        def move() -> None:
            for i in (indices if delta < 0 else reversed(indices)):
                siblings[i], siblings[i + delta] = siblings[i + delta], siblings[i]

        self._change("Move steps", move)
        self.render(selected)

    # ------------------------------------------------------ clipboard (steps)
    def copy_steps(self) -> None:
        selected = self._selected_actions()
        if selected:
            QApplication.clipboard().setText(json.dumps(
                {"format": CLIPBOARD_FORMAT, "steps": [a.to_dict() for a in selected]}, ensure_ascii=False))

    def cut_steps(self) -> None:
        if self._selected_actions():
            self.copy_steps()
            self._delete()

    def paste_steps(self) -> list[Action]:
        """Insert steps copied from this or another macro; ignores any other clipboard text."""
        try:
            data = json.loads(QApplication.clipboard().text())
            if not isinstance(data, dict) or data.get("format") != CLIPBOARD_FORMAT:
                return []
            steps = [Action.from_dict(d) for d in data.get("steps", [])]
        except (ValueError, TypeError):
            return []
        if not steps:
            return []

        def paste() -> None:
            target, index = self._insertion_point()
            target[index:index] = steps

        self._change("Paste steps", paste)
        self.render(steps)
        return steps

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
        review = RecordReviewDialog(recorded, self)
        if review.exec() != RecordReviewDialog.DialogCode.Accepted:
            return
        recorded = review.actions()

        def insert() -> None:
            target, index = self._insertion_point()
            target[index:index] = recorded

        self._change("Record steps", insert)
        self.render(recorded)

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
