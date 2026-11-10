"""Sync & Remote tab: sync between computers, team libraries, share codes, remote
control, and starting in the background."""

from __future__ import annotations

import os
import socket
from datetime import datetime
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from keyboardmanager.core import share
from keyboardmanager.core.autostart import Autostart, launch_command
from keyboardmanager.core.database import default_db_path
from keyboardmanager.core.service import MacroService
from keyboardmanager.core.sync import SyncFolder, SyncError
from keyboardmanager.core.team import TeamError
from keyboardmanager.gui.theme import set_tone


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(True)
    set_tone(label, "secondary")
    return label


def lan_address() -> str:
    """This computer's address on the local network (best guess, no traffic sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # UDP connect: only picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return socket.gethostname()


class SyncPage(QWidget):
    def __init__(self, service: MacroService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("e.g. ~/Dropbox/Keyboard Manager  (the same folder on every computer)")
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(browse)
        self.device_edit = QLineEdit()
        self.device_edit.setPlaceholderText(service.device_name)
        self.interval_spin = QSpinBox()
        self.interval_spin.setRange(0, 24 * 60)
        self.interval_spin.setSuffix(" min")
        self.interval_spin.setSpecialValueText("only after changes and by hand")
        self.settings_check = QCheckBox("Also sync profiles, context rules and trigger rules")
        self.save_btn = QPushButton("Apply")
        self.save_btn.clicked.connect(self.apply)
        self.sync_btn = QPushButton("Sync now")
        self.sync_btn.clicked.connect(self.sync_now)
        self.last_label = QLabel()
        self.last_label.setWordWrap(True)
        self.report = QPlainTextEdit()
        self.report.setReadOnly(True)
        self.report.setMaximumHeight(110)
        self.devices = QTableWidget(0, 3)
        self.devices.setHorizontalHeaderLabels(("Computer", "Last sync", "Macros"))
        self.devices.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.devices.verticalHeader().setVisible(False)
        self.devices.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        form = QFormLayout()
        form.addRow("Sync folder", folder_row)
        form.addRow("This computer", self.device_edit)
        form.addRow("Sync every", self.interval_spin)
        form.addRow("", self.settings_check)
        buttons = QHBoxLayout()
        buttons.addWidget(self.save_btn)
        buttons.addWidget(self.sync_btn)
        buttons.addStretch(1)
        layout = QVBoxLayout(self)
        layout.addWidget(_hint(
            "Keeps your macros the same on all your computers through a folder that a sync "
            "service (Dropbox, iCloud Drive, OneDrive, Syncthing...) or a network drive shares. "
            "Changes on both sides are merged macro by macro; when the same macro was changed "
            "on two computers, both versions are kept."))
        layout.addLayout(form)
        layout.addLayout(buttons)
        layout.addWidget(self.last_label)
        layout.addWidget(self.report)
        layout.addWidget(QLabel("Computers using this folder:"))
        layout.addWidget(self.devices, 1)
        self.refresh()

    def refresh(self) -> None:
        svc = self.service
        self.folder_edit.setText(svc.sync_folder)
        self.device_edit.setText(svc.db.get_setting("sync_device", ""))
        self.interval_spin.setValue(svc.sync_interval)
        self.settings_check.setChecked(svc.sync_settings_too)
        self.sync_btn.setEnabled(bool(svc.sync_folder or svc.teams))
        last = svc.last_sync()
        if last.get("time"):
            when = datetime.fromtimestamp(float(last["time"])).strftime("%Y-%m-%d %H:%M:%S")
            self.last_label.setText(f"Last sync {when}: {last.get('summary', '')}")
            set_tone(self.last_label, "danger" if last.get("failed") else None)
        else:
            self.last_label.setText("Not synced yet.")
        self._show_devices()

    def _show_devices(self) -> None:
        rows: list[dict] = []
        if self.service.sync_folder:
            try:
                rows = SyncFolder(self.service.sync_folder).devices()
            except SyncError:
                rows = []
        self.devices.setRowCount(len(rows))
        for row, d in enumerate(rows):
            for col, value in enumerate((d.get("device", ""), str(d.get("last_sync", "")).replace("T", " ")[:19],
                                         str(d.get("macros", "")))):
                self.devices.setItem(row, col, QTableWidgetItem(value))

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Sync folder", self.folder_edit.text() or os.path.expanduser("~"))
        if path:
            self.folder_edit.setText(path)

    def apply(self) -> bool:
        try:
            self.service.set_sync_options(self.folder_edit.text(), self.device_edit.text(),
                                          self.interval_spin.value(), self.settings_check.isChecked())
        except ValueError as exc:
            QMessageBox.warning(self, "Sync", str(exc))
            return False
        self.refresh()
        return True

    def sync_now(self) -> None:
        if self.folder_edit.text().strip() != self.service.sync_folder and not self.apply():
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            outcome = self.service.sync_now()
        finally:
            QApplication.restoreOverrideCursor()
        self.show_outcome(outcome)

    def show_outcome(self, outcome) -> None:
        lines = [outcome.summary()]
        report = outcome.report
        if report is not None:
            for label, items in (("Sent", report.uploaded), ("Received", report.downloaded),
                                 ("Deleted here", report.deleted_here), ("Conflicts (both kept)", report.conflicts),
                                 ("Adjusted", report.adjusted), ("Problems", report.errors)):
                if items:
                    lines.append(f"{label}: " + ", ".join(items))
        self.report.setPlainText("\n".join(lines))
        self.refresh()


class TeamsPage(QWidget):
    def __init__(self, service: MacroService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self.author_edit = QLineEdit()
        self.author_edit.setPlaceholderText(service.device_name)
        self.author_edit.editingFinished.connect(lambda: service.set_author(self.author_edit.text()))
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(("Team", "Folder", "Team macros here"))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.join_btn = QPushButton("Join a library...")
        self.create_btn = QPushButton("Create a library...")
        self.leave_btn = QPushButton("Leave")
        self.pull_btn = QPushButton("Update now")
        self.join_btn.clicked.connect(self._join)
        self.create_btn.clicked.connect(self._create)
        self.leave_btn.clicked.connect(self._leave)
        self.pull_btn.clicked.connect(self._pull)
        buttons = QHBoxLayout()
        for b in (self.join_btn, self.create_btn, self.leave_btn, self.pull_btn):
            buttons.addWidget(b)
        buttons.addStretch(1)
        self.result_label = QLabel()
        self.result_label.setWordWrap(True)
        form = QFormLayout()
        form.addRow("Your name", self.author_edit)
        layout = QVBoxLayout(self)
        layout.addWidget(_hint(
            "A team library is a shared folder. Its macros appear here as read-only team macros "
            '(folder "Team/<name>") and follow the library\'s changes. Publish your own macros '
            'with right-click > Publish to team in the macro list; "Edit locally" makes a team '
            "macro yours so you can change it and publish it back."))
        layout.addLayout(form)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)
        layout.addWidget(self.result_label)
        self.refresh()

    def refresh(self) -> None:
        self.author_edit.setText(self.service.db.get_setting("team_author", ""))
        subs = self.service.teams
        counts: dict[str, int] = {}
        for m in self.service.db.list_macros():
            if m.source:
                counts[m.source] = counts.get(m.source, 0) + 1
        self.table.setRowCount(len(subs))
        for row, sub in enumerate(subs):
            for col, value in enumerate((sub.name, sub.path, str(counts.get(sub.source, 0)))):
                self.table.setItem(row, col, QTableWidgetItem(value))
        self._update_buttons()

    def _selected(self):
        rows = {i.row() for i in self.table.selectedItems()}
        subs = self.service.teams
        return subs[min(rows)] if rows and min(rows) < len(subs) else None

    def _update_buttons(self) -> None:
        self.leave_btn.setEnabled(self._selected() is not None)
        self.pull_btn.setEnabled(bool(self.service.teams))

    def _join(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Team library folder", os.path.expanduser("~"))
        if not path:
            return
        try:
            report = self.service.subscribe_team(path)
        except (TeamError, OSError) as exc:
            QMessageBox.warning(self, "Join team library", str(exc))
            return
        self.result_label.setText(f"Joined: {report.summary()}")
        self.refresh()

    def _create(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Folder for the new team library", os.path.expanduser("~"))
        if not path:
            return
        name, ok = QInputDialog.getText(self, "Create team library", "Name of the team:")
        if not ok or not name.strip():
            return
        try:
            self.service.create_team(path, name)
        except (TeamError, OSError) as exc:
            QMessageBox.warning(self, "Create team library", str(exc))
            return
        self.result_label.setText(f'Created "{name.strip()}". Share the folder with your team.')
        self.refresh()

    def _leave(self) -> None:
        sub = self._selected()
        if sub is None:
            return
        answer = QMessageBox.question(
            self, "Leave team library",
            f'Leave "{sub.name}"? Its team macros are removed from this computer '
            "(the library itself is not changed).")
        if answer == QMessageBox.StandardButton.Yes:
            removed = self.service.unsubscribe_team(sub.id)
            self.result_label.setText(f"Left {sub.name}: {removed} team macro(s) removed.")
            self.refresh()

    def _pull(self) -> None:
        outcome = self.service.sync_now()
        self.result_label.setText(outcome.summary())
        self.refresh()


class SharingPage(QWidget):
    def __init__(self, service: MacroService, selected_ids: Callable[[], list[int]],
                 on_imported: Callable[[object], None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self._selected_ids = selected_ids
        self._on_imported = on_imported
        self.copy_btn = QPushButton("Copy share code of the selected macros")
        self.copy_btn.clicked.connect(self.copy_code)
        self.code_edit = QPlainTextEdit()
        self.code_edit.setPlaceholderText("Paste a share code (KMSHARE1:...) here")
        self.import_btn = QPushButton("Import")
        self.import_btn.clicked.connect(self.import_code)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(_hint(
            "A share code is one line of text holding macros: send it by chat or mail, or paste "
            "it into a ticket. Importing never overwrites anything (names that exist get a "
            "number, hotkeys in use are dropped)."))
        layout.addWidget(self.copy_btn)
        layout.addWidget(self.code_edit, 1)
        layout.addWidget(self.import_btn)
        layout.addWidget(self.status)

    def copy_code(self) -> str:
        ids = self._selected_ids()
        if not ids:
            QMessageBox.information(self, "Share", "Select the macros to share in the list first.")
            return ""
        code = self.service.share_code(ids)
        QApplication.clipboard().setText(code)
        self.status.setText(f"Share code of {len(ids)} macro(s) copied ({len(code)} characters).")
        return code

    def import_code(self) -> None:
        text = self.code_edit.toPlainText()
        if not text.strip() and share.looks_like_code(QApplication.clipboard().text()):
            text = QApplication.clipboard().text()
        try:
            result = self.service.import_share_code(text)
        except ValueError as exc:
            QMessageBox.warning(self, "Import share code", str(exc))
            return
        self.code_edit.clear()
        self.status.setText(f"Imported {len(result.imported)} macro(s).")
        self._on_imported(result)


class RemotePage(QWidget):
    def __init__(self, service: MacroService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self.enable_check = QCheckBox("Allow remote control")
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.lan_check = QCheckBox("Also from other devices on my network (phone, tablet...)")
        self.scope_combo = QComboBox()
        self.scope_combo.addItem("All macros", "all")
        self.scope_combo.addItem("Only favorites", "favorites")
        self.apply_btn = QPushButton("Apply")
        self.apply_btn.clicked.connect(self.apply)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.token_edit = QLineEdit()
        self.token_edit.setReadOnly(True)
        self.new_token_btn = QPushButton("New token")
        self.new_token_btn.setToolTip("Everything using the old token stops working")
        self.new_token_btn.clicked.connect(self._new_token)
        token_row = QHBoxLayout()
        token_row.addWidget(self.token_edit, 1)
        token_row.addWidget(self.new_token_btn)
        self.url_edit = QLineEdit()
        self.url_edit.setReadOnly(True)
        self.copy_url_btn = QPushButton("Copy link")
        self.copy_url_btn.clicked.connect(lambda: QApplication.clipboard().setText(self.url_edit.text()))
        url_row = QHBoxLayout()
        url_row.addWidget(self.url_edit, 1)
        url_row.addWidget(self.copy_url_btn)
        form = QFormLayout()
        form.addRow("", self.enable_check)
        form.addRow("Port", self.port_spin)
        form.addRow("", self.lan_check)
        form.addRow("Remote macros", self.scope_combo)
        form.addRow("", self.apply_btn)
        form.addRow("Access token", token_row)
        form.addRow("Web panel", url_row)
        layout = QVBoxLayout(self)
        layout.addWidget(_hint(
            "Run and stop macros, switch profiles and layers from a browser, a script or the "
            'command line (keyboardmanager ctl run "Name"). Every request needs the access '
            "token; anyone who has it can run your macros, so only share it with your own devices. "
            "Remote runs count as triggers: limits apply, disabled macros are refused."))
        layout.addLayout(form)
        layout.addWidget(self.status)
        layout.addStretch(1)
        self.refresh()

    def refresh(self) -> None:
        settings = self.service.remote_settings()
        self.enable_check.setChecked(settings["enabled"])
        self.port_spin.setValue(settings["port"])
        self.lan_check.setChecked(settings["lan"])
        self.scope_combo.setCurrentIndex(max(0, self.scope_combo.findData(settings["scope"])))
        self.token_edit.setText(settings["token"])
        host = lan_address() if settings["lan"] else "127.0.0.1"
        self.url_edit.setText(self.service.remote_url(host))
        remote = self.service.remote
        if remote is not None:
            where = "this computer and your network" if remote.lan else "this computer only"
            self.status.setText(f"Listening on port {remote.port} ({where}).")
            set_tone(self.status, "success")
        elif self.service.remote_error:
            self.status.setText(self.service.remote_error)
            set_tone(self.status, "danger")
        else:
            self.status.setText("Remote control is off.")
            set_tone(self.status, None)

    def apply(self) -> None:
        if self.lan_check.isChecked() and self.enable_check.isChecked() and not self.service.remote_settings()["lan"]:
            answer = QMessageBox.question(
                self, "Remote control",
                "Other devices on your network will be able to reach Keyboard Manager (with the "
                "token). Only do this on networks you trust. Continue?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            self.service.set_remote_settings(self.enable_check.isChecked(), self.port_spin.value(),
                                             self.lan_check.isChecked(), self.scope_combo.currentData())
        except ValueError as exc:
            QMessageBox.warning(self, "Remote control", str(exc))
        self.refresh()

    def _new_token(self) -> None:
        answer = QMessageBox.question(self, "New token", "Make a new access token? Links and scripts "
                                                         "using the current one stop working.")
        if answer == QMessageBox.StandardButton.Yes:
            self.service.regenerate_remote_token()
            self.refresh()


class BackgroundPage(QWidget):
    def __init__(self, db_path: str, autostart: Autostart | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.autostart = autostart or Autostart()
        self.db_path = db_path
        self.login_check = QCheckBox("Start Keyboard Manager when I log in (window hidden, tray icon only)")
        self.login_check.toggled.connect(self._toggle)
        self.where = _hint("")
        layout = QVBoxLayout(self)
        layout.addWidget(self.login_check)
        layout.addWidget(self.where)
        layout.addWidget(_hint(
            "Closing the window keeps Keyboard Manager running in the tray, so hotkeys, "
            "schedules, triggers, sync and remote control keep working. To run it as a "
            "background service without any window (e.g. on a machine that should only "
            "react to schedules and remote control), start it with --headless. Only one "
            "instance runs at a time; starting it again shows the running one."))
        layout.addStretch(1)
        self.refresh()

    def command(self) -> list[str]:
        extra = ["--background"]
        if self.db_path and os.path.abspath(self.db_path) != os.path.abspath(str(default_db_path())):
            extra += ["--db", os.path.abspath(self.db_path)]
        return launch_command(extra)

    def refresh(self) -> None:
        self.login_check.blockSignals(True)
        self.login_check.setChecked(self.autostart.is_enabled())
        self.login_check.blockSignals(False)
        self.where.setText(f"Registered in {self.autostart.location}" if self.autostart.is_enabled() else "")

    def _toggle(self, on: bool) -> None:
        try:
            if on:
                self.autostart.enable(self.command())
            else:
                self.autostart.disable()
        except OSError as exc:
            QMessageBox.warning(self, "Start at login", f"Could not change it: {exc}")
        self.refresh()


class ConnectView(QTabWidget):
    """The Sync & Remote tab."""

    def __init__(self, service: MacroService, selected_ids: Callable[[], list[int]],
                 on_imported: Callable[[object], None], db_path: str,
                 autostart: Autostart | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.sync_page = SyncPage(service)
        self.teams_page = TeamsPage(service)
        self.sharing_page = SharingPage(service, selected_ids, on_imported)
        self.remote_page = RemotePage(service)
        self.background_page = BackgroundPage(db_path, autostart)
        self.addTab(self.sync_page, "Sync")
        self.addTab(self.teams_page, "Teams")
        self.addTab(self.sharing_page, "Sharing")
        self.addTab(self.remote_page, "Remote control")
        self.addTab(self.background_page, "Background")

    def refresh(self) -> None:
        for page in (self.sync_page, self.teams_page, self.remote_page, self.background_page):
            page.refresh()
