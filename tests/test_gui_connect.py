"""GUI: the Sync & Remote tab, team macros in the editor and list, share codes."""

import socket

import pytest
from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMessageBox

from keyboardmanager.core import actions as catalog
from keyboardmanager.core import team as team_mod
from keyboardmanager.core.autostart import Autostart
from keyboardmanager.core.database import Database
from keyboardmanager.core.models import Macro
from keyboardmanager.core.remote import RemoteClient
from keyboardmanager.core.service import MacroService
from keyboardmanager.core.sync import sync_folder
from keyboardmanager.core.team import TeamLibrary
from keyboardmanager.gui.mainwindow import MainWindow
from tests_support import FakeHotkeys, wait_until


def A(t, **params):
    a = catalog.new_action(t)
    a.params.update(params)
    return a


def typing(name, **kw):
    return Macro(name, [A("type_text", text=name)], **kw)


@pytest.fixture
def win(qapp, db, backend):
    service = MacroService(db, backend, FakeHotkeys(), scheduler_thread=False)
    window = MainWindow(db, service)
    qapp.processEvents()
    yield window
    window.editor.dirty = False
    window._quitting = True
    window.close()
    service.shutdown()
    window.deleteLater()
    qapp.processEvents()


def pump(qapp, predicate, timeout=3.0):
    return wait_until(lambda: (qapp.processEvents(), predicate())[1], timeout)


def folder_answer(monkeypatch, path):
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(path)))


# ---------------------------------------------------------------------- sync
def test_sync_page_apply_and_sync_now(win, db, qapp, tmp_path, no_modal_boxes):
    page = win.connect_view.sync_page
    assert not page.sync_btn.isEnabled() and page.last_label.text() == "Not synced yet."
    page.folder_edit.setText(str(tmp_path / "missing"))
    assert not page.apply()
    assert no_modal_boxes[-1][0] == "warning" and "does not exist" in no_modal_boxes[-1][1][2]
    shared = tmp_path / "Dropbox"
    shared.mkdir()
    db.save_macro(typing("mine"))
    page.folder_edit.setText(str(shared))
    page.device_edit.setText("laptop")
    page.interval_spin.setValue(0)
    assert page.apply()
    assert win.service.sync_folder == str(shared) and win.service.device_name == "laptop"
    assert win.service.sync_interval == 0 and page.sync_btn.isEnabled()
    page.sync_btn.click()
    assert "Sent: mine" in page.report.toPlainText()
    assert page.last_label.text().startswith("Last sync ")
    assert page.devices.rowCount() == 1 and page.devices.item(0, 0).text() == "laptop"


def test_sync_from_another_computer_updates_the_list(win, db, qapp, tmp_path):
    shared = tmp_path / "Shared"
    shared.mkdir()
    other = Database(":memory:")
    other.save_macro(typing("from the desktop"))
    sync_folder(other, shared, "desktop")
    win.service.set_sync_options(str(shared), "laptop")
    win.service.sync_now()
    assert pump(qapp, lambda: any("from the desktop" in i.text(0) for i in win.list.macro_items()))
    assert "Sync: 1 received" in win.statusBar().currentMessage()
    other.close()


def test_failed_sync_shows_in_the_status_bar(win, qapp, tmp_path):
    win.service.db.set_setting("sync_folder", str(tmp_path / "unplugged"))
    win.service.sync_now()
    assert pump(qapp, lambda: "Sync failed" in win.statusBar().currentMessage())
    win.connect_view.sync_page.refresh()
    assert win.connect_view.sync_page.last_label.property("tone") == "danger"


# --------------------------------------------------------------------- teams
@pytest.fixture
def library(tmp_path):
    folder = tmp_path / "Team"
    folder.mkdir()
    TeamLibrary.create(folder, "Support")
    author = Database(":memory:")
    sub = team_mod.new_subscription(str(folder))
    macro = author.save_macro(typing("Reply", hotkey="ctrl+r"))
    team_mod.publish(author, sub, macro, "Alice")
    yield folder, author, sub, macro
    author.close()


def test_join_library_and_read_only_team_macros(win, qapp, library, monkeypatch, no_modal_boxes):
    folder, _, _, published = library
    page = win.connect_view.teams_page
    folder_answer(monkeypatch, folder)
    page.join_btn.click()
    assert page.result_label.text() == "Joined: 1 new"
    assert page.table.rowCount() == 1 and page.table.item(0, 0).text() == "Support"
    assert page.table.item(0, 2).text() == "1"
    qapp.processEvents()
    copy = next(m for m in win._macros.values() if m.uid == published.uid)
    win._select(copy.id)
    ed = win.editor
    assert ed.read_only and ed.team_banner.isVisibleTo(win) and "Support" in ed.team_label.text()
    assert not ed.save_btn.isEnabled() and not ed._steps_box.isEnabled() and ed.run_btn.isEnabled()
    assert not win.save_current() and no_modal_boxes[-1][0] == "information"
    ed.detach_btn.click()  # Edit locally
    qapp.processEvents()
    assert not win.editor.read_only and win.db.get_macro(copy.id).source == ""


def test_joining_twice_or_a_non_library_is_explained(win, library, monkeypatch, tmp_path, no_modal_boxes):
    folder = library[0]
    page = win.connect_view.teams_page
    folder_answer(monkeypatch, folder)
    page.join_btn.click()
    page.join_btn.click()
    assert "already use" in no_modal_boxes[-1][1][2]
    folder_answer(monkeypatch, tmp_path)
    page.join_btn.click()
    assert "not a team library" in no_modal_boxes[-1][1][2]


def test_create_leave_and_update(win, qapp, tmp_path, monkeypatch):
    folder = tmp_path / "New"
    folder.mkdir()
    page = win.connect_view.teams_page
    folder_answer(monkeypatch, folder)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Ops", True)))
    page.create_btn.click()
    assert [t.name for t in win.service.teams] == ["Ops"] and "Created" in page.result_label.text()
    page.pull_btn.click()
    assert 'Team "Ops": up to date' in page.result_label.text()
    page.table.selectRow(0)
    page.leave_btn.click()
    assert win.service.teams == [] and "Left Ops" in page.result_label.text()


def test_publish_from_the_list_and_conflicts(win, qapp, library, monkeypatch):
    folder = library[0]
    win.service.set_author("Bob")
    win.service.subscribe_team(str(folder))
    mine = win.db.save_macro(typing("Bob's macro"))
    win._reload_macros()
    win._select(mine.id)
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a[2]))
    team_id = win.service.teams[0].id
    win._publish([mine.id], team_id)
    assert shown[-1] == '"Bob\'s macro": published (version 1)'
    # someone else publishes a newer version meanwhile
    lib = TeamLibrary(folder)
    lib.write(mine, 7, "Carol")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    win._publish([mine.id], team_id)
    assert lib.entry(mine.uid)["version"] == 7  # not replaced
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    win._publish([mine.id], team_id)
    assert lib.entry(mine.uid)["version"] == 8 and lib.entry(mine.uid)["author"] == "Bob"


def test_remove_a_published_macro_from_the_team(win, qapp, library):
    folder = library[0]
    win.service.subscribe_team(str(folder))
    mine = win.db.save_macro(typing("Mine"))
    win._reload_macros()
    win._select(mine.id)
    team_id = win.service.teams[0].id
    win._list_menu(win.list.visualItemRect(win.list.currentItem()).center())
    texts = [a.text() for a in win._last_menu.actions()]
    assert 'Remove from team "Support"' not in texts  # not published yet
    win.service.publish([mine.id], team_id)
    win._list_menu(win.list.visualItemRect(win.list.currentItem()).center())
    remove = next(a for a in win._last_menu.actions() if a.text() == 'Remove from team "Support"')
    remove.trigger()  # confirmed (question boxes answer Yes)
    assert TeamLibrary(folder).entry(mine.uid) is None
    assert win.db.get_macro(mine.id) is not None


def test_list_menu_offers_sharing_and_team_actions(win, qapp, library):
    folder, _, _, published = library
    win.service.subscribe_team(str(folder))
    win._reload_macros()
    copy = next(m for m in win._macros.values() if m.uid == published.uid)
    win._select(copy.id)
    win._list_menu(win.list.visualItemRect(win.list.currentItem()).center())
    texts = [a.text() for a in win._last_menu.actions()]
    assert "Copy share code" in texts and "Edit locally (make it mine)" in texts
    publish = next(a for a in win._last_menu.actions() if a.text() == 'Publish to team "Support"')
    assert not publish.isEnabled()  # a team macro cannot be published as it is


# ------------------------------------------------------------------- sharing
def test_share_codes_through_the_tab(win, db, qapp, monkeypatch, no_modal_boxes):
    page = win.connect_view.sharing_page
    win._select(None)
    win.list.clearSelection()
    assert page.copy_code() == ""
    assert no_modal_boxes[-1][0] == "information"
    m = db.save_macro(typing("Signature", hotkey="ctrl+s"))
    win._reload_macros()
    win._select(m.id)
    code = page.copy_code()
    assert QApplication.clipboard().text() == code and code.startswith("KMSHARE1:")
    page.code_edit.setPlainText(code)
    page.import_btn.click()
    assert sorted(x.name for x in db.list_macros()) == ["Signature", "Signature (2)"]
    assert "Imported 1 macro(s)" in page.status.text()
    assert no_modal_boxes[-1][1][1] == "Import share code"
    assert "1 hotkey(s) removed" in no_modal_boxes[-1][1][2]
    page.import_btn.click()  # empty box: the clipboard's code is used
    assert len(db.list_macros()) == 3
    page.code_edit.setPlainText("not a code")
    page.import_btn.click()
    assert no_modal_boxes[-1][0] == "warning" and "share code" in no_modal_boxes[-1][1][2]


def test_copy_share_code_from_the_list(win, db, qapp):
    m = db.save_macro(typing("X"))
    win._reload_macros()
    win._select(m.id)
    win._copy_share_code()
    assert QApplication.clipboard().text().startswith("KMSHARE1:")


# ------------------------------------------------------------- remote page
def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_remote_page(win, qapp, monkeypatch):
    page = win.connect_view.remote_page
    assert page.status.text() == "Remote control is off."
    port = free_port()
    page.enable_check.setChecked(True)
    page.port_spin.setValue(port)
    page.apply_btn.click()
    qapp.processEvents()
    assert win.service.remote is not None and "Listening on port" in page.status.text()
    assert page.url_edit.text() == f"http://127.0.0.1:{port}/?token={page.token_edit.text()}"
    assert RemoteClient(port, page.token_edit.text()).call("GET", "/api/status")["running"] is None
    old = page.token_edit.text()
    page.new_token_btn.click()  # question boxes answer Yes
    assert page.token_edit.text() != old
    # allowing other devices asks first; "No" changes nothing
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    page.lan_check.setChecked(True)
    page.apply_btn.click()
    assert not win.service.remote_settings()["lan"]
    page.enable_check.setChecked(False)
    page.lan_check.setChecked(False)
    page.apply_btn.click()
    assert win.service.remote is None and page.status.text() == "Remote control is off."


# --------------------------------------------------------- background page
def test_start_at_login(win, tmp_path):
    page = win.connect_view.background_page
    page.autostart = Autostart(platform="linux", home=tmp_path, env={})
    page.db_path = str(tmp_path / "custom.db")
    page.refresh()
    assert not page.login_check.isChecked() and page.where.text() == ""
    page.login_check.setChecked(True)
    entry = tmp_path / ".config" / "autostart" / "keyboardmanager.desktop"
    assert entry.exists()
    command = page.autostart.command()
    assert command[-3:] == ["--background", "--db", str(tmp_path / "custom.db")]
    assert "Registered in" in page.where.text()
    page.login_check.setChecked(False)
    assert not entry.exists()


def test_start_at_login_without_db_argument_for_the_default(win, tmp_path, monkeypatch):
    from keyboardmanager.gui import connect

    monkeypatch.setattr(connect, "default_db_path", lambda: tmp_path / "km.db")
    page = win.connect_view.background_page
    page.db_path = str(tmp_path / "km.db")
    assert page.command()[-1] == "--background"


def test_start_at_login_failure_is_explained(win, tmp_path, no_modal_boxes):
    class Broken(Autostart):
        def enable(self, command):
            raise PermissionError("read-only home")

    page = win.connect_view.background_page
    page.autostart = Broken(platform="linux", home=tmp_path, env={})
    page.login_check.setChecked(True)
    assert no_modal_boxes[-1][0] == "warning" and "read-only home" in no_modal_boxes[-1][1][2]
    assert not page.login_check.isChecked()
