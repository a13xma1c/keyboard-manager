"""Macro editor: step operations, dialogs, recording and window detection."""

import pytest
from PySide6.QtCore import QTimer

from keyboardmanager.core import actions as catalog
from keyboardmanager.core.models import Action, ActiveWindow, Macro
from keyboardmanager.gui import editor as editor_mod
from keyboardmanager.gui.editor import MacroEditor
from keyboardmanager.gui.widgets import ActionDialog


@pytest.fixture
def ed(qapp):
    e = MacroEditor()
    e.load(Macro(name="m"))
    return e


@pytest.fixture
def dialogs(monkeypatch):
    """ActionDialog.exec answers with ``result[0]`` (Accepted by default)."""
    result = [ActionDialog.DialogCode.Accepted]
    monkeypatch.setattr(ActionDialog, "exec", lambda self: result[0])
    return result


@pytest.fixture
def immediate_timers(monkeypatch):
    monkeypatch.setattr(QTimer, "singleShot", staticmethod(lambda ms, fn: fn()))


def test_add_menu_lists_every_step_type(ed, dialogs):
    labels = {a.text() for sub in ed.add_btn.menu().actions() for a in sub.menu().actions()}
    assert labels == {spec.label for spec in catalog.SPECS.values()}
    for sub in ed.add_btn.menu().actions():
        for action in sub.menu().actions():
            ed.tree.clearSelection()
            action.trigger()
    assert sorted(a.type for a in ed.actions) == sorted(catalog.SPECS)
    assert ed.dirty


def test_cancelled_add_and_edit_change_nothing(ed, dialogs):
    dialogs[0] = ActionDialog.DialogCode.Rejected
    assert ed.add_step("delay") is None and ed.actions == [] and not ed.dirty
    dialogs[0] = ActionDialog.DialogCode.Accepted
    step = ed.add_step("delay")
    ed.dirty = False
    dialogs[0] = ActionDialog.DialogCode.Rejected
    ed.tree.setCurrentItem(ed._find_item(step))
    ed._edit_selected()
    assert not ed.dirty
    dialogs[0] = ActionDialog.DialogCode.Accepted
    ed.tree.itemDoubleClicked.emit(ed._find_item(step), 0)
    assert ed.dirty


def test_steps_without_fields_skip_the_dialog(ed, monkeypatch):
    monkeypatch.setattr(ActionDialog, "exec", lambda self: pytest.fail("no dialog for 'stop'"))
    ed.add_step("stop")
    ed.tree.setCurrentItem(ed._find_item(ed.actions[0]))
    ed._edit_selected()


def test_buttons_follow_the_selection(ed):
    assert not ed.edit_btn.isEnabled()
    step = ed.add_step("key_press", edit=False)
    assert ed.edit_btn.isEnabled() and ed.del_btn.isEnabled()
    cond = ed.add_step("if", edit=False)
    ed.tree.setCurrentItem(ed._find_item(cond).child(0))  # the "Then" branch header
    assert not ed.del_btn.isEnabled()
    ed._delete()  # nothing selected that can be deleted
    ed._move(1)
    ed._duplicate()
    assert [a.type for a in ed.actions] == ["key_press", "if"]
    assert step in ed.actions


def test_move_stays_within_bounds(ed):
    a = ed.add_step("key_press", edit=False)
    ed.tree.setCurrentItem(ed._find_item(a))
    b = ed.add_step("delay", edit=False)
    ed.tree.setCurrentItem(ed._find_item(a))
    ed._move(-1)
    assert ed.actions == [a, b]
    ed._move(1)
    ed._move(1)
    assert ed.actions == [b, a]


def test_load_and_dirty_tracking(ed):
    from keyboardmanager.core.schedule import Schedule

    macro = Macro("x", [Action("stop")], description="d", hotkey="ctrl+k", app_filter="code",
                  window_filter="main", enabled=False, schedule=Schedule("interval", minutes=3), id=9)
    ed.load(macro)
    assert not ed.dirty
    built = ed.build_macro()
    assert built.to_dict() == macro.to_dict() and built.id == 9
    modified = []
    ed.modified.connect(lambda: modified.append(1))
    for change in (lambda: ed.desc_edit.setPlainText("new"), lambda: ed.window_edit.setText("w"),
                   lambda: ed.enabled_check.setChecked(True),
                   lambda: ed.schedule_edit.minutes_spin.setValue(4)):
        ed.dirty = False
        change()
        assert ed.dirty
    assert len(modified) == 4
    ed.load(None)
    assert ed.name_edit.text() == "" and ed.actions == []


def test_invalid_steps_block_building(ed):
    ed.actions = [Action("key_press", {"keys": "ctrl+nope"})]
    ed.name_edit.setText("x")
    with pytest.raises(ValueError, match="nope"):
        ed.build_macro()


class _FakeSession:
    """Replaces RecordingSession: 'records' the given actions immediately."""

    result: list = []
    fail = False

    def __init__(self, options, parent):
        from PySide6.QtCore import QObject, Signal

        class Emitter(QObject):
            finished = Signal(list)

        self._emitter = Emitter()
        self.finished = self._emitter.finished
        self.options = options

    def start(self):
        if _FakeSession.fail:
            raise RuntimeError("no permission")
        self.finished.emit(list(_FakeSession.result))


@pytest.fixture
def recording(monkeypatch):
    from keyboardmanager.gui.recording import RecordOptionsDialog

    monkeypatch.setattr(editor_mod, "RecordingSession", _FakeSession)
    accepted = [RecordOptionsDialog.DialogCode.Accepted]
    monkeypatch.setattr(RecordOptionsDialog, "exec", lambda self: accepted[0])
    _FakeSession.result, _FakeSession.fail = [], False
    return accepted


def test_recording_inserts_steps_at_the_selection(ed, recording):
    first = ed.add_step("stop", edit=False)
    ed.tree.setCurrentItem(ed._find_item(first))
    _FakeSession.result = [Action("type_text", {"text": "a"}), Action("type_text", {"text": "b"})]
    ed._record()
    assert [a.params.get("text") for a in ed.actions] == [None, "a", "b"] and ed.dirty


def test_recording_nothing_or_failure_explains_why(ed, recording, no_modal_boxes):
    from keyboardmanager.gui.recording import RecordOptionsDialog

    ed._record()
    assert no_modal_boxes[-1][0] == "information" and "macOS" in no_modal_boxes[-1][1][2]
    _FakeSession.fail = True
    ed._record()
    assert no_modal_boxes[-1][0] == "critical" and ed._session is None
    recording[0] = RecordOptionsDialog.DialogCode.Rejected
    ed._record()
    assert ed.actions == []


def test_detect_fills_in_the_application(ed, immediate_timers, monkeypatch, no_modal_boxes):
    monkeypatch.setattr(editor_mod, "get_active_window", lambda: ActiveWindow("Code", "main.py"))
    ed.detect_btn.click()
    assert ed.app_edit.text() == "Code" and ed.detect_btn.isEnabled() and ed.detect_btn.text() == "Detect"
    monkeypatch.setattr(editor_mod, "get_active_window", lambda: ActiveWindow())
    ed.app_edit.setText("")
    ed.detect_btn.click()
    assert ed.app_edit.text() == "" and no_modal_boxes[-1][0] == "information"


def test_drag_and_drop_rules(ed):
    from PySide6.QtWidgets import QTreeWidgetItem

    from keyboardmanager.gui.editor import ROLE_ACTION, ROLE_KIND, StepTree

    rep = ed.add_step("repeat", edit=False)
    ed.tree.clearSelection()
    cond = ed.add_step("if", edit=False)
    ed.tree.clearSelection()
    press = ed.add_step("key_press", edit=False)
    accepts = StepTree._accepts_children
    assert accepts(None) and accepts(ed._find_item(rep))
    assert not accepts(ed._find_item(press)) and not accepts(ed._find_item(cond))
    assert accepts(ed._find_item(cond).child(1))  # an Else branch
    ed._after_drop()
    assert ed.dirty and [a.type for a in ed.actions] == ["repeat", "if", "key_press"]
    assert isinstance(ed._find_item(rep), QTreeWidgetItem) and ed._find_item(rep).data(0, ROLE_KIND) == "action"
    assert ed._find_item(rep).data(0, ROLE_ACTION) is rep
