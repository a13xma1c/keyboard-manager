import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from keyboardmanager.core import actions as catalog  # noqa: E402
from keyboardmanager.core.models import Macro  # noqa: E402
from keyboardmanager.core.service import MacroService  # noqa: E402
from keyboardmanager.gui.editor import MacroEditor, ROLE_ACTION, ROLE_KIND  # noqa: E402
from keyboardmanager.gui.mainwindow import MainWindow  # noqa: E402
from keyboardmanager.gui.widgets import ActionDialog, KeyCaptureEdit, qt_key_name  # noqa: E402


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def no_modal_boxes(monkeypatch):
    shown = []
    for name in ("warning", "information", "critical", "question"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, _n=name, **k: shown.append((_n, a)) or QMessageBox.StandardButton.Yes)
    return shown


def test_qt_key_names():
    assert qt_key_name(Qt.Key.Key_F5) == "f5"
    assert qt_key_name(Qt.Key.Key_A) == "a"
    assert qt_key_name(Qt.Key.Key_Return) == "enter"
    assert qt_key_name(Qt.Key.Key_PageUp) == "page_up"
    assert qt_key_name(Qt.Key.Key_Shift) == "shift"


def test_key_capture_normalizes(qapp):
    w = KeyCaptureEdit("hotkey")
    w.setText("Alt+Ctrl+K")
    assert w.normalized() == "ctrl+alt+k"
    w.setText("ctrl+alt")
    with pytest.raises(ValueError):
        w.normalized()


def test_action_dialog_builds_form_for_every_action(qapp):
    for t in catalog.SPECS:
        action = catalog.new_action(t)
        dlg = ActionDialog(action)
        assert set(dlg.values()) == {f.name for f in catalog.spec_for(t).fields}


def test_action_dialog_conditional_fields_and_accept(qapp):
    action = catalog.new_action("if")
    dlg = ActionDialog(action)
    dlg.show()
    assert not dlg._widgets["variable"].isVisible() and dlg._widgets["value"].isVisible()
    dlg._widgets["kind"].setCurrentText("variable")
    assert dlg._widgets["variable"].isVisible() and not dlg._widgets["value"].isVisible()
    dlg._widgets["variable"].setText("n")
    dlg._widgets["compare_to"].setText("3")
    dlg._accept()
    assert action.params["variable"] == "n" and action.params["compare_to"] == "3"


def test_action_dialog_rejects_invalid(qapp, no_modal_boxes):
    action = catalog.new_action("key_press")
    dlg = ActionDialog(action)
    dlg._widgets["keys"].setText("ctrl+bogus")
    dlg._accept()
    assert no_modal_boxes and dlg.result() != dlg.DialogCode.Accepted
    assert action.params["keys"] == "ctrl+c"


def test_editor_steps_nesting_move_and_build(qapp):
    ed = MacroEditor()
    ed.load(Macro(name="m"))
    ed.name_edit.setText("demo")
    ed.hotkey_edit.setText("Alt+Ctrl+K")
    rep = ed.add_step("repeat", edit=False)
    # a selected, empty repeat receives the next step as a child
    first = ed.add_step("key_press", edit=False)
    assert ed.actions == [rep] and rep.children == [first]
    cond = ed.add_step("if", edit=False)          # after `first`, inside repeat
    assert rep.children == [first, cond]
    ed.tree.setCurrentItem(ed._find_item(cond).child(1))  # the Else branch
    ed.add_step("delay", edit=False)
    assert len(cond.else_children) == 1 and cond.children == []
    ed.tree.setCurrentItem(ed._find_item(first))
    ed._move(1)
    assert rep.children == [cond, first]
    ed._duplicate()
    assert len(rep.children) == 3
    ed._delete()
    assert len(rep.children) == 2

    cond.params.update(kind="app", value="code")
    macro = ed.build_macro()
    assert macro.hotkey == "ctrl+alt+k" and macro.name == "demo"
    assert macro.actions[0].children[0].else_children[0].type == "delay"
    assert macro.actions[0] is not ed.actions[0]  # build returns a copy


def test_editor_drag_sync_rebuilds_model_from_tree(qapp):
    ed = MacroEditor()
    a = catalog.new_action("key_press")
    rep = catalog.new_action("repeat")
    ed.load(Macro(name="m", actions=[a, rep]))
    a, rep = ed.actions  # load() works on copies

    item = ed._find_item(a)
    ed.tree.takeTopLevelItem(0)
    ed._find_item(rep).addChild(item)   # what Qt's internal move does
    ed.actions = ed._collect(None)
    assert ed.actions[0].type == "repeat" and ed.actions[0].children[0].type == "key_press"


def test_editor_build_validation_errors(qapp):
    ed = MacroEditor()
    ed.load(Macro(name=""))
    with pytest.raises(ValueError, match="name"):
        ed.build_macro()
    ed.name_edit.setText("x")
    ed.hotkey_edit.setText("ctrl")
    with pytest.raises(ValueError, match="hotkey"):
        ed.build_macro()


def test_main_window_create_save_duplicate_export_import(qapp, db, backend, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    service = MacroService(db, backend, hotkeys=None)
    win = MainWindow(db, service)
    win._new_macro()
    win.editor.name_edit.setText("Hello")
    win.editor.hotkey_edit.setText("ctrl+alt+h")
    win.editor.add_step("type_text", edit=False).params["text"] = "hi"
    assert win.save_current()
    saved = db.list_macros()[0]
    assert (saved.name, saved.hotkey, len(saved.actions)) == ("Hello", "ctrl+alt+h", 1)

    win._duplicate_macro()
    assert sorted(m.name for m in db.list_macros()) == ["Hello", "Hello copy"]
    assert [m.hotkey for m in db.list_macros() if m.name == "Hello copy"] == [""]

    # a conflicting hotkey is refused
    win.editor.hotkey_edit.setText("ctrl+alt+h")
    assert not win.save_current()
    win.editor.hotkey_edit.setText("")

    out = tmp_path / "out.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: self.buttons()[1])
    win._export()
    assert out.exists()

    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(out), ""))
    win.editor.dirty = False
    win._import()
    assert len(db.list_macros()) == 4

    win.history.refresh()
    win.close()
    win.service.shutdown()


def test_hotkey_conflict_with_reserved_hotkeys():
    from keyboardmanager.gui.editor import hotkey_conflict

    reserved = {"ctrl+alt+f12": "stop", "ctrl+alt+f11": "pause"}
    assert "pause" in hotkey_conflict(Macro("m", hotkey="ctrl+alt+f11"), [], reserved)
    assert "stop" in hotkey_conflict(Macro("m", hotkey="ctrl+alt+f12"), [], reserved)
    assert hotkey_conflict(Macro("m", hotkey="ctrl+alt+f10"), [], reserved) is None


def test_main_window_pause_resume_controls(qapp, db, backend):
    import time

    def pump(pred):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not pred():
            qapp.processEvents()
            time.sleep(0.01)
        return pred()

    slow = Macro("slow", [catalog.new_action("delay")])
    slow.actions[0].params["ms"] = 10_000
    slow = db.save_macro(slow)
    service = MacroService(db, backend, hotkeys=None)
    win = MainWindow(db, service)
    assert not win.pause_action.isEnabled()
    assert service.run(slow)
    assert pump(lambda: win.pause_action.isEnabled())
    assert win.pause_action.text() == "Pause"

    win.pause_action.trigger()
    assert pump(lambda: win.pause_action.text() == "Resume")
    assert service.paused and "Paused" in win.statusBar().currentMessage()
    win.pause_action.trigger()
    assert pump(lambda: win.pause_action.text() == "Pause")
    assert not service.paused and "Running" in win.statusBar().currentMessage()

    service.pause()
    win.stop_action.trigger()
    assert pump(lambda: not win.pause_action.isEnabled())
    assert win.pause_action.text() == "Pause" and db.list_history()[0].status == "stopped"

    win._select(slow.id)
    win.editor.hotkey_edit.setText("ctrl+alt+f11")
    assert not win.save_current()  # reserved as the pause hotkey
    win.editor.dirty = False
    win.close()
    service.shutdown()


def test_schedule_edit_round_trip(qapp):
    from keyboardmanager.core.schedule import Schedule
    from keyboardmanager.gui.widgets import ScheduleEdit

    w = ScheduleEdit()
    for s in (Schedule(), Schedule("once", at="2031-05-06 07:08"),
              Schedule("weekly", time="18:30", days=(0, 2, 4)), Schedule("interval", minutes=45)):
        w.set_schedule(s)
        assert w.schedule() == s
        assert w.pages.currentIndex() == w.kind_combo.currentIndex()
    changed = []
    w.changed.connect(lambda: changed.append(1))
    w.set_schedule(Schedule("weekly", time="09:00"))
    assert changed == []  # loading is not an edit
    for b in w.day_buttons:
        b.setChecked(False)
    assert changed
    with pytest.raises(ValueError, match="day"):
        w.schedule()


def test_main_window_schedule(qapp, db, backend):
    from keyboardmanager.core.schedule import Schedule

    service = MacroService(db, backend, hotkeys=None, scheduler_thread=False)
    win = MainWindow(db, service)
    win._new_macro()
    ed = win.editor
    ed.schedule_edit.set_schedule(Schedule("weekly", time="09:00", days=(0,)))
    ed.schedule_edit.changed.emit()
    assert ed.schedule_status.text() == "Save to apply"
    assert win.save_current()
    saved = db.list_macros()[0]
    assert saved.schedule == Schedule("weekly", time="09:00", days=(0,))
    assert "next run" in win.statusBar().currentMessage()
    assert ed.schedule_status.text().startswith("Next run: ")
    assert "(scheduled)" in win.list.item(0).text()
    assert 'Schedules: 1, next "New macro"' in win.status_label.text()

    # a time that has already passed is accepted but clearly reported
    ed.schedule_edit.set_schedule(Schedule("once", at="2001-01-01 00:00"))
    assert win.save_current()
    assert "already passed" in win.statusBar().currentMessage()
    assert ed.schedule_status.text() == "No upcoming run"

    # no weekday selected
    ed.schedule_edit.set_schedule(Schedule("weekly", time="09:00"))
    for b in ed.schedule_edit.day_buttons:
        b.setChecked(False)
    assert not win.save_current()

    ed.schedule_edit.set_schedule(Schedule("interval", minutes=5))
    assert win.save_current()
    win.schedules_action.setChecked(False)
    assert not service.schedules_enabled and "Schedules: off" in win.status_label.text()
    assert ed.schedule_status.text() == "Schedules are switched off"
    win.schedules_action.setChecked(True)
    ed.enabled_check.setChecked(False)
    assert win.save_current()
    assert ed.schedule_status.text() == "Macro disabled"
    win.close()
    service.shutdown()
