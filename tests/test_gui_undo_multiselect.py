"""Editor undo / redo, selecting several steps, copying steps, and several macros."""

import json

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from keyboardmanager.core import actions as catalog
from keyboardmanager.core.models import Macro
from keyboardmanager.core.service import MacroService
from keyboardmanager.gui.editor import CLIPBOARD_FORMAT, MacroEditor
from keyboardmanager.gui.mainwindow import MainWindow
from keyboardmanager.gui.widgets import ActionDialog
from tests_support import FakeHotkeys


def key(text):
    a = catalog.new_action("key_press")
    a.params["keys"] = text
    return a


@pytest.fixture
def ed(qapp):
    e = MacroEditor()
    e.load(Macro("m", [key("a"), key("b"), key("c"), key("d")]))
    e.show()
    e.activateWindow()  # keyboard shortcuts only work in the active window
    QTest.qWaitForWindowActive(e)
    return e


def names(ed):
    def walk(actions):
        return [a.params.get("keys", a.type) if not a.children else [a.type, walk(a.children)] for a in actions]
    return walk(ed.actions)


def select(ed, *indices):
    ed.tree.clearSelection()
    for i in indices:
        ed.tree.topLevelItem(i).setSelected(True)


# --------------------------------------------------------------- undo / redo
def test_every_step_operation_can_be_undone_and_redone(ed):
    history = [names(ed)]
    select(ed, 1)
    ed._delete()
    history.append(names(ed))
    select(ed, 0)
    ed._duplicate()
    history.append(names(ed))
    select(ed, 2)  # "c" (moving an "a" past its identical copy would change nothing)
    ed._move(-1)
    history.append(names(ed))
    ed.tree.clearSelection()
    ed.add_step("delay", edit=False)
    history.append(names(ed))
    assert history[0] == ["a", "b", "c", "d"] and history[1] == ["a", "c", "d"]
    assert history[3] == ["a", "c", "a", "d"]
    assert ed.undo_action.text() == "Undo Add Delay"
    for expected in reversed(history[:-1]):  # undo everything, one step at a time
        ed.undo_action.trigger()
        assert names(ed) == expected
    assert not ed.undo_stack.canUndo()
    for expected in history[1:]:  # and redo it all
        ed.redo_action.trigger()
        assert names(ed) == expected
    assert ed.dirty


def test_undo_of_an_edit_and_of_a_cancelled_edit(ed, monkeypatch):
    select(ed, 0)

    def change(dialog):
        dialog._action.params["keys"] = "ctrl+z"
        return ActionDialog.DialogCode.Accepted

    monkeypatch.setattr(ActionDialog, "exec", change)
    ed._edit_selected()
    assert names(ed)[0] == "ctrl+z" and ed.undo_action.text() == "Undo Edit Press keys"
    ed.undo_action.trigger()
    assert names(ed)[0] == "a"
    monkeypatch.setattr(ActionDialog, "exec", lambda d: ActionDialog.DialogCode.Rejected)
    count = ed.undo_stack.count()
    select(ed, 0)
    ed._edit_selected()
    assert ed.undo_stack.count() == count  # a cancelled edit records nothing


def test_undo_restores_the_selection(ed):
    select(ed, 1, 2)
    ed._delete()
    ed.undo_action.trigger()
    assert [i.text(0) for i in ed.tree.selectedItems()] == ["Press b", "Press c"]


def test_undo_of_a_drag_and_drop(ed):
    ed._before_drop()  # what StepTree emits just before Qt moves the items
    item = ed.tree.takeTopLevelItem(0)
    ed.tree.insertTopLevelItem(3, item)
    ed._after_drop()
    assert names(ed) == ["b", "c", "d", "a"]
    ed.undo_action.trigger()
    assert names(ed) == ["a", "b", "c", "d"]


def test_loading_another_macro_clears_the_history_but_saving_keeps_it(ed):
    select(ed, 0)
    ed._delete()
    ed.load(ed.build_macro(), keep_history=True)  # what saving does
    assert ed.undo_stack.canUndo() and not ed.dirty
    ed.undo_action.trigger()
    assert names(ed) == ["a", "b", "c", "d"] and ed.dirty  # differs from what was saved
    ed.load(Macro("other"))
    assert not ed.undo_stack.canUndo()


def test_ctrl_z_undoes_steps_but_text_fields_keep_their_own(ed):
    select(ed, 0)
    ed._delete()
    ed.tree.setFocus()
    QTest.keyClick(ed.tree, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert names(ed) == ["a", "b", "c", "d"]
    ed.name_edit.setFocus()
    ed.name_edit.setText("")
    QTest.keyClicks(ed.name_edit, "xy")
    QTest.keyClick(ed.name_edit, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert ed.name_edit.text() != "xy"  # the line edit undid its typing...
    assert names(ed) == ["a", "b", "c", "d"]  # ...and the steps were left alone


# ---------------------------------------------------------- several steps
def test_delete_duplicate_and_move_several_steps(ed):
    select(ed, 0, 2)
    assert not ed.edit_btn.isEnabled() and ed.del_btn.isEnabled() and ed.up_btn.isEnabled()
    ed._duplicate()
    assert names(ed) == ["a", "a", "b", "c", "c", "d"]
    assert [i.text(0) for i in ed.tree.selectedItems()] == ["Press a", "Press c"]  # the copies
    select(ed, 2, 3)  # b, c
    ed._move(-1)
    assert names(ed) == ["a", "b", "c", "a", "c", "d"]
    ed._move(-1)
    ed._move(-1)  # at the top: the block keeps its shape
    assert names(ed) == ["b", "c", "a", "a", "c", "d"]
    select(ed, 0, 5)
    ed._delete()
    assert names(ed) == ["c", "a", "a", "c"]


def test_steps_in_different_lists_cannot_move_together(ed):
    rep = catalog.new_action("repeat")
    rep.children = [key("x")]
    ed.load(Macro("m", [key("a"), rep]))
    ed.tree.clearSelection()
    ed.tree.topLevelItem(0).setSelected(True)
    ed.tree.topLevelItem(1).child(0).setSelected(True)
    assert not ed.up_btn.isEnabled() and ed.del_btn.isEnabled()
    ed._move(1)
    assert ed.actions[0].params["keys"] == "a"
    ed._delete()  # deleting works across lists
    assert names(ed) == [["repeat", []]] or names(ed) == ["repeat"]


def test_a_selected_container_includes_its_children(ed):
    rep = catalog.new_action("repeat")
    rep.children = [key("x"), key("y")]
    ed.load(Macro("m", [rep, key("z")]))
    ed.tree.clearSelection()
    ed.tree.topLevelItem(0).setSelected(True)
    ed.tree.topLevelItem(0).child(0).setSelected(True)  # also inside the selected repeat
    assert ed._selected_actions() == [ed.actions[0]]
    ed._duplicate()
    assert len(ed.actions) == 3 and ed.actions[1].children[1].params["keys"] == "y"


# --------------------------------------------------------------- clipboard
def test_copy_and_paste_steps_between_macros(ed, qapp):
    select(ed, 1, 3)
    ed.copy_steps()
    data = json.loads(QApplication.clipboard().text())
    assert data["format"] == CLIPBOARD_FORMAT and [s["params"]["keys"] for s in data["steps"]] == ["b", "d"]
    other = MacroEditor()
    other.load(Macro("other", [key("x")]))
    other.tree.topLevelItem(0).setSelected(True)
    pasted = other.paste_steps()
    assert names(other) == ["x", "b", "d"] and other._selected_actions() == pasted
    other.undo_action.trigger()
    assert names(other) == ["x"]


def test_cut_and_paste_moves_steps(ed):
    select(ed, 0)
    ed.cut_steps()
    assert names(ed) == ["b", "c", "d"]
    select(ed, 2)
    ed.paste_steps()
    assert names(ed) == ["b", "c", "d", "a"]


def test_paste_ignores_other_clipboard_content(ed):
    for text in ("just some text", '{"format": "something else"}', "[1, 2]", '{"format": "%s", "steps": "x"}' % CLIPBOARD_FORMAT):
        QApplication.clipboard().setText(text)
        assert ed.paste_steps() == [] and names(ed) == ["a", "b", "c", "d"]


def test_pasting_with_ctrl_held_selects_only_the_pasted_steps(ed):
    """The user holds Ctrl for Ctrl+V; Ctrl must not turn selecting into toggling."""
    select(ed, 0)
    ed.copy_steps()
    QTest.keyPress(ed.tree, Qt.Key.Key_Control, Qt.KeyboardModifier.ControlModifier)
    pasted = ed.paste_steps()
    QTest.keyRelease(ed.tree, Qt.Key.Key_Control)
    assert ed._selected_actions() == pasted


def test_keyboard_shortcuts_on_the_step_list(ed, qapp):
    ed.tree.setFocus()
    select(ed, 0)
    QTest.keyClick(ed.tree, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    select(ed, 3)
    QTest.keyClick(ed.tree, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
    assert names(ed) == ["a", "b", "c", "d", "a"]
    select(ed, 1)
    QTest.keyClick(ed.tree, Qt.Key.Key_Delete)
    assert names(ed) == ["a", "c", "d", "a"]


# ------------------------------------------------------- several macros
@pytest.fixture
def win(qapp, db, backend):
    service = MacroService(db, backend, FakeHotkeys(), scheduler_thread=False)
    for name in ("Alpha", "Beta", "Gamma", "Delta"):
        db.save_macro(Macro(name, hotkey=f"ctrl+alt+{name[0].lower()}"))
    window = MainWindow(db, service)
    window.show()
    yield window
    window.editor.dirty = False
    window._quitting = True
    window.close()
    service.shutdown()


def select_macros(win, *names_):
    win.list.clearSelection()
    for item in win.list.macro_items():
        if item.text(0).split("  ")[0].lstrip("\u2605 ") in names_:
            item.setSelected(True)


def test_delete_several_macros(win, db, monkeypatch):
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.StandardButton.Yes)
    select_macros(win, "Beta", "Delta")
    win._delete_macro()
    assert asked and "Delete 2 macros (Beta, Delta)?" in asked[0]
    assert sorted(m.name for m in db.list_macros()) == ["Alpha", "Gamma"]
    assert set(win.service.hotkeys.mapping) >= {"ctrl+alt+a", "ctrl+alt+g"}
    assert "ctrl+alt+b" not in win.service.hotkeys.mapping


def test_enable_and_disable_several_macros(win, db):
    select_macros(win, "Alpha", "Gamma")
    win._set_enabled(False)
    assert {m.name: m.enabled for m in db.list_macros()} == {
        "Alpha": False, "Beta": True, "Delta": True, "Gamma": False}
    assert "ctrl+alt+a" not in win.service.hotkeys.mapping
    select_macros(win, "Alpha", "Gamma")
    win._set_enabled(True)
    assert all(m.enabled for m in db.list_macros())


def test_export_several_macros(win, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    out = tmp_path / "two.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    select_macros(win, "Alpha", "Delta")
    win._export_selected()
    assert sorted(m["name"] for m in json.loads(out.read_text())["macros"]) == ["Alpha", "Delta"]


def test_context_menu_offers_batch_actions(win):
    select_macros(win, "Alpha", "Beta", "Gamma")
    win._list_menu(win.list.rect().center())
    labels = [a.text() for a in win._last_menu.actions() if a.text()]
    assert labels == ["Add to favorites", "Move to folder...", "Run", "Duplicate", "Enable", "Disable",
                      "Export 3 macros...", "Copy share code", "Delete 3 macros"]
    run = next(a for a in win._last_menu.actions() if a.text() == "Run")
    assert not run.isEnabled()  # Run needs exactly one
    win._last_menu.close()


def test_programmatic_navigation_selects_just_one_macro(win, db):
    select_macros(win, "Alpha", "Beta")
    win._new_macro()  # jumps to the new macro: the earlier selection is dropped
    assert win._selected_ids() == [win._current_id]


def test_a_change_that_changes_nothing_is_not_recorded(ed, monkeypatch):
    select(ed, 0)
    monkeypatch.setattr(ActionDialog, "exec", lambda d: ActionDialog.DialogCode.Accepted)  # OK, no edits
    ed.dirty = False
    ed._edit_selected()
    assert ed.undo_stack.count() == 0 and not ed.dirty


def test_undo_buttons_keep_short_labels_and_explain_in_tooltips(ed):
    assert not ed.undo_btn.isEnabled() and not ed.redo_btn.isEnabled()
    select(ed, 0)
    ed._delete()
    assert ed.undo_btn.isEnabled() and ed.undo_btn.text() == "Undo"
    assert ed.undo_btn.toolTip() == "Undo Delete step (Ctrl+Z)"
    ed.undo_btn.click()
    assert names(ed)[0] == "a" and ed.redo_btn.isEnabled() and ed.redo_btn.toolTip() == "Redo Delete step"
    ed.redo_btn.click()
    assert names(ed)[0] == "b"
