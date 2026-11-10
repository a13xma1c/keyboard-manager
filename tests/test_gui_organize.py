"""Folders, favorites, tags, speed and the Check tab in the main window."""

import pytest
from PySide6.QtWidgets import QInputDialog, QMessageBox

from keyboardmanager.core import actions as catalog
from keyboardmanager.core.models import Macro
from keyboardmanager.core.service import MacroService
from keyboardmanager.gui.macrotree import FAVORITES, ROLE_FOLDER, matches
from keyboardmanager.gui.mainwindow import MainWindow
from tests_support import FakeHotkeys


def A(t, **params):
    a = catalog.new_action(t)
    a.params.update(params)
    return a


@pytest.fixture
def win(qapp, db, backend):
    for m in (Macro("Sign off", [A("stop")], folder="Work/Email", tags=["email", "work"], favorite=True),
              Macro("Notes", [A("stop")], folder="Work", tags=["work"]),
              Macro("Zebra", [A("stop")]),
              Macro("Apple", [A("stop")], favorite=True)):
        db.save_macro(m)
    service = MacroService(db, backend, FakeHotkeys(), scheduler_thread=False)
    window = MainWindow(db, service)
    window.show()
    yield window
    window.editor.dirty = False
    window._quitting = True
    window.close()
    service.shutdown()


def tree(win):
    """The tree as nested text: [label, [children]] for folders, label for macros."""
    def node(item):
        if item.data(0, ROLE_FOLDER) is not None:
            return [item.text(0), [node(item.child(i)) for i in range(item.childCount())]]
        return item.text(0)
    return [node(win.list.topLevelItem(i)) for i in range(win.list.topLevelItemCount())]


def macro_ids(win, *names):
    return [m.id for m in win._macros.values() if m.name in names]


# --------------------------------------------------------------------- tree
def test_folders_nest_and_favorites_come_first(win):
    assert tree(win) == [
        ["Work  (2)", [["Email  (1)", ["\u2605 Sign off"]], "Notes"]],
        "\u2605 Apple", "Zebra",
    ]
    tip = win.list.item_for(macro_ids(win, "Sign off")[0]).toolTip(0)
    assert "Folder: Work/Email" in tip and "Tags: #email, #work" in tip


def test_filters_and_tag_search(win):
    win.filter_combo.setCurrentIndex(win.filter_combo.findData(FAVORITES))
    assert [i.text(0) for i in win.list.macro_items()] == ["\u2605 Sign off", "\u2605 Apple"]
    assert [win.filter_combo.itemText(i) for i in range(win.filter_combo.count())] == [
        "All macros", "\u2605 Favorites", "#email", "#work"]
    win.filter_combo.setCurrentIndex(win.filter_combo.findData("work"))
    assert sorted(i.text(0) for i in win.list.macro_items()) == ["Notes", "\u2605 Sign off"]
    win.service.folders.add("Archive")  # an empty folder
    win.filter_combo.setCurrentIndex(0)
    win._reload_macros()
    assert ["Archive", []] in tree(win)
    win.search.setText("#ema")
    assert tree(win) == [["Work  (1)", [["Email  (1)", ["\u2605 Sign off"]]]]]  # only folders with results


def test_matches_rules():
    m = Macro("Sign off", hotkey="ctrl+s", abbreviation=";sig", tags=["email"], favorite=False)
    assert matches(m, "sign", "") and matches(m, ";sig", "") and matches(m, "#em", "") and matches(m, "email", "")
    assert not matches(m, "#work", "") and not matches(m, "", FAVORITES) and not matches(m, "", "work")
    assert matches(m, "sign #email", "email")


def test_collapsed_folders_stay_collapsed(win):
    work = next(i for i in win.list.folder_items() if i.data(0, ROLE_FOLDER) == "Work")
    work.setExpanded(False)
    win._reload_macros()
    work = next(i for i in win.list.folder_items() if i.data(0, ROLE_FOLDER) == "Work")
    assert not work.isExpanded()


def test_clicking_a_folder_keeps_the_macro_in_the_editor(win):
    win._select(macro_ids(win, "Zebra")[0])
    win.list.setCurrentItem(win.list.folder_items()[0])
    assert win.editor.name_edit.text() == "Zebra"


# ------------------------------------------------------------------ folders
def answer_text(monkeypatch, text, ok=True):
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: (text, ok)))


def test_new_folders(win, db, monkeypatch):
    answer_text(monkeypatch, "Archive")
    win.list.clearSelection()
    win.list.setCurrentItem(None)
    win._new_folder("")
    assert "Archive" in win._all_folders() and ["Archive", []] in tree(win)
    answer_text(monkeypatch, "2024/Q1")  # nested in one go, inside "Work"
    win._new_folder("Work")
    assert {"Work/2024", "Work/2024/Q1"} <= set(win._all_folders())
    answer_text(monkeypatch, " / ")
    win._new_folder("")  # refused with a message
    answer_text(monkeypatch, "ignored", ok=False)
    win._new_folder("")


def test_new_macro_goes_into_the_selected_folder(win):
    email = next(i for i in win.list.folder_items() if i.data(0, ROLE_FOLDER) == "Work/Email")
    win.list.setCurrentItem(email)
    win._new_macro()
    assert win._macros[win._current_id].folder == "Work/Email"


def test_rename_folder_moves_its_macros_and_subfolders(win, db, monkeypatch):
    win.service.folders.add("Work/Email/Old")
    answer_text(monkeypatch, "Job")
    win._rename_folder("Work")
    assert {m.name: m.folder for m in db.list_macros()} == {
        "Sign off": "Job/Email", "Notes": "Job", "Zebra": "", "Apple": ""}
    assert "Job/Email/Old" in win._all_folders() and "Work" not in win._all_folders()


def test_delete_folder_moves_macros_up_and_deletes_nothing(win, db, monkeypatch):
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.StandardButton.Yes)
    win._delete_folder("Work")
    assert 'Delete the folder "Work"? Its 2 macro(s) move to the top level.' == asked[0]
    assert {m.name: m.folder for m in db.list_macros()} == {"Sign off": "Email", "Notes": "", "Zebra": "", "Apple": ""}
    assert len(db.list_macros()) == 4
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    win._delete_folder("Email")
    assert db.get_macro(macro_ids(win, "Sign off")[0]).folder == "Email"


def test_drag_and_drop_and_move_to_folder(win, db, monkeypatch):
    ids = macro_ids(win, "Zebra", "Apple")
    win.list.moved.emit(ids, "Work")  # what dropping them on "Work" does
    assert {db.get_macro(i).folder for i in ids} == {"Work"}
    monkeypatch.setattr(QInputDialog, "getItem", staticmethod(lambda *a, **k: ("(top level)", True)))
    win._ask_move(ids)
    assert {db.get_macro(i).folder for i in ids} == {""}
    monkeypatch.setattr(QInputDialog, "getItem", staticmethod(lambda *a, **k: ("Brand/New", True)))
    win._ask_move(ids[:1])
    assert db.get_macro(ids[0]).folder == "Brand/New" and "Brand/New" in win._all_folders()


def test_drop_target_is_the_folder_under_the_cursor(win, qapp):
    from PySide6.QtCore import QMimeData, QPointF, Qt
    from PySide6.QtGui import QDropEvent

    moved = []
    win.list.moved.connect(lambda ids, folder: moved.append((ids, folder)))
    zebra = win.list.item_for(macro_ids(win, "Zebra")[0])
    win.list.clearSelection()
    zebra.setSelected(True)
    email = next(i for i in win.list.folder_items() if i.data(0, ROLE_FOLDER) == "Work/Email")
    point = QPointF(win.list.visualItemRect(email).center())
    event = QDropEvent(point, Qt.DropAction.MoveAction, QMimeData(), Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier)
    win.list.dropEvent(event)
    assert moved == [(macro_ids(win, "Zebra"), "Work/Email")]


# ---------------------------------------------------------------- favorites
def test_favorites_from_the_context_menu_and_the_tray(win, db):
    zebra = macro_ids(win, "Zebra")
    win._set_favorite(zebra, True)
    win._set_favorite(macro_ids(win, "Apple"), False)
    assert db.get_macro(zebra[0]).favorite
    assert tree(win)[1:] == ["\u2605 Zebra", "Apple"]  # a favorite comes before "Apple"


def test_tray_favorites_menu(qapp, db, backend, monkeypatch):
    from PySide6.QtWidgets import QSystemTrayIcon

    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    db.save_macro(Macro("Fav", [A("stop")], favorite=True))
    db.save_macro(Macro("Plain", [A("stop")]))
    service = MacroService(db, backend, FakeHotkeys(), scheduler_thread=False)
    window = MainWindow(db, service)
    started = []
    monkeypatch.setattr(window, "_start", lambda macro, trigger: started.append((macro.name, trigger)))
    actions = window.favorites_menu.actions()
    assert [a.text() for a in actions] == ["Fav"]
    actions[0].trigger()
    assert started == [("Fav", "favorite")]
    window._quitting = True
    window.close()
    service.shutdown()


# ------------------------------------------------------------ editor fields
def test_editor_folder_tags_favorite_and_speed(win, db):
    win._select(macro_ids(win, "Zebra")[0])
    ed = win.editor
    assert [ed.folder_combo.itemText(i) for i in range(ed.folder_combo.count())] == ["", "Work", "Work/Email"]
    ed.folder_combo.setCurrentText(" Fresh / Folder ")
    ed.tags_edit.setText("#One, two")
    ed.favorite_check.setChecked(True)
    ed.speed_spin.setValue(1.5)
    assert ed.dirty and win.save_current()
    saved = db.get_macro(macro_ids(win, "Zebra")[0])
    assert (saved.folder, saved.tags, saved.favorite, saved.speed) == ("Fresh/Folder", ["one", "two"], True, 1.5)
    ed.tags_edit.setText("x" * 40)
    assert not win.save_current()


def test_toolbar_speed(win):
    win.speed_box.setValue(3.0)
    assert win.service.playback_speed == 3.0


# -------------------------------------------------------------- check tab
@pytest.fixture
def checked(qapp, db, backend):
    db.save_macro(Macro("Broken", [A("type_text", text="ok"), A("run_macro", name="Ghost")]))
    db.save_macro(Macro("Chatty", [A("type_text", text="{nobody}")], hotkey="ctrl+alt+c"))
    db.save_macro(Macro("Fine", [A("delay", ms=2000)]))
    service = MacroService(db, backend, FakeHotkeys(), scheduler_thread=False)
    window = MainWindow(db, service)
    window._run_checks()
    yield window
    window.editor.dirty = False
    window._quitting = True
    window.close()
    service.shutdown()


def test_check_tab_lists_problems(checked):
    view = checked.check_view
    rows = [(view.table.item(r, 0).text(), view.table.item(r, 1).text(), view.table.item(r, 3).text())
            for r in range(view.table.rowCount())]
    assert rows == [("error", "Broken", 'there is no macro named "Ghost"'),
                    ("warning", "Chatty", "{nobody} is never set, so it is typed as it is")]
    assert view.summary.text() == "1 problem(s) and 1 warning(s) in 2 macro(s)"
    view.show_info.setChecked(True)
    assert any(view.table.item(r, 0).text() == "info" for r in range(view.table.rowCount()))


def test_double_click_opens_the_macro_at_the_step(checked):
    view = checked.check_view
    view._open(view.table.item(0, 3))
    assert checked.tabs.currentWidget() is checked.editor
    assert checked.editor.name_edit.text() == "Broken"
    assert [a.params.get("name") for a in checked.editor._selected_actions()] == ["Ghost"]


def test_editor_shows_the_issues_of_what_is_being_edited(checked, qapp):
    broken = next(m for m in checked._macros.values() if m.name == "Broken")
    checked._select(broken.id)
    checked._run_checks()
    assert not checked.editor.issues_btn.isHidden() and "1 problem" in checked.editor.issues_btn.text()
    checked.editor.actions.pop()  # remove the bad step (unsaved)
    checked._run_checks()
    assert checked.editor.issues_btn.isHidden()


def test_running_a_macro_with_broken_steps_is_refused(checked, no_modal_boxes):
    broken = next(m for m in checked._macros.values() if m.name == "Broken")
    checked._start(broken, "manual")
    kind, args = no_modal_boxes[-1]
    assert kind == "warning" and "Ghost" in args[2] and checked.tabs.currentWidget() is checked.check_view
    assert checked.service.running_macro is None


def test_trigger_problems_do_not_stop_a_manual_run(checked, db):
    db.save_macro(Macro("Clash", [A("stop")], hotkey="ctrl+alt+c"))  # same hotkey as "Chatty"
    checked._reload_macros()
    checked.TEST_DELAY_S = 0
    clash = next(m for m in checked._macros.values() if m.name == "Clash")
    checked._start(clash, "manual")
    from tests_support import wait_until

    assert wait_until(lambda: checked.db.list_history() != [])
