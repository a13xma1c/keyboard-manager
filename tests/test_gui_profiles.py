"""GUI: the Profiles tab, the toolbar profile switch, layers and context rules."""

import pytest
from PySide6.QtWidgets import QDialog, QInputDialog

from keyboardmanager.core import actions as catalog
from keyboardmanager.core.context import ContextRule
from keyboardmanager.core.models import ActiveWindow, Macro
from keyboardmanager.core.service import MacroService
from keyboardmanager.gui.macrotree import ROLE_ID
from keyboardmanager.gui.mainwindow import MainWindow
from keyboardmanager.gui.profiles import ContextRuleDialog, ProfilesView
from tests_support import FakeHotkeys


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


# ------------------------------------------------------------------- toolbar
def test_toolbar_profile_combo(win, db, qapp):
    db.save_macro(typing("w", profiles=["Work"]))
    win.service.profiles.add("Gaming")
    win._reload_macros()
    combo = win.profile_combo
    labels = [combo.itemText(i) for i in range(combo.count())]
    assert labels == ["All profiles", "Profile: Gaming", "Profile: Work"]
    assert combo.currentIndex() == 0
    combo.setCurrentIndex(2)
    combo.activated.emit(2)
    assert win.service.active_profile == "Work"
    qapp.processEvents()  # the "profile" event refreshes the views
    assert win.profile_combo.currentText() == "Profile: Work"
    assert win.profiles_view.profile_list.currentItem().text().startswith("Work")


def test_profile_events_update_the_window(win, db, backend, qapp):
    m = db.save_macro(typing("w", profiles=["Work"], layer="nav", hotkey="ctrl+1"))
    win._reload_macros()
    win.service.set_active_profile("Home")
    win.service.set_layer("nav")
    qapp.processEvents()
    assert win.profile_combo.currentText() == "Profile: Home"
    assert "layer: nav" in win.status_label.text()
    [item] = [i for i in win.list.macro_items() if i.data(0, ROLE_ID) == m.id]
    assert "(other profile)" in item.text(0) and "Profiles: Work" in item.toolTip(0)
    win.service.set_active_profile("")
    win.service.set_layer("")
    qapp.processEvents()
    assert "layer:" not in win.status_label.text()
    [item] = [i for i in win.list.macro_items() if i.data(0, ROLE_ID) == m.id]
    assert "(other profile)" not in item.text(0)


def test_context_profile_not_in_the_list_still_shows(win, qapp):
    win.service.set_context_rules([ContextRule(app="game", profile="Gaming")])
    win.service.backend.window = ActiveWindow("Game", "")
    win.service.contexts.check()
    qapp.processEvents()
    assert win.profile_combo.currentText() == "Profile: Gaming"


# -------------------------------------------------------------------- editor
def test_editor_profiles_and_layer_fields(win, db):
    saved = db.save_macro(typing("m"))
    win._reload_macros()
    win._select(saved.id)
    ed = win.editor
    ed.profiles_edit.setText("Work,  work, Gaming")
    ed.layer_combo.setCurrentText("  nav  mode ")
    assert ed.dirty
    assert win.save_current()
    again = db.get_macro(saved.id)
    assert again.profiles == ["Work", "Gaming"] and again.layer == "nav mode"
    ed.load(again)
    assert ed.profiles_edit.text() == "Work, Gaming" and ed.layer_combo.currentText() == "nav mode"
    ed.set_layers(["nav mode", "other"])
    assert [ed.layer_combo.itemText(i) for i in range(ed.layer_combo.count())] == ["", "nav mode", "other"]
    ed.profiles_edit.setText("a,b")  # a comma inside one name cannot happen; "a,b" is two
    assert ed.build_macro().profiles == ["a", "b"]


# -------------------------------------------------------------- profiles tab
@pytest.fixture
def view(qapp, db, backend):
    service = MacroService(db, backend, FakeHotkeys(), scheduler_thread=False)
    v = ProfilesView(service)
    v.service_ = service
    yield v
    service.shutdown()


def test_profiles_tab_lists_and_activates(view, db):
    db.save_macro(typing("m", profiles=["Work"]))
    view.service.profiles.add("Gaming")
    view.refresh()
    texts = [view.profile_list.item(i).text() for i in range(view.profile_list.count())]
    assert texts == ["All profiles   \u25c0 active", "Gaming", "Work"]
    view.profile_list.setCurrentRow(2)
    view.activate_btn.click()
    assert view.service.active_profile == "Work"
    assert view.profile_list.item(2).text() == "Work   \u25c0 active"


def test_profiles_tab_add_rename_delete(view, db, monkeypatch, no_modal_boxes):
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Work", True)))
    view.add_profile_btn.click()
    assert view.service.all_profiles() == ["Work"]
    view.add_profile_btn.click()  # the same name again: explained, not added
    assert no_modal_boxes[-1][0] == "warning" and view.service.all_profiles() == ["Work"]
    m = db.save_macro(typing("m", profiles=["Work"]))
    view.profile_list.setCurrentRow(1)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Office", True)))
    view.rename_btn.click()
    assert view.service.all_profiles() == ["Office"] and db.get_macro(m.id).profiles == ["Office"]
    view.profile_list.setCurrentRow(1)
    view.delete_profile_btn.click()  # question boxes answer Yes
    assert view.service.all_profiles() == [] and db.get_macro(m.id).profiles == []


def test_profiles_tab_layer_switch(view, db):
    db.save_macro(typing("m", layer="nav", hotkey="ctrl+1"))
    view.refresh()
    assert [view.layer_combo.itemText(i) for i in range(view.layer_combo.count())] == ["", "nav"]
    view.layer_combo.setCurrentText("nav")
    view.switch_layer_btn.click()
    assert view.service.active_layer == "nav"
    view.refresh()
    assert view.layer_label.text() == "Active: nav"
    view.base_btn.click()
    assert view.service.active_layer == ""


# ------------------------------------------------------------- rule dialog
def test_context_rule_dialog_roundtrip(qapp):
    rule = ContextRule(app="chrome", title="docs", profile="Work", layer=None)
    dialog = ContextRuleDialog(rule, ["Work"], ["nav"])
    assert dialog.profile_check.isChecked() and not dialog.layer_check.isChecked()
    assert dialog.rule() == rule
    dialog.layer_check.setChecked(True)  # layer "" = base (not None)
    assert dialog.rule().layer == ""
    dialog.profile_check.setChecked(False)
    assert dialog.rule().profile is None
    dialog.enabled_check.setChecked(False)
    assert not dialog.rule().enabled


def test_context_rule_dialog_validates(qapp, no_modal_boxes):
    dialog = ContextRuleDialog(None, [], [])
    dialog._accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert no_modal_boxes[-1][0] == "warning" and "which windows" in no_modal_boxes[-1][1][2]
    dialog.app_edit.setText("chrome")
    dialog._accept()
    assert "profile and/or the layer" in no_modal_boxes[-1][1][2]
    dialog.layer_check.setChecked(True)
    dialog._accept()
    assert dialog.result() == QDialog.DialogCode.Accepted


@pytest.fixture
def rule_dialogs(monkeypatch):
    """ContextRuleDialog.exec fills the dialog with ``fill`` and accepts."""
    state = {"fill": None, "accept": True}

    def exec_(self):
        if state["fill"]:
            state["fill"](self)
        return QDialog.DialogCode.Accepted if state["accept"] else QDialog.DialogCode.Rejected

    monkeypatch.setattr(ContextRuleDialog, "exec", exec_)
    return state


def test_rules_table_add_edit_move_delete(view, rule_dialogs):
    def fill(d):
        d.app_edit.setText("code")
        d.profile_check.setChecked(True)
        d.profile_combo.setCurrentIndex(0)  # All profiles -> ""

    rule_dialogs["fill"] = fill
    view.add_rule_btn.click()
    rule_dialogs["fill"] = lambda d: (d.title_edit.setText("game"), d.layer_check.setChecked(True),
                                      d.layer_combo.setCurrentText("g"))
    view.add_rule_btn.click()
    rules = view.service.context_rules
    assert rules[0].app == "code" and rules[0].profile == "" and rules[0].layer is None
    assert rules[1].title == "game" and rules[1].layer == "g"
    assert view.rules_table.rowCount() == 2
    assert view.rules_table.item(0, 3).text() == "All profiles"
    assert view.rules_table.item(1, 4).text() == "g"
    view.rules_table.selectRow(1)
    view.up_rule_btn.click()
    assert view.service.context_rules[0].title == "game"
    assert not view.up_rule_btn.isEnabled() and view.down_rule_btn.isEnabled()
    rule_dialogs["fill"] = lambda d: d.title_edit.setText("lobby")
    view.rules_table.selectRow(0)
    view.edit_rule_btn.click()
    assert view.service.context_rules[0].title == "lobby"
    view.rules_table.selectRow(0)
    view.delete_rule_btn.click()
    assert len(view.service.context_rules) == 1
    rule_dialogs["accept"] = False
    view.add_rule_btn.click()
    assert len(view.service.context_rules) == 1  # cancelled


def test_watch_checkbox(view):
    assert view.watch_check.isChecked()
    view.watch_check.setChecked(False)
    assert not view.service.contexts_enabled
    view.watch_check.setChecked(True)
    assert view.service.contexts_enabled


def test_preflight_still_clean_with_profiles(win, db, qapp):
    db.save_macro(typing("m", profiles=["Work"], layer="nav", hotkey="ctrl+1"))
    win._reload_macros()
    win.check_view.refresh()
    assert not any(i.severity == "error" for i in win.check_view.issues)
