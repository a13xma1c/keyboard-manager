"""Main application window."""

from __future__ import annotations

import os
import sys

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QKeySequence, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QMenu,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QPlainTextEdit,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QComboBox,
    QDoubleSpinBox,
    QTreeWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStyle,
    QSystemTrayIcon,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QInputDialog,
)

from keyboardmanager.core import io as macro_io
from keyboardmanager.core import keys
from keyboardmanager.core import os_shortcuts, permissions
from keyboardmanager.core.database import Database
from keyboardmanager.core.models import Macro
from keyboardmanager.core.actions import new_action
from keyboardmanager.core.expander import normalize_abbreviation
from keyboardmanager.core.organize import MAX_SPEED, MIN_SPEED, moved_path, normalize_folder, parent_folder
from keyboardmanager.core.preflight import ERROR, check_macro
from keyboardmanager.core.schedule import format_when
from keyboardmanager.core.service import MacroService
from keyboardmanager.gui.editor import MacroEditor, abbreviation_conflict, hotkey_conflict
from keyboardmanager.gui.macrotree import FAVORITES, ROLE_FOLDER, ROLE_ID, MacroTree
from keyboardmanager.gui.preflight import PreflightView
from keyboardmanager.gui.remapping import RemapView
from keyboardmanager.gui.uibridge import QtUserInterface
from keyboardmanager.gui.variables import VariablesView
from keyboardmanager.gui.history import HistoryView
from keyboardmanager.gui.connect import ConnectView
from keyboardmanager.gui import theme as theme_mod
from keyboardmanager.gui.profiles import ProfilesView
from keyboardmanager.gui.runview import RunView
from keyboardmanager.gui.triggers import TriggersView
from keyboardmanager.core.debugger import Debugger
from keyboardmanager.gui.theme import set_tone


# How the process ends on macOS once quitting is complete (see MainWindow._finish_quit).
# Replaced in tests, which run this code on Macs too.
EXIT_PROCESS = os._exit


class _Bridge(QObject):
    """Moves service callbacks from worker / hotkey threads onto the GUI thread."""

    event = Signal(str, object)


class SnippetDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("New snippet")
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        hint = QLabel("When you type the abbreviation anywhere, it is replaced by the text.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        form = QFormLayout()
        self.abbreviation = QLineEdit()
        self.abbreviation.setPlaceholderText("e.g. ;sig")
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("e.g. Best regards,\nAlex")
        form.addRow("Abbreviation", self.abbreviation)
        form.addRow("Text", self.text)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def macro(self, name: str) -> Macro:
        """The snippet as a macro; raises ``ValueError`` when incomplete."""
        abbreviation = normalize_abbreviation(self.abbreviation.text())
        if not abbreviation:
            raise ValueError("Please enter an abbreviation.")
        text = self.text.toPlainText()
        if not text:
            raise ValueError("Please enter the text that replaces the abbreviation.")
        step = new_action("type_text")
        step.params["text"] = text
        return Macro(name, [step], abbreviation=abbreviation,
                     description=f'Replaces "{abbreviation}" with text')


class MainWindow(QMainWindow):
    TEST_DELAY_S = 3

    def __init__(self, db: Database, service: MacroService) -> None:
        super().__init__()
        self.db = db
        self.service = service
        self._macros: dict[int, Macro] = {}
        self._current_id: int | None = None
        self._quitting = False
        self._debug_breakpoints: set[tuple] = set()  # shown in the Run tab for the next debug run
        self.setWindowTitle("Keyboard Manager")
        available = self.screen().availableGeometry() if self.screen() is not None else None
        if available is not None and available.width() > 400:  # never larger than the screen
            self.resize(min(1180, available.width() - 40), min(860, available.height() - 40))
        else:
            self.resize(1180, 860)

        self._bridge = _Bridge(self)
        self._bridge.event.connect(self._on_service_event)
        service.listener = self._bridge.event.emit
        # the look first, so every widget below is created in it
        self.theme = theme_mod.ThemeManager.instance()
        self.theme.apply(db.get_setting(theme_mod.APPEARANCE_SETTING, "glass"),
                         db.get_setting(theme_mod.ACCENT_SETTING, "system"))
        self.theme.changed.connect(self._theme_changed)

        # --- left: macro list
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search macros... (#tag for a tag)")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._refresh_list)
        self.filter_combo = QComboBox()
        self.filter_combo.setToolTip("Show all macros, the favorites, or those with a tag")
        self.filter_combo.currentIndexChanged.connect(self._refresh_list)
        self.list = MacroTree()
        self.list.moved.connect(self._move_to_folder)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._list_menu)
        delete_shortcut = QAction(self.list)
        delete_shortcut.setShortcut(QKeySequence.StandardKey.Delete)
        delete_shortcut.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        delete_shortcut.triggered.connect(self._delete_macro)
        self.list.addAction(delete_shortcut)
        self.list.currentItemChanged.connect(self._on_current_changed)
        self.new_btn = QPushButton("New")
        self.snippet_btn = QPushButton("New snippet...")
        self.snippet_btn.setToolTip("Text expansion: an abbreviation that is replaced by a text")
        self.dup_btn = QPushButton("Duplicate")
        self.del_btn = QPushButton("Delete")
        self.run_btn = QPushButton("Run")
        self.import_btn = QPushButton("Import...")
        self.export_btn = QPushButton("Export...")
        row1, row2 = QHBoxLayout(), QHBoxLayout()
        for b in (self.new_btn, self.dup_btn, self.del_btn):
            row1.addWidget(b)
        for b in (self.run_btn, self.import_btn, self.export_btn):
            row2.addWidget(b)
        self.folder_btn = QPushButton("New folder...")
        row0 = QHBoxLayout()
        row0.addWidget(self.snippet_btn)
        row0.addWidget(self.folder_btn)
        left = QWidget()
        left.setObjectName("glassSidebar")  # a floating glass panel in the Liquid Glass look
        left.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.sidebar = left
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(10, 10, 10, 10)
        left_layout.addWidget(self.search)
        left_layout.addWidget(self.filter_combo)
        left_layout.addWidget(self.list, 1)
        left_layout.addLayout(row0)
        left_layout.addLayout(row1)
        left_layout.addLayout(row2)

        # --- right: tabs
        self.editor = MacroEditor()
        self.history = HistoryView(db, log_steps=(lambda: service.log_steps, service.set_log_steps))
        self.tabs = QTabWidget()
        self.tabs.setObjectName("mainTabs")
        self.tabs.addTab(self.editor, "Editor")
        self.run_view = RunView(service)
        self.tabs.addTab(self.run_view, "Run")
        self.tabs.addTab(self.history, "History")
        self.triggers_view = TriggersView(service, lambda: sorted(m.name for m in self._macros.values()))
        self.tabs.addTab(self.triggers_view, "Triggers")
        self.profiles_view = ProfilesView(service)
        self.tabs.addTab(self.profiles_view, "Profiles")
        self.connect_view = ConnectView(service, self._selected_ids, self._after_share_import, db.path)
        self.tabs.addTab(self.connect_view, "Sync && Remote")
        self.remap_view = RemapView(service)
        self.tabs.addTab(self.remap_view, "Remapping")
        self.variables_view = VariablesView(service.saved_variables, evaluate=service.evaluate)
        self.tabs.addTab(self.variables_view, "Variables")
        self.check_view = PreflightView(service, lambda: list(self._macros.values()))
        self.check_view.navigate.connect(self._show_issue)
        self.tabs.addTab(self.check_view, "Check")
        self._check_timer = QTimer(self)
        self._check_timer.setSingleShot(True)
        self._check_timer.setInterval(300)
        self._check_timer.timeout.connect(self._run_checks)
        self.editor.modified.connect(self._schedule_check)
        self.editor.issues_btn.clicked.connect(lambda: self.tabs.setCurrentWidget(self.check_view))
        self.editor.macro_names = lambda: sorted(m.name for m in self._macros.values())
        self.tabs.currentChanged.connect(
            lambda i: self.history.refresh() if self.tabs.widget(i) is self.history else None)

        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 800])
        splitter.setHandleWidth(12)
        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(12, 6, 12, 4)
        central_layout.addWidget(splitter)
        self.setCentralWidget(central)

        # --- toolbar / status
        toolbar = self.addToolBar("Main")
        toolbar.setObjectName("glassToolbar")
        toolbar.setMovable(False)
        self.toolbar = toolbar
        self.hotkeys_action = QAction("Global hotkeys", self, checkable=True, checked=True)
        self.hotkeys_action.setToolTip("Hotkeys and abbreviations (text expansion)")
        self.hotkeys_action.toggled.connect(self._toggle_hotkeys)
        self.autorun_action = QAction("Auto-run", self, checkable=True, checked=True)
        self.autorun_action.setToolTip("Run macros by themselves: schedules and events")
        self.autorun_action.toggled.connect(self._toggle_autorun)
        self.stop_action = QAction("Stop running macro", self)
        self.stop_action.triggered.connect(service.stop)
        self.stop_action.setEnabled(False)
        self.pause_action = QAction("Pause", self)
        self.pause_action.triggered.connect(self._toggle_pause)
        self.pause_action.setEnabled(False)
        stop_key_action = QAction("Stop hotkey...", self)
        stop_key_action.triggered.connect(self._change_stop_hotkey)
        pause_key_action = QAction("Pause hotkey...", self)
        pause_key_action.triggered.connect(self._change_pause_hotkey)
        toolbar.addAction(self.hotkeys_action)
        toolbar.addAction(self.autorun_action)
        toolbar.addAction(self.pause_action)
        toolbar.addAction(self.stop_action)
        # the stop / pause hotkey settings share one menu button: a compact toolbar
        self.safety_keys_btn = QToolButton()
        self.safety_keys_btn.setText("Safety hotkeys")
        self.safety_keys_btn.setToolTip("Change the global hotkeys that stop or pause a running macro")
        self.safety_keys_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        safety_menu = QMenu(self.safety_keys_btn)
        safety_menu.addAction(stop_key_action)
        safety_menu.addAction(pause_key_action)
        self.safety_keys_btn.setMenu(safety_menu)
        self.stop_key_action, self.pause_key_action = stop_key_action, pause_key_action
        toolbar.addWidget(self.safety_keys_btn)
        toolbar.addSeparator()
        self.speed_box = QDoubleSpinBox()
        self.speed_box.setRange(MIN_SPEED, MAX_SPEED)
        self.speed_box.setSingleStep(0.25)
        self.speed_box.setDecimals(2)
        self.speed_box.setPrefix("Speed ")
        self.speed_box.setSuffix("x")
        self.speed_box.setToolTip("Playback speed of all macros (delays and typing); "
                                  "applies at once, also to a running macro")
        self.speed_box.setValue(service.playback_speed)
        self.speed_box.setMaximumWidth(130)
        self.speed_box.valueChanged.connect(self._set_speed)
        toolbar.addWidget(self.speed_box)
        toolbar.addSeparator()
        self.profile_combo = QComboBox()
        self.profile_combo.setToolTip("The active profile: only its macros' hotkeys, abbreviations, "
                                      "schedules, events and triggers work (manage them in the Profiles tab)")
        self.profile_combo.activated.connect(self._profile_chosen)
        self.profile_combo.setMaximumWidth(180)
        toolbar.addWidget(self.profile_combo)
        toolbar.addSeparator()
        self.appearance_btn = QToolButton()
        self.appearance_btn.setText("Appearance")
        self.appearance_btn.setToolTip("Liquid Glass (light / dark / following the system) or the classic look, "
                                       "and the accent colour")
        self.appearance_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.appearance_btn.setMenu(self._build_appearance_menu())
        toolbar.addWidget(self.appearance_btn)
        self.status_label = QLabel()
        self.statusBar().addPermanentWidget(self.status_label)

        # --- wiring
        self.new_btn.clicked.connect(self._new_macro)
        self.snippet_btn.clicked.connect(self._new_snippet)
        self.folder_btn.clicked.connect(self._new_folder)
        self.dup_btn.clicked.connect(self._duplicate_macro)
        self.del_btn.clicked.connect(self._delete_macro)
        self.run_btn.clicked.connect(self._run_selected)
        self.import_btn.clicked.connect(self._import)
        self.export_btn.clicked.connect(self._export)
        self.editor.saveRequested.connect(self.save_current)
        self.editor.runRequested.connect(self._test_run)
        self.editor.debugRequested.connect(self._debug)
        self.editor.detachRequested.connect(self._detach_current)
        self.editor.modified.connect(self._update_schedule_label)

        self._refresh_profile_combo()
        self._setup_tray()
        # dialogs, clipboard and notifications for running macros (on the GUI thread)
        service.ui = QtUserInterface(self, notify=self._notify)
        self._reload_macros()
        self.service.reload()
        self._update_status()
        self._select(None)
        # Cmd+Q, Dock > Quit and logging out send the application a Quit event
        QApplication.instance().installEventFilter(self)
        self._check_permissions()
        self.service.apply_remaps()
        self.remap_view.show_status()
        self.service.fire_startup_event()  # macros with the "when Keyboard Manager starts" event

    # -------------------------------------------------------------- appearance
    def _build_appearance_menu(self) -> QMenu:
        menu = QMenu(self)
        group = QActionGroup(menu)
        self.appearance_actions: dict[str, QAction] = {}
        for key, label in theme_mod.APPEARANCES.items():
            action = menu.addAction(label, lambda k=key: self.set_appearance(k))
            action.setCheckable(True)
            group.addAction(action)
            self.appearance_actions[key] = action
        menu.addSeparator()
        accents = menu.addMenu("Accent colour")
        accent_group = QActionGroup(accents)
        self.accent_actions: dict[str, QAction] = {}
        for key, value in theme_mod.ACCENTS.items():
            label = value if key == "system" else key.capitalize()
            action = accents.addAction(label, lambda k=key: self.set_accent(k))
            action.setCheckable(True)
            accent_group.addAction(action)
            self.accent_actions[key] = action
        self._sync_appearance_menu()
        return menu

    def _sync_appearance_menu(self) -> None:
        for key, action in getattr(self, "appearance_actions", {}).items():
            action.setChecked(key == self.theme.appearance)
        for key, action in getattr(self, "accent_actions", {}).items():
            action.setChecked(key == self.theme.accent)
            action.setEnabled(self.theme.appearance != "classic")

    def set_appearance(self, appearance: str) -> None:
        self.db.set_setting(theme_mod.APPEARANCE_SETTING, appearance)
        self.theme.apply(appearance, self.theme.accent)
        self._sync_appearance_menu()

    def set_accent(self, accent: str) -> None:
        self.db.set_setting(theme_mod.ACCENT_SETTING, accent)
        self.theme.apply(self.theme.appearance, accent)
        self._sync_appearance_menu()

    def _theme_changed(self) -> None:
        self._sync_appearance_menu()
        self.run_view.theme_changed()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        if not self.theme.glass:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        panels = []
        for widget, grow in ((self.sidebar, 0), (self._tab_page_frame(), 7)):
            if widget is not None and widget.isVisible():
                rect = QRectF(widget.mapTo(self, widget.rect().topLeft()), widget.size().toSizeF())
                panels.append(rect.adjusted(-grow, -grow, grow, grow))
        pill = self._toolbar_pill()
        if pill is not None:
            panels.append(pill)
        theme_mod.paint_backdrop(painter, self.rect(), self.theme.palette, panels)
        if pill is not None:
            theme_mod.paint_glass(painter, pill, self.theme.palette, pill.height() / 2)
        painter.end()

    def _toolbar_pill(self) -> QRectF | None:
        """Where the toolbar's floating capsule is drawn (inside its inset)."""
        if not self.toolbar.isVisible():
            return None
        left, top, right, bottom = theme_mod.TOOLBAR_INSET
        rect = QRectF(self.toolbar.geometry())
        return rect.adjusted(left, top, -right, -bottom)

    def _tab_page_frame(self):
        """The tab widget's page area (the glass pane is drawn around it)."""
        return self.tabs.currentWidget().parentWidget() if self.tabs.currentWidget() else None

    # ------------------------------------------------------------- permissions
    def _check_permissions(self) -> None:
        missing = permissions.missing_permissions()
        if not missing:
            return
        warning = QLabel("Permissions missing")
        set_tone(warning, "danger")
        warning.setStyleSheet("font-weight: bold;")
        warning.setToolTip(permissions.explain(missing))
        self.statusBar().addPermanentWidget(warning)
        self.permission_warning = warning
        QTimer.singleShot(0, lambda: QMessageBox.warning(self, "Permissions needed", permissions.explain(missing)))

    # -------------------------------------------------------------------- tray
    def _setup_tray(self) -> None:
        self.tray: QSystemTrayIcon | None = None
        self.favorites_menu: QMenu | None = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.setWindowIcon(icon)
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("Keyboard Manager")
        menu = QMenu()
        menu.addAction("Show", self._show_window)
        self.favorites_menu = menu.addMenu("Run favorite")
        menu.addAction(self.pause_action)
        menu.addAction(self.stop_action)
        menu.addAction("Quit", self.quit)
        self._tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self._show_window() if reason == QSystemTrayIcon.ActivationReason.Trigger else None
        )
        self.tray.show()

    def _notify(self, title: str, message: str) -> None:
        """"Show notification" steps: a tray balloon, or the status bar without a tray."""
        if self.tray is not None:
            self.tray.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, 5000)
        else:
            self.statusBar().showMessage(f"{title}: {message}", 8000)

    def _show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit(self) -> None:
        if not self._confirm_discard():
            return
        self._quitting = True
        self.service.shutdown()
        if self.tray is not None:
            self.tray.hide()
        QApplication.quit()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt API
        if event.type() == QEvent.Type.Quit and obj is QApplication.instance():
            # The whole application is quitting (Cmd+Q, Dock > Quit, logout, the tray's
            # Quit): ask about unsaved edits, then let closeEvent accept instead of
            # hiding to the tray. Ignoring the event cancels the quit (and a logout).
            if not self._quitting:
                if not self._confirm_discard():
                    event.ignore()
                    return True
                self._quitting = True
            self._finish_quit()
        return super().eventFilter(obj, event)

    def _finish_quit(self) -> None:
        """Final clean-up once the application is really quitting. Runs once: the
        event filter that calls it is removed first.

        On macOS quitting goes through ``[NSApp terminate:]``, which ends the process
        with ``exit()`` from inside the event loop. ``exit()`` then runs PySide's C++
        static destructors, which destroy the remaining widgets while the bindings are
        being torn down, and any Python code reached from there (an event filter, a
        signal handler) crashes the process (SIGSEGV). So the application-wide event
        filter is removed, everything is cleaned up here, and on macOS the process
        ends right away instead of going through that teardown.
        """
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self._stop_timers()
        self.service.shutdown()  # stops listeners and releases keys a macro holds
        if sys.platform == "darwin":
            self.db.close()
            for stream in (sys.stdout, sys.stderr):
                try:
                    stream.flush()
                except Exception:  # noqa: BLE001 - e.g. no console
                    pass
            EXIT_PROCESS(0)

    def _stop_timers(self) -> None:
        """Nothing may poll the service or the database once quitting has begun."""
        for timer in (self._check_timer, self.run_view.timer, self.triggers_view.timer):
            timer.stop()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if self._quitting or self.tray is None:
            if not self._quitting:
                if not self._confirm_discard():
                    event.ignore()
                    return
                self._quitting = True
            self._stop_timers()
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)  # it sees every event of the application
            self.service.shutdown()
            event.accept()
            QApplication.quit()
            return
        # keep running in the tray so global hotkeys stay active
        event.ignore()
        self.hide()
        self.tray.showMessage("Keyboard Manager", "Still running - global hotkeys and schedules remain active.",
                              QSystemTrayIcon.MessageIcon.Information, 3000)

    # -------------------------------------------------------------------- list
    def _reload_macros(self) -> None:
        self._macros = {m.id: m for m in self.db.list_macros() if m.id is not None}
        self._refresh_filters()
        self._refresh_list()
        self.editor.set_folders(self._all_folders())
        self.editor.set_layers(self.service.all_layers())
        self._refresh_profile_combo()
        self._refresh_favorites_menu()
        self._schedule_check()

    def _all_folders(self) -> list[str]:
        return self.service.folders.all(m.folder for m in self._macros.values())

    def _refresh_filters(self) -> None:
        """All / Favorites / one entry per tag in use."""
        current = self.filter_combo.currentData()
        tags = sorted({t for m in self._macros.values() for t in m.tags})
        self.filter_combo.blockSignals(True)
        self.filter_combo.clear()
        self.filter_combo.addItem("All macros", "")
        self.filter_combo.addItem("\u2605 Favorites", FAVORITES)
        for tag in tags:
            self.filter_combo.addItem(f"#{tag}", tag)
        index = self.filter_combo.findData(current)
        self.filter_combo.setCurrentIndex(max(0, index))
        self.filter_combo.blockSignals(False)

    def _selected_ids(self) -> list[int]:
        """Macros selected in the list (in list order); the current one if none."""
        ids = self.list.selected_ids()
        return ids or ([self._current_id] if self._current_id is not None else [])

    def _refresh_list(self) -> None:
        self.list.populate(
            list(self._macros.values()), self._all_folders(), self.search.text(),
            self.filter_combo.currentData() or "", self._current_id, set(self.list.selected_ids()),
            active_profile=self.service.active_profile,
        )

    def _on_current_changed(self, current: QTreeWidgetItem | None, previous: QTreeWidgetItem | None) -> None:
        if current is not None and current.data(0, ROLE_ID) is None:
            return  # a folder: the editor keeps showing the macro
        new_id = current.data(0, ROLE_ID) if current else None
        if new_id == self._current_id:
            return
        if not self._confirm_discard():
            self.list.clearSelection()
            self._refresh_list()  # restore the previous selection
            return
        self._select(new_id, keep_selection=True)  # Ctrl/Shift+click may be selecting several

    def _select(self, macro_id: int | None, keep_selection: bool = False) -> None:
        """Show ``macro_id`` in the editor. Unless the user is extending the selection,
        the list then selects just that macro."""
        if not keep_selection:
            self.list.clearSelection()
        self._current_id = macro_id
        macro = self._macros.get(macro_id) if macro_id is not None else None
        self.editor.load(macro)
        self.editor.setEnabled(macro is not None)
        team = self.service.team_of(macro) if macro is not None else None
        self.editor.set_read_only(
            f'Team macro from "{team.name}" - it follows the team library and cannot be changed here.'
            if team is not None else ("Team macro from a library you left." if macro is not None and macro.source
                                      else ""))
        self._update_schedule_label()
        for b in (self.dup_btn, self.del_btn, self.run_btn, self.export_btn):
            b.setEnabled(macro is not None or b is self.export_btn)
        self._refresh_list()

    def _confirm_discard(self) -> bool:
        if not self.editor.dirty or not self.editor.isEnabled():
            return True
        answer = QMessageBox.question(
            self, "Unsaved changes", "Discard the unsaved changes to this macro?",
            QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Discard

    # ----------------------------------------------------------------- actions
    def _new_macro(self) -> None:
        if not self._confirm_discard():
            return
        macro = Macro(name=self.db.unique_name("New macro"), folder=self.list.selected_folder() or "")
        self.db.save_macro(macro)
        self._reload_macros()
        self._select(macro.id)
        self.editor.name_edit.selectAll()
        self.editor.name_edit.setFocus()
        self.tabs.setCurrentWidget(self.editor)

    def _new_snippet(self) -> None:
        """Text expansion in one step: an abbreviation and the text that replaces it."""
        if not self._confirm_discard():
            return
        dialog = SnippetDialog(self)
        while dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                macro = dialog.macro(self.db.unique_name(f"Snippet {dialog.abbreviation.text().strip()}"))
            except ValueError as exc:
                QMessageBox.warning(self, "New snippet", str(exc))
                continue
            problem = abbreviation_conflict(macro, list(self._macros.values()))
            if problem:
                QMessageBox.warning(self, "New snippet", problem)
                continue
            self.db.save_macro(macro)
            self._reload_macros()
            self._select(macro.id)
            self.service.reload()
            self.statusBar().showMessage(f'Typing "{macro.abbreviation}" now inserts the text', 6000)
            return

    def _duplicate_macro(self) -> None:
        if self._current_id is None or not self._confirm_discard():
            return
        source = self._macros[self._current_id]
        copy = Macro.from_dict(source.to_dict())
        copy.name = self.db.unique_name(f"{source.name} copy")
        copy.hotkey = ""  # a duplicate must not steal the original's hotkey
        copy.abbreviation = ""  # ...or its abbreviation
        copy.source = ""  # a copy of a team macro is your own
        self.db.save_macro(copy)
        self.service.request_sync()
        self._reload_macros()
        self._select(copy.id)
        self.service.reload()

    def _delete_macro(self) -> None:
        macros = [self._macros[i] for i in self._selected_ids() if i in self._macros]
        if not macros:
            return
        if len(macros) == 1:
            question = f'Delete "{macros[0].name}"?'
        else:
            names = ", ".join(m.name for m in macros[:5]) + (", ..." if len(macros) > 5 else "")
            question = f"Delete {len(macros)} macros ({names})?"
        answer = QMessageBox.question(self, "Delete", question + " This cannot be undone.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        for macro in macros:
            self.db.delete_macro(macro.id)  # type: ignore[arg-type]
        self.service.request_sync()
        self.editor.dirty = False
        self.list.clearSelection()
        self._current_id = None
        self._reload_macros()
        self._select(None)
        self.service.reload()
        self.statusBar().showMessage(f"Deleted {len(macros)} macro(s)", 4000)

    # ---------------------------------------------------------------- folders
    def _folder_menu(self, folder: str, position) -> None:
        menu = QMenu(self)
        menu.addAction("New subfolder...", lambda: self._new_folder(folder))
        menu.addAction("Rename...", lambda: self._rename_folder(folder))
        menu.addAction("Delete folder", lambda: self._delete_folder(folder))
        self._last_menu = menu
        menu.popup(self.list.viewport().mapToGlobal(position))

    def _new_folder(self, inside: str | None = None) -> None:
        inside = self.list.selected_folder() if inside is None else inside
        where = f' in "{inside}"' if inside else ""
        name, ok = QInputDialog.getText(self, "New folder", f"Folder name{where} (a/b makes nested folders):")
        if not ok:
            return
        try:
            path = self.service.folders.add(f"{inside}/{name}" if inside else name)
        except ValueError as exc:
            QMessageBox.warning(self, "New folder", str(exc))
            return
        self._reload_macros()
        self.statusBar().showMessage(f'Created folder "{path}"', 4000)

    def _rename_folder(self, folder: str) -> None:
        name, ok = QInputDialog.getText(self, "Rename folder", "New name:", text=folder.rsplit("/", 1)[-1])
        if not ok:
            return
        parent = parent_folder(folder)
        try:
            new = normalize_folder(f"{parent}/{name}" if parent else name)
            if not new:
                raise ValueError("please give the folder a name")
        except ValueError as exc:
            QMessageBox.warning(self, "Rename folder", str(exc))
            return
        if new != folder:
            self._relocate(folder, new)

    def _delete_folder(self, folder: str) -> None:
        """Removes the folder; what was in it moves up one level (nothing is deleted)."""
        inside = [m for m in self._macros.values() if m.folder == folder or m.folder.startswith(folder + "/")]
        parent = parent_folder(folder)
        target = f'"{parent}"' if parent else "the top level"
        question = f'Delete the folder "{folder}"?'
        if inside:
            question += f" Its {len(inside)} macro(s) move to {target}."
        if QMessageBox.question(self, "Delete folder", question) != QMessageBox.StandardButton.Yes:
            return
        self._relocate(folder, parent)
        self.service.folders.remove(folder)
        self._reload_macros()

    def _relocate(self, old: str, new: str) -> None:
        """Move folder ``old`` (with its subfolders and macros) to ``new``."""
        changed = [m for m in self._macros.values() if m.folder == old or m.folder.startswith(old + "/")]
        if self._current_id in {m.id for m in changed} and not self._confirm_discard():
            return
        for macro in changed:
            macro.folder = normalize_folder(moved_path(macro.folder, old, new))
            self.db.save_macro(macro)
        self.service.folders.rename(old, new)
        if new:
            self.service.folders.add(new)
        self._reload_macros()
        if self._current_id in self._macros:
            self.editor.load(self._macros[self._current_id], keep_history=True)

    def _move_to_folder(self, ids: list[int], folder: str) -> None:
        """Drag and drop (or "Move to folder") of macros."""
        ids = [i for i in ids if i in self._macros and self._macros[i].folder != folder]
        if not ids or (self._current_id in ids and not self._confirm_discard()):
            return
        for macro_id in ids:
            macro = self._macros[macro_id]
            macro.folder = folder
            self.db.save_macro(macro)
        self._reload_macros()
        if self._current_id in ids:
            self.editor.load(self._macros[self._current_id], keep_history=True)
        where = f'"{folder}"' if folder else "the top level"
        self.statusBar().showMessage(f"Moved {len(ids)} macro(s) to {where}", 4000)

    def _ask_move(self, ids: list[int]) -> None:
        folders = ["(top level)", *self._all_folders()]
        choice, ok = QInputDialog.getItem(self, "Move to folder", "Folder (or type a new one):", folders, 0, True)
        if not ok:
            return
        try:
            folder = "" if choice == "(top level)" else normalize_folder(choice)
        except ValueError as exc:
            QMessageBox.warning(self, "Move to folder", str(exc))
            return
        self._move_to_folder(ids, folder)

    # -------------------------------------------------------------- favorites
    def _set_favorite(self, ids: list[int], favorite: bool) -> None:
        if self._current_id in ids and not self._confirm_discard():
            return
        for macro_id in ids:
            macro = self._macros[macro_id]
            macro.favorite = favorite
            self.db.save_macro(macro)
        self._reload_macros()
        if self._current_id in ids:
            self.editor.load(self._macros[self._current_id], keep_history=True)

    def _refresh_favorites_menu(self) -> None:
        """The tray's "Run favorite" submenu: one click runs a macro (after the usual countdown)."""
        if self.favorites_menu is None:
            return
        self.favorites_menu.clear()
        favorites = sorted((m for m in self._macros.values() if m.favorite), key=lambda m: m.name.lower())
        for macro in favorites:
            self.favorites_menu.addAction(macro.name, lambda m=macro: self._start(m, "favorite"))
        if not favorites:
            self.favorites_menu.addAction("(no favorites yet)").setEnabled(False)

    # ------------------------------------------------------- speed and checks
    def _set_speed(self, speed: float) -> None:
        self.service.set_playback_speed(speed)
        self._schedule_check()  # durations shown by the check depend on the speed

    def _schedule_check(self, *_args) -> None:
        if hasattr(self, "_check_timer"):
            self._check_timer.start()

    def _run_checks(self) -> None:
        self.check_view.refresh()
        self.editor.show_issues(self._editor_issues())

    def _editor_issues(self):
        """Issues of the macro as it is in the editor (unsaved changes included)."""
        if self._current_id is None:
            return []
        try:
            macro = self.editor.build_macro()
        except ValueError as exc:
            from keyboardmanager.core.preflight import Issue

            return [Issue(ERROR, str(exc), self._current_id, self.editor.name_edit.text())]
        others = [m for m in self._macros.values() if m.id != macro.id] + [macro]
        return check_macro(macro, self.check_view.context(others))

    def _show_issue(self, macro_id: int, step: tuple) -> None:
        """Double-click in the Check tab: open the macro and select the step."""
        if macro_id not in self._macros:
            return
        if macro_id != self._current_id:
            if not self._confirm_discard():
                return
            self._select(macro_id)
        self.tabs.setCurrentWidget(self.editor)
        self.editor.select_step(step)

    def _set_enabled(self, enabled: bool) -> None:
        """Enable or disable the selected macros (their hotkeys, schedules, ...)."""
        ids = self._selected_ids()
        if self._current_id in ids and not self._confirm_discard():
            return
        for macro_id in ids:
            macro = self._macros[macro_id]
            if macro.enabled != enabled:
                macro.enabled = enabled
                self.db.save_macro(macro)
        self._reload_macros()
        if self._current_id in ids:
            self.editor.load(self._macros[self._current_id])
        self.service.reload()
        self.statusBar().showMessage(f"{'Enabled' if enabled else 'Disabled'} {len(ids)} macro(s)", 4000)

    def _list_menu(self, position) -> None:
        item = self.list.itemAt(position)
        if item is not None and item.data(0, ROLE_FOLDER) is not None:
            self._folder_menu(item.data(0, ROLE_FOLDER), position)
            return
        ids = self._selected_ids()
        if not ids:
            return
        single = len(ids) == 1
        menu = QMenu(self)
        favorite = all(self._macros[i].favorite for i in ids if i in self._macros)
        menu.addAction("Remove from favorites" if favorite else "Add to favorites",
                       lambda: self._set_favorite(ids, not favorite))
        menu.addAction("Move to folder...", lambda: self._ask_move(ids))
        menu.addSeparator()
        run = menu.addAction("Run", self._run_selected)
        dup = menu.addAction("Duplicate", self._duplicate_macro)
        run.setEnabled(single)
        dup.setEnabled(single)
        menu.addSeparator()
        menu.addAction("Enable", lambda: self._set_enabled(True))
        menu.addAction("Disable", lambda: self._set_enabled(False))
        menu.addAction("Export..." if single else f"Export {len(ids)} macros...", self._export_selected)
        menu.addAction("Copy share code", self._copy_share_code)
        own = [i for i in ids if i in self._macros and not self._macros[i].source]
        team_copies = [i for i in ids if i in self._macros and self._macros[i].source]
        for sub in self.service.teams:
            action = menu.addAction(f'Publish to team "{sub.name}"', lambda s=sub: self._publish(own, s.id))
            action.setEnabled(bool(own))
            published = [i for i in own if self.db.sync_get(sub.scope, self._macros[i].uid) is not None]
            if published:
                menu.addAction(f'Remove from team "{sub.name}"', lambda s=sub, p=published: self._unpublish(p, s.id))
        if team_copies:
            menu.addAction("Edit locally (make it mine)", lambda: self._detach(team_copies))
        menu.addSeparator()
        menu.addAction("Delete" if single else f"Delete {len(ids)} macros", self._delete_macro)
        self._last_menu = menu  # kept for tests
        menu.popup(self.list.viewport().mapToGlobal(position))

    def save_current(self) -> bool:
        if self._current_id is None:
            return False
        if self.editor.read_only:
            QMessageBox.information(self, "Team macro", 'Team macros cannot be changed here: use "Edit locally" '
                                                        "to make it your own first.")
            return False
        try:
            macro = self.editor.build_macro()
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot save", str(exc))
            return False
        problem = hotkey_conflict(macro, list(self._macros.values()), self.service.reserved_hotkeys()) or (
            abbreviation_conflict(macro, list(self._macros.values()))
        )
        if problem:
            QMessageBox.warning(self, "Hotkey conflict", problem)
            return False
        previous = self._macros.get(macro.id) if macro.id is not None else None
        if macro.hotkey and (previous is None or previous.hotkey != macro.hotkey):
            if not self._accept_os_shortcut(macro.hotkey, "Hotkey"):
                return False
        if any(m.id != macro.id and m.name.lower() == macro.name.lower() for m in self._macros.values()):
            QMessageBox.warning(self, "Cannot save", f'A macro named "{macro.name}" already exists.')
            return False
        self.db.save_macro(macro)
        self.service.request_sync()
        renamed = 0
        if previous is not None and previous.name != macro.name:
            renamed = self.service.rename_in_triggers(previous.name, macro.name)
        self._reload_macros()
        self.editor.load(self._macros[macro.id], keep_history=True)  # type: ignore[index]
        self._refresh_list()
        failures = self.service.reload()
        self._update_schedule_label()
        saved = f'Saved "{macro.name}"'
        if renamed:
            saved += f" ({renamed} trigger(s) updated to the new name)"
        if macro.schedule.active and macro.enabled:
            when = self.service.next_run(macro.id)  # type: ignore[arg-type]
            if when is None:
                saved += " - its scheduled time has already passed, so it will not run"
            elif self.service.autorun_enabled:
                saved += f" - next run {format_when(when, self.service.clock())}"
        self.statusBar().showMessage(saved, 6000)
        if macro.hotkey in failures:
            QMessageBox.warning(self, "Hotkey", f"Could not register {macro.hotkey}: {failures[macro.hotkey]}")
        return True

    # --------------------------------------------------------------------- run
    def _run_selected(self) -> None:
        if self._current_id is None:
            return
        self._start(self._macros[self._current_id], "manual")

    def _test_run(self) -> None:
        try:
            macro = self.editor.build_macro()
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot run", str(exc))
            return
        self._start(macro, "test run")

    def _start(self, macro: Macro, trigger: str) -> None:
        errors = [i for i in check_macro(macro, self.check_view.context()) if i.blocks_run]
        if errors:  # the run would fail: say why instead of starting it
            lines = "\n".join(f"\u2022 {i.message}" for i in errors[:5])
            QMessageBox.warning(self, "Cannot run", f'"{macro.name}" has problems:\n{lines}\n\nSee the Check tab.')
            self.tabs.setCurrentWidget(self.check_view)
            return
        if self.service.run(macro, trigger=trigger, start_delay=self.TEST_DELAY_S, queue=False):
            return
        running = self.service.running_macro
        answer = QMessageBox.question(
            self, "Busy",
            f'"{running.name if running else "Another macro"}" is running. Queue "{macro.name}" to run '
            "when it has finished?",
        )
        if answer == QMessageBox.StandardButton.Yes and self.service.run(
            macro, trigger=trigger, start_delay=self.TEST_DELAY_S, queue=True
        ):
            self.statusBar().showMessage(f'Queued "{macro.name}"', 5000)

    def _debug(self) -> None:
        """Run the macro as edited under the debugger, showing it in the Run tab."""
        try:
            macro = self.editor.build_macro()
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot debug", str(exc))
            return
        errors = [i for i in check_macro(macro, self.check_view.context()) if i.blocks_run]
        if errors:
            lines = "\n".join(f"\u2022 {i.message}" for i in errors[:5])
            QMessageBox.warning(self, "Cannot debug", f'"{macro.name}" has problems:\n{lines}')
            return
        if self.service.running_macro is not None:
            QMessageBox.information(self, "Busy", "Another macro is running; stop it before debugging.")
            return
        breakpoints = self.editor.breakpoint_paths()
        debugger = Debugger(breakpoints, stop_at_start=self.editor.debug_stop_at_start)
        simulate = self.editor.debug_simulate
        self._debug_breakpoints = breakpoints
        self.service.run(macro, trigger="debug", start_delay=0 if simulate else self.TEST_DELAY_S,
                         queue=False, debugger=debugger, simulate=simulate)
        self.tabs.setCurrentWidget(self.run_view)

    def _toggle_pause(self) -> None:
        self.service.toggle_pause()

    def _sync_run_controls(self) -> None:
        """Match the stop / pause controls to the service state.

        Events arrive queued from other threads, so the state is re-read instead of
        trusting the order of events (e.g. a late "paused" after "finished").
        """
        running = self.service.running_macro is not None
        paused = running and self.service.paused
        self.stop_action.setEnabled(running)
        self.pause_action.setEnabled(running)
        self.pause_action.setText("Resume" if paused else "Pause")
        hotkey = self.service.pause_hotkey
        self.pause_action.setToolTip(
            ("Resume the paused macro" if paused else "Pause the running macro")
            + (f" ({hotkey})" if hotkey else "")
        )
        if self.tray is not None:
            self.tray.setToolTip("Keyboard Manager - macro paused" if paused else "Keyboard Manager")

    def _show_running_message(self, macro: Macro) -> None:
        if self.service.paused:
            hotkey = self.service.pause_hotkey
            how = f"press {hotkey} or Resume" if hotkey else "press Resume"
            self.statusBar().showMessage(f'Paused "{macro.name}" - {how} to continue')
        else:
            self.statusBar().showMessage(f'Running "{macro.name}"...')

    def _on_service_event(self, event: str, payload: object) -> None:
        if self.service.closed:  # late events while quitting (the database may be closed)
            return
        if event == "started":
            breakpoints, self._debug_breakpoints = self._debug_breakpoints, set()
            self.run_view.started(breakpoints)
        elif event == "queue":
            self.run_view.show_queue(payload)  # type: ignore[arg-type]
        if event in ("started", "paused", "resumed"):
            macro: Macro = payload  # type: ignore[assignment]
            self._sync_run_controls()
            self.run_view.refresh()
            if self.service.running_macro is not None:
                self._show_running_message(macro)
        elif event == "finished":
            macro, result = payload  # type: ignore[misc]
            self._sync_run_controls()
            self.run_view.finished(macro, result.status, result.message)
            self.statusBar().showMessage(
                f'"{macro.name}": {result.status}'
                + (f" - {result.message}" if result.message and result.status == "error" else ""),
                8000,
            )
            if self.tabs.currentWidget() is self.history:
                self.history.refresh()
            self.variables_view.refresh()  # a macro may have changed saved variables
            if result.status == "error" and self.tray is not None and not self.isVisible():
                self.tray.showMessage(macro.name, result.message, QSystemTrayIcon.MessageIcon.Warning, 5000)
        elif event == "remaps":
            self.remap_view.show_status()
        elif event == "library":  # sync / a team library changed macros here
            current = self._current_id
            self._reload_macros()
            if not self.editor.dirty:
                self._select(current if current in self._macros else None)
            self.statusBar().showMessage(str(payload), 8000)
            self.connect_view.teams_page.refresh()
            self._update_status()
        elif event == "sync":
            self.connect_view.sync_page.refresh()
            if payload.failed:  # type: ignore[attr-defined]
                self.statusBar().showMessage(payload.summary(), 8000)  # type: ignore[attr-defined]
        elif event == "remote":
            self.connect_view.remote_page.refresh()
        elif event in ("profile", "layer"):
            self._refresh_profile_combo()
            self.profiles_view.refresh()
            self._refresh_list()
            self._update_status()
        elif event == "skipped":
            macro, reason = payload  # type: ignore[misc]
            self.statusBar().showMessage(f'"{macro.name}": {reason}', 8000)
            if self.tabs.currentWidget() is self.history:
                self.history.refresh()
        if event in ("hotkeys", "schedule", "finished", "skipped"):
            self._update_status()
            self._update_schedule_label()

    # ------------------------------------------------------ sharing and teams
    def _copy_share_code(self) -> None:
        code = self.service.share_code(self._selected_ids())
        QApplication.clipboard().setText(code)
        self.statusBar().showMessage(f"Share code copied ({len(code)} characters): paste it anywhere", 6000)

    def _publish(self, ids: list[int], team_id: str) -> None:
        if not ids:
            return
        lines = self.service.publish(ids, team_id)
        conflicts = [line for line in lines if line.endswith("not published")]
        if conflicts:
            answer = QMessageBox.question(
                self, "Publish", "\n".join(lines) + "\n\nPublish anyway, replacing the library's version?")
            if answer == QMessageBox.StandardButton.Yes:
                stale = [i for i in ids if any(f'"{self._macros[i].name}"' in line for line in conflicts)]
                lines = self.service.publish(stale, team_id, force=True)
            else:
                return
        QMessageBox.information(self, "Publish", "\n".join(lines))

    def _unpublish(self, ids: list[int], team_id: str) -> None:
        names = ", ".join(f'"{self._macros[i].name}"' for i in ids)
        answer = QMessageBox.question(self, "Remove from team", f"Remove {names} from the team library? "
                                      "Team members lose their copies at the next update; your macro stays.")
        if answer != QMessageBox.StandardButton.Yes:
            return
        for macro_id in ids:
            self.service.unpublish(macro_id, team_id)
        self.statusBar().showMessage(f"Removed {names} from the team library", 6000)

    def _detach(self, ids: list[int]) -> None:
        for macro_id in ids:
            self.service.detach(macro_id)
        self._reload_macros()
        self._select(ids[-1] if ids[-1] in self._macros else None)

    def _detach_current(self) -> None:
        if self._current_id is not None:
            self._detach([self._current_id])

    # --------------------------------------------------------------- profiles
    def _refresh_profile_combo(self) -> None:
        active = self.service.active_profile
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItem("All profiles", "")
        for name in self.service.all_profiles():
            self.profile_combo.addItem(f"Profile: {name}", name)
        index = next((i for i in range(self.profile_combo.count())
                      if self.profile_combo.itemData(i).lower() == active.lower()), -1)
        if index < 0:  # a context rule switched to a profile not in the list
            self.profile_combo.addItem(f"Profile: {active}", active)
            index = self.profile_combo.count() - 1
        self.profile_combo.setCurrentIndex(index)
        self.profile_combo.blockSignals(False)

    def _profile_chosen(self, index: int) -> None:
        self.service.set_active_profile(self.profile_combo.itemData(index) or "")

    # ---------------------------------------------------------- hotkey settings
    def _toggle_hotkeys(self, on: bool) -> None:
        self.service.set_hotkeys_enabled(on)

    def _toggle_autorun(self, on: bool) -> None:
        self.service.set_autorun_enabled(on)

    def _update_schedule_label(self) -> None:
        """Next run of the macro in the editor, next to its schedule."""
        saved = self._macros.get(self._current_id) if self._current_id is not None else None
        text = ""
        try:
            shown = self.editor.schedule_edit.schedule()
        except ValueError:
            shown = None
        if saved is None:
            pass
        elif shown != saved.schedule:
            text = "Save to apply"
        elif not saved.schedule.active:
            pass
        elif not saved.enabled:
            text = "Macro disabled"
        elif not self.service.autorun_enabled:
            text = "Auto-run is switched off"
        else:
            when = self.service.next_run(saved.id)  # type: ignore[arg-type]
            text = f"Next run: {format_when(when, self.service.clock())}" if when else "No upcoming run"
        self.editor.schedule_status.setText(text)

    def _change_stop_hotkey(self) -> None:
        text, ok = QInputDialog.getText(
            self, "Stop hotkey", "Hotkey that stops a running macro (e.g. ctrl+alt+f12):",
            text=self.service.stop_hotkey,
        )
        if not ok:
            return
        try:
            hotkey = keys.normalize_hotkey(text)
            self._check_unused_by_macros(hotkey)
            if not self._accept_os_shortcut(hotkey, "Stop hotkey"):
                return
            self.service.set_stop_hotkey(hotkey)
        except ValueError as exc:
            QMessageBox.warning(self, "Stop hotkey", str(exc))

    def _change_pause_hotkey(self) -> None:
        text, ok = QInputDialog.getText(
            self, "Pause hotkey",
            "Hotkey that pauses / resumes a running macro (e.g. ctrl+alt+f11).\nLeave empty for none:",
            text=self.service.pause_hotkey,
        )
        if not ok:
            return
        try:
            hotkey = keys.normalize_hotkey(text) if text.strip() else ""
            if hotkey:
                self._check_unused_by_macros(hotkey)
                if not self._accept_os_shortcut(hotkey, "Pause hotkey"):
                    return
            self.service.set_pause_hotkey(hotkey)
        except ValueError as exc:
            QMessageBox.warning(self, "Pause hotkey", str(exc))
            return
        self._sync_run_controls()
        if self.service.running_macro is not None:
            self._show_running_message(self.service.running_macro)

    def _accept_os_shortcut(self, hotkey: str, title: str) -> bool:
        """Ask before using a well-known system / application shortcut."""
        problem = os_shortcuts.warning(hotkey)
        if problem is None:
            return True
        answer = QMessageBox.question(self, title, f"{problem[0].upper()}{problem[1:]}.\n\nUse it anyway?")
        return answer == QMessageBox.StandardButton.Yes

    def _check_unused_by_macros(self, hotkey: str) -> None:
        for m in self._macros.values():
            if m.hotkey and hotkey in m.hotkey.split():
                raise ValueError(f'{hotkey} is already used by "{m.name}" ({m.hotkey}).')

    def _update_status(self) -> None:
        failures = self.service.hotkey_failures
        if not self.service.hotkeys_enabled:
            text = "Global hotkeys: off"
        elif failures:
            text = f"Global hotkeys: {len(failures)} problem(s)"
        else:
            macros = [m for m in self._macros.values() if self.service.macro_active(m)]
            active = sum(1 for m in macros if m.hotkey)
            abbreviations = sum(1 for m in macros if m.abbreviation)
            pause = self.service.pause_hotkey
            text = (
                f"Global hotkeys: on ({active} macro hotkeys"
                + (f", {abbreviations} abbreviations" if abbreviations else "")
                + f", stop = {self.service.stop_hotkey}"
                + (f", pause = {pause})" if pause else ")")
            )
        text += " | " + self._schedule_status()
        layer = self.service.active_layer
        if layer:
            text += f" | layer: {layer}"
        self.status_label.setText(text)
        self.status_label.setToolTip("\n".join(f"{k}: {v}" for k, v in failures.items()))

    def _schedule_status(self) -> str:
        if not self.service.autorun_enabled:
            return "Auto-run: off"
        parts = []
        upcoming = self.service.scheduler.upcoming()
        if upcoming:
            when, macro = upcoming[0]
            parts.append(f'{len(upcoming)} scheduled, next "{macro.name}" {format_when(when, self.service.clock())}')
        on_event = sum(1 for m in self._macros.values() if m.enabled and m.event)
        if on_event:
            parts.append(f"{on_event} on events")
        rules = self.service.triggers.active_count()
        if rules:
            parts.append(f"{rules} trigger{'s' if rules != 1 else ''}")
        return "Auto-run: " + ("; ".join(parts) if parts else "nothing")

    # ------------------------------------------------------------ import/export
    def _export(self) -> None:
        macros = list(self._macros.values())
        if not macros:
            QMessageBox.information(self, "Export", "There are no macros to export.")
            return
        choice = QMessageBox(self)
        choice.setWindowTitle("Export")
        choice.setText("What do you want to export?")
        selected = [self._macros[i] for i in self._selected_ids() if i in self._macros]
        only = choice.addButton(
            "Selected macro" if len(selected) <= 1 else f"Selected macros ({len(selected)})",
            QMessageBox.ButtonRole.AcceptRole,
        )
        everything = choice.addButton("All macros", QMessageBox.ButtonRole.AcceptRole)
        choice.addButton(QMessageBox.StandardButton.Cancel)
        only.setEnabled(bool(selected))
        choice.exec()
        clicked = choice.clickedButton()
        if clicked is only:
            macros = selected
        elif clicked is not everything:
            return
        self._export_to_file(macros)

    def _export_selected(self) -> None:
        macros = [self._macros[i] for i in self._selected_ids() if i in self._macros]
        if macros:
            self._export_to_file(macros)

    def _export_to_file(self, macros: list[Macro]) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export macros", "macros.json", "Macro files (*.json)")
        if not path:
            return
        try:
            macro_io.export_to_file(macros, path)
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self.statusBar().showMessage(f"Exported {len(macros)} macro(s) to {path}", 6000)

    def _import(self) -> None:
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Import macros", "", "Macro files (*.json);;All files (*)")
        if not path:
            return
        try:
            result = macro_io.import_from_file(path, self.db)
        except macro_io.ImportError_ as exc:
            QMessageBox.critical(self, "Import failed", str(exc))
            return
        self._after_import(result, "Import")

    def _after_share_import(self, result) -> None:
        self._after_import(result, "Import share code")

    def _after_import(self, result, title: str) -> None:
        self._reload_macros()
        self._select(result.imported[-1].id if result.imported else self._current_id)
        self.service.reload()
        self.service.request_sync()
        lines = [f"Imported {len(result.imported)} macro(s)."]
        if result.renamed:
            lines.append(f"{result.renamed} renamed because the name already existed.")
        if result.hotkeys_cleared:
            lines.append(f"{result.hotkeys_cleared} hotkey(s) removed because they were already in use.")
        if result.abbreviations_cleared:
            lines.append(
                f"{result.abbreviations_cleared} abbreviation(s) removed because they clash with existing ones."
            )
        if result.scheduled:
            lines.append(
                f"{result.scheduled} imported macro(s) have a schedule or event and will run "
                "by themselves. Review them, or switch off Auto-run in the toolbar."
            )
        if result.skipped:
            lines.append("\nSkipped:\n" + "\n".join(result.skipped))
        QMessageBox.information(self, title, "\n".join(lines))
