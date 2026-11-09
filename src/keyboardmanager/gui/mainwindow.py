"""Main application window."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStyle,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
    QInputDialog,
)

from keyboardmanager.core import io as macro_io
from keyboardmanager.core import keys
from keyboardmanager.core import permissions
from keyboardmanager.core.database import Database
from keyboardmanager.core.models import Macro
from keyboardmanager.core.schedule import format_when
from keyboardmanager.core.service import MacroService
from keyboardmanager.gui.editor import MacroEditor, hotkey_conflict
from keyboardmanager.gui.history import HistoryView

ROLE_ID = Qt.ItemDataRole.UserRole


class _Bridge(QObject):
    """Moves service callbacks from worker / hotkey threads onto the GUI thread."""

    event = Signal(str, object)


class MainWindow(QMainWindow):
    TEST_DELAY_S = 3

    def __init__(self, db: Database, service: MacroService) -> None:
        super().__init__()
        self.db = db
        self.service = service
        self._macros: dict[int, Macro] = {}
        self._current_id: int | None = None
        self._quitting = False
        self.setWindowTitle("Keyboard Manager")
        self.resize(1100, 720)

        self._bridge = _Bridge(self)
        self._bridge.event.connect(self._on_service_event)
        service.listener = self._bridge.event.emit

        # --- left: macro list
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search macros...")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._refresh_list)
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._on_current_changed)
        self.new_btn = QPushButton("New")
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
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(self.search)
        left_layout.addWidget(self.list, 1)
        left_layout.addLayout(row1)
        left_layout.addLayout(row2)

        # --- right: tabs
        self.editor = MacroEditor()
        self.history = HistoryView(db)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.editor, "Editor")
        self.tabs.addTab(self.history, "History")
        self.tabs.currentChanged.connect(lambda i: self.history.refresh() if i == 1 else None)

        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 800])
        self.setCentralWidget(splitter)

        # --- toolbar / status
        toolbar = self.addToolBar("Main")
        toolbar.setMovable(False)
        self.hotkeys_action = QAction("Global hotkeys", self, checkable=True, checked=True)
        self.hotkeys_action.toggled.connect(self._toggle_hotkeys)
        self.schedules_action = QAction("Schedules", self, checkable=True, checked=True)
        self.schedules_action.setToolTip("Run macros automatically at their scheduled times")
        self.schedules_action.toggled.connect(self._toggle_schedules)
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
        toolbar.addAction(self.schedules_action)
        toolbar.addAction(self.pause_action)
        toolbar.addAction(self.stop_action)
        toolbar.addAction(stop_key_action)
        toolbar.addAction(pause_key_action)
        self.status_label = QLabel()
        self.statusBar().addPermanentWidget(self.status_label)

        # --- wiring
        self.new_btn.clicked.connect(self._new_macro)
        self.dup_btn.clicked.connect(self._duplicate_macro)
        self.del_btn.clicked.connect(self._delete_macro)
        self.run_btn.clicked.connect(self._run_selected)
        self.import_btn.clicked.connect(self._import)
        self.export_btn.clicked.connect(self._export)
        self.editor.saveRequested.connect(self.save_current)
        self.editor.runRequested.connect(self._test_run)
        self.editor.modified.connect(self._update_schedule_label)

        self._setup_tray()
        self._reload_macros()
        self.service.reload()
        self._update_status()
        self._select(None)
        # Cmd+Q, Dock > Quit and logging out send the application a Quit event
        QApplication.instance().installEventFilter(self)
        self._check_permissions()

    # ------------------------------------------------------------- permissions
    def _check_permissions(self) -> None:
        missing = permissions.missing_permissions()
        if not missing:
            return
        warning = QLabel("Permissions missing")
        warning.setStyleSheet("color: #c62828; font-weight: bold;")
        warning.setToolTip(permissions.explain(missing))
        self.statusBar().addPermanentWidget(warning)
        self.permission_warning = warning
        QTimer.singleShot(0, lambda: QMessageBox.warning(self, "Permissions needed", permissions.explain(missing)))

    # -------------------------------------------------------------------- tray
    def _setup_tray(self) -> None:
        self.tray: QSystemTrayIcon | None = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.setWindowIcon(icon)
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("Keyboard Manager")
        from PySide6.QtWidgets import QMenu

        menu = QMenu()
        menu.addAction("Show", self._show_window)
        menu.addAction(self.pause_action)
        menu.addAction(self.stop_action)
        menu.addAction("Quit", self.quit)
        self._tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self._show_window() if reason == QSystemTrayIcon.ActivationReason.Trigger else None
        )
        self.tray.show()

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
        if (
            event.type() == QEvent.Type.Quit
            and obj is QApplication.instance()
            and not self._quitting
        ):
            # The whole application is quitting (not just this window closing):
            # ask about unsaved edits, then let closeEvent accept instead of hiding
            # to the tray. Ignoring the event cancels the quit (and a logout).
            if not self._confirm_discard():
                event.ignore()
                return True
            self._quitting = True
        return super().eventFilter(obj, event)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        if self._quitting or self.tray is None:
            if not self._quitting:
                if not self._confirm_discard():
                    event.ignore()
                    return
                self._quitting = True
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
        self._refresh_list()

    def _refresh_list(self) -> None:
        needle = self.search.text().strip().lower()
        self.list.blockSignals(True)
        self.list.clear()
        for macro in self._macros.values():
            if needle and needle not in macro.name.lower() and needle not in macro.hotkey.lower():
                continue
            parts = [macro.name]
            if macro.hotkey:
                parts.append(f"[{macro.hotkey}]")
            if macro.schedule.active:
                parts.append("(scheduled)")
            if not macro.enabled:
                parts.append("(disabled)")
            item = QListWidgetItem("  ".join(parts))
            item.setData(ROLE_ID, macro.id)
            tip = [macro.scope_label]
            if macro.schedule.active:
                tip.append(f"Schedule: {macro.schedule.describe()}")
            tip.append(macro.description)
            item.setToolTip("\n".join(tip).strip())
            self.list.addItem(item)
            if macro.id == self._current_id:
                self.list.setCurrentItem(item)
        self.list.blockSignals(False)

    def _on_current_changed(self, current: QListWidgetItem | None, previous: QListWidgetItem | None) -> None:
        new_id = current.data(ROLE_ID) if current else None
        if new_id == self._current_id:
            return
        if not self._confirm_discard():
            self._refresh_list()  # restore the previous selection
            return
        self._select(new_id)

    def _select(self, macro_id: int | None) -> None:
        self._current_id = macro_id
        macro = self._macros.get(macro_id) if macro_id is not None else None
        self.editor.load(macro)
        self.editor.setEnabled(macro is not None)
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
        macro = Macro(name=self.db.unique_name("New macro"))
        self.db.save_macro(macro)
        self._reload_macros()
        self._select(macro.id)
        self.editor.name_edit.selectAll()
        self.editor.name_edit.setFocus()
        self.tabs.setCurrentWidget(self.editor)

    def _duplicate_macro(self) -> None:
        if self._current_id is None or not self._confirm_discard():
            return
        source = self._macros[self._current_id]
        copy = Macro.from_dict(source.to_dict())
        copy.name = self.db.unique_name(f"{source.name} copy")
        copy.hotkey = ""  # a duplicate must not steal the original's hotkey
        self.db.save_macro(copy)
        self._reload_macros()
        self._select(copy.id)
        self.service.reload()

    def _delete_macro(self) -> None:
        if self._current_id is None:
            return
        macro = self._macros[self._current_id]
        answer = QMessageBox.question(self, "Delete macro", f'Delete "{macro.name}"? This cannot be undone.')
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.db.delete_macro(macro.id)  # type: ignore[arg-type]
        self.editor.dirty = False
        self._reload_macros()
        self._select(None)
        self.service.reload()

    def save_current(self) -> bool:
        if self._current_id is None:
            return False
        try:
            macro = self.editor.build_macro()
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot save", str(exc))
            return False
        problem = hotkey_conflict(macro, list(self._macros.values()), self.service.reserved_hotkeys())
        if problem:
            QMessageBox.warning(self, "Hotkey conflict", problem)
            return False
        if any(m.id != macro.id and m.name.lower() == macro.name.lower() for m in self._macros.values()):
            QMessageBox.warning(self, "Cannot save", f'A macro named "{macro.name}" already exists.')
            return False
        self.db.save_macro(macro)
        self._reload_macros()
        self.editor.load(self._macros[macro.id])  # type: ignore[index]
        self._refresh_list()
        failures = self.service.reload()
        self._update_schedule_label()
        saved = f'Saved "{macro.name}"'
        if macro.schedule.active and macro.enabled:
            when = self.service.next_run(macro.id)  # type: ignore[arg-type]
            if when is None:
                saved += " - its scheduled time has already passed, so it will not run"
            elif self.service.schedules_enabled:
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
        if not self.service.run(macro, trigger=trigger, start_delay=self.TEST_DELAY_S):
            QMessageBox.information(self, "Busy", "Another macro is already running.")

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
        if event in ("started", "paused", "resumed"):
            macro: Macro = payload  # type: ignore[assignment]
            self._sync_run_controls()
            if self.service.running_macro is not None:
                self._show_running_message(macro)
        elif event == "finished":
            macro, result = payload  # type: ignore[misc]
            self._sync_run_controls()
            self.statusBar().showMessage(
                f'"{macro.name}": {result.status}'
                + (f" - {result.message}" if result.message and result.status == "error" else ""),
                8000,
            )
            if self.tabs.currentWidget() is self.history:
                self.history.refresh()
            if result.status == "error" and self.tray is not None and not self.isVisible():
                self.tray.showMessage(macro.name, result.message, QSystemTrayIcon.MessageIcon.Warning, 5000)
        elif event == "skipped":
            macro, reason = payload  # type: ignore[misc]
            self.statusBar().showMessage(f'"{macro.name}": {reason}', 8000)
            if self.tabs.currentWidget() is self.history:
                self.history.refresh()
        if event in ("hotkeys", "schedule", "finished", "skipped"):
            self._update_status()
            self._update_schedule_label()

    # ---------------------------------------------------------- hotkey settings
    def _toggle_hotkeys(self, on: bool) -> None:
        self.service.set_hotkeys_enabled(on)

    def _toggle_schedules(self, on: bool) -> None:
        self.service.set_schedules_enabled(on)

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
        elif not self.service.schedules_enabled:
            text = "Schedules are switched off"
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
            self.service.set_pause_hotkey(hotkey)
        except ValueError as exc:
            QMessageBox.warning(self, "Pause hotkey", str(exc))
            return
        self._sync_run_controls()
        if self.service.running_macro is not None:
            self._show_running_message(self.service.running_macro)

    def _check_unused_by_macros(self, hotkey: str) -> None:
        for m in self._macros.values():
            if m.hotkey == hotkey:
                raise ValueError(f'{hotkey} is already used by "{m.name}".')

    def _update_status(self) -> None:
        failures = self.service.hotkey_failures
        if not self.service.hotkeys_enabled:
            text = "Global hotkeys: off"
        elif failures:
            text = f"Global hotkeys: {len(failures)} problem(s)"
        else:
            active = sum(1 for m in self._macros.values() if m.enabled and m.hotkey)
            pause = self.service.pause_hotkey
            text = (
                f"Global hotkeys: on ({active} macro hotkeys, stop = {self.service.stop_hotkey}"
                + (f", pause = {pause})" if pause else ")")
            )
        text += " | " + self._schedule_status()
        self.status_label.setText(text)
        self.status_label.setToolTip("\n".join(f"{k}: {v}" for k, v in failures.items()))

    def _schedule_status(self) -> str:
        if not self.service.schedules_enabled:
            return "Schedules: off"
        upcoming = self.service.scheduler.upcoming()
        if not upcoming:
            return "Schedules: none"
        when, macro = upcoming[0]
        return f'Schedules: {len(upcoming)}, next "{macro.name}" {format_when(when, self.service.clock())}'

    # ------------------------------------------------------------ import/export
    def _export(self) -> None:
        macros = list(self._macros.values())
        if not macros:
            QMessageBox.information(self, "Export", "There are no macros to export.")
            return
        choice = QMessageBox(self)
        choice.setWindowTitle("Export")
        choice.setText("What do you want to export?")
        only = choice.addButton("Selected macro", QMessageBox.ButtonRole.AcceptRole)
        everything = choice.addButton("All macros", QMessageBox.ButtonRole.AcceptRole)
        choice.addButton(QMessageBox.StandardButton.Cancel)
        only.setEnabled(self._current_id is not None)
        choice.exec()
        clicked = choice.clickedButton()
        if clicked is only:
            macros = [self._macros[self._current_id]]  # type: ignore[index]
        elif clicked is not everything:
            return
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
        self._reload_macros()
        self._select(result.imported[-1].id if result.imported else self._current_id)
        self.service.reload()
        lines = [f"Imported {len(result.imported)} macro(s)."]
        if result.renamed:
            lines.append(f"{result.renamed} renamed because the name already existed.")
        if result.hotkeys_cleared:
            lines.append(f"{result.hotkeys_cleared} hotkey(s) removed because they were already in use.")
        if result.scheduled:
            lines.append(
                f"{result.scheduled} imported macro(s) have a schedule and will run automatically. "
                "Review them, or switch off Schedules in the toolbar."
            )
        if result.skipped:
            lines.append("\nSkipped:\n" + "\n".join(result.skipped))
        QMessageBox.information(self, "Import", "\n".join(lines))
