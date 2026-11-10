"""GUI: the Run tab (live view, debugger, queue), breakpoints in the editor, the
Triggers tab, the expression tester and the busy / queue prompt."""

import pytest
from PySide6.QtWidgets import QDialog, QLabel, QMessageBox

from keyboardmanager.core import actions as catalog
from keyboardmanager.core.models import Action, Macro
from keyboardmanager.core.service import MacroService
from keyboardmanager.core.triggers import TriggerRule
from keyboardmanager.gui import triggers as triggers_gui
from keyboardmanager.gui.editor import MacroEditor
from keyboardmanager.gui.mainwindow import MainWindow
from keyboardmanager.gui.runview import BREAKPOINT_MARK, RunView, step_label
from keyboardmanager.gui.triggers import TriggerDialog, TriggersView
from keyboardmanager.gui.variables import VariablesView
from keyboardmanager.gui.widgets import ActionDialog
from tests_support import FakeHotkeys, wait_until


def A(t, children=None, else_children=None, **params):
    a = catalog.new_action(t)
    a.params.update(params)
    a.children = children or []
    a.else_children = else_children or []
    return a


@pytest.fixture
def win(qapp, db, backend):
    service = MacroService(db, backend, FakeHotkeys(), scheduler_thread=False)
    window = MainWindow(db, service)
    window.TEST_DELAY_S = 0
    # Widgets of earlier tests leave layout / polish events behind; handle them now,
    # not in the middle of a timed wait.
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


def typed(backend):
    return [c[1] for c in backend.calls if c[0] == "type"]


def open_macro(win, db, macro):
    saved = db.save_macro(macro)
    win._reload_macros()
    win._select(saved.id)
    return saved


# ------------------------------------------------------------------ labels
@pytest.mark.parametrize("path, label", [
    ((0,), "1"), ((2, "children", 0), "3.1"), ((1, "else_children", 2), "2 else.3"),
    (("macro:Other", 0), '1 in "Other"'), ((), ""),
])
def test_step_labels(path, label):
    assert step_label(path) == label


# ---------------------------------------------------------------- run view
def test_run_view_idle(qapp, db, backend):
    view = RunView(MacroService(db, backend, scheduler_thread=False))
    assert view.title.text() == "No macro is running."
    assert not any(b.isEnabled() for b in (view.continue_btn, view.step_btn, view.pause_btn, view.stop_btn))
    assert not view.remove_btn.isEnabled() and not view.clear_btn.isEnabled()


def test_live_view_of_a_running_macro(win, db, backend, qapp):
    macro = Macro("Live", [A("set_variable", name="v", value="42"),
                           A("repeat", count=1, children=[A("delay", ms=5000)])])
    win.service.run(macro)
    assert pump(qapp, lambda: "Current step 2.1" in win.run_view.step_label.text())
    view = win.run_view
    assert view.title.text() == '"Live" - running' and "Started by: manual" in view.details.text()
    assert view.timer.isActive()
    current = view._items[(1, "children", 0)]
    assert current.background(0).color() == view.current_color()
    rows = {view.variables.item(r, 0).text(): view.variables.item(r, 1).text() for r in range(view.variables.rowCount())}
    assert rows == {"iteration": "1", "v": "42"}
    assert view.recent.rowCount() >= 1 and not view.panes.isTabEnabled(2)  # not simulated
    assert view.pause_btn.isEnabled() and not view.continue_btn.isEnabled() and not view.step_btn.isEnabled()
    view.pause_btn.click()
    assert pump(qapp, lambda: view.title.text() == '"Live" - paused')
    assert view.continue_btn.isEnabled() and not view.pause_btn.isEnabled()
    assert not view.step_btn.isEnabled()  # stepping needs a debug run
    view.continue_btn.click()  # resumes (no debugger)
    assert pump(qapp, lambda: view.title.text() == '"Live" - running')
    view.stop_btn.click()
    assert pump(qapp, lambda: view.title.text() == '"Live" finished: stopped')
    assert not view.timer.isActive() and view._current is None


def test_queue_in_the_run_view(win, db, backend, qapp, monkeypatch):
    win.service.run(Macro("Long", [A("delay", ms=5000)]))
    for name in ("q1", "q2", "q3"):
        win.service.run(Macro(name, [A("type_text", text=name)]), queue=True)
    view = win.run_view
    assert pump(qapp, lambda: view.queue_list.count() == 3)
    assert view.queue_list.item(0).text() == "1. q1  (manual)" and view.clear_btn.isEnabled()
    view.queue_list.item(1).setSelected(True)
    assert view.remove_btn.isEnabled()
    view.remove_btn.click()
    assert pump(qapp, lambda: view.queue_list.count() == 2)
    assert [n for n, _ in win.service.queue_snapshot()] == ["q1", "q3"]
    view.clear_btn.click()
    assert pump(qapp, lambda: view.queue_list.count() == 0) and not view.clear_btn.isEnabled()
    win.service.stop()
    assert pump(qapp, lambda: win.service.running_macro is None)
    assert typed(backend) == []


def test_busy_run_asks_and_queues(win, db, backend, qapp, no_modal_boxes):
    open_macro(win, db, Macro("Second", [A("type_text", text="second")]))
    win.service.run(Macro("First", [A("delay", ms=300), A("type_text", text="first")]))
    win._run_selected()
    kind, args = no_modal_boxes[-1]
    assert kind == "question" and '"First" is running. Queue "Second"' in args[2]
    assert win.service.queue_snapshot() == [("Second", "manual")]
    assert pump(qapp, lambda: typed(backend) == ["first", "second"])


def test_busy_run_declined(win, db, backend, qapp, monkeypatch):
    open_macro(win, db, Macro("Second", [A("type_text", text="second")]))
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    win.service.run(Macro("First", [A("delay", ms=200)]))
    win._run_selected()
    assert win.service.queue_snapshot() == []
    assert pump(qapp, lambda: win.service.running_macro is None)
    assert typed(backend) == []


# ------------------------------------------------------------------ debug
def test_debug_with_breakpoint_step_and_continue(win, db, backend, qapp):
    open_macro(win, db, Macro("Dbg", [A("type_text", text="a"),
                                      A("repeat", count=2, children=[A("type_text", text="r")]),
                                      A("type_text", text="z")]))
    win.editor.simulate_action.setChecked(False)  # real (fake backend) input, to see the effect
    win.editor.stop_at_start_action.setChecked(False)
    win.editor.select_step([1, "children", 0])
    win.editor.toggle_breakpoint()
    win.editor.debug_btn.click()
    view = win.run_view
    assert win.tabs.currentWidget() is view
    assert pump(qapp, lambda: "stopped before step 2.1" in view.title.text())
    assert typed(backend) == ["a"]
    assert view._items[(1, "children", 0)].text(0).startswith("2.1  " + BREAKPOINT_MARK)
    assert view._current is view._items[(1, "children", 0)]
    assert view.step_label.text() == 'Next step 2.1: Type "r"'
    assert view.step_btn.isEnabled() and view.continue_btn.isEnabled() and not view.pause_btn.isEnabled()
    view.step_btn.click()  # runs the "r", stops before the loop's second round of it
    assert pump(qapp, lambda: typed(backend) == ["a", "r"] and win.service.paused)
    assert pump(qapp, lambda: view.step_btn.isEnabled())  # the view has caught up
    view.step_btn.click()  # the second "r", then stops before the last step
    assert pump(qapp, lambda: typed(backend) == ["a", "r", "r"] and win.service.paused)
    assert pump(qapp, lambda: view.step_label.text() == 'Next step 3: Type "z"')
    assert pump(qapp, lambda: view.continue_btn.isEnabled())
    view.continue_btn.click()  # no breakpoint left on the way: to the end
    assert pump(qapp, lambda: win.service.running_macro is None)
    assert typed(backend) == ["a", "r", "r", "z"]
    assert pump(qapp, lambda: view.title.text() == '"Dbg" finished: success')
    assert db.list_history()[0].trigger == "debug"


def test_debug_simulates_input_by_default(win, db, backend, qapp):
    open_macro(win, db, Macro("Sim", [A("type_text", text="secret"), A("key_press", keys="enter")]))
    assert win.editor.debug_simulate and win.editor.debug_stop_at_start
    win._debug()
    view = win.run_view
    assert pump(qapp, lambda: "stopped before step 1" in view.title.text())
    assert view.panes.isTabEnabled(2) and "simulated" in view.details.text()
    view.continue_btn.click()
    assert pump(qapp, lambda: win.service.running_macro is None)
    assert backend.calls == []
    # what the run would have sent stays visible after it
    assert pump(qapp, lambda: view.title.text() == '"Sim" finished: success')
    assert [view.simulated.item(i).text().split(" s  ", 1)[1] for i in range(view.simulated.count())] == [
        "type 'secret'", "key down enter", "key up enter"]
    assert view.recent.rowCount() == 2 and "2 steps run" in view.details.text()


def test_debug_refused_when_invalid_or_busy(win, db, backend, no_modal_boxes):
    open_macro(win, db, Macro("Bad", [A("run_macro", name="Missing")]))
    win._debug()
    assert no_modal_boxes[-1][0] == "warning" and "Cannot debug" in no_modal_boxes[-1][1][1]
    open_macro(win, db, Macro("Good", [A("type_text", text="x")]))
    win.service.run(Macro("Busy", [A("delay", ms=300)]))
    win._debug()
    assert no_modal_boxes[-1][0] == "information"
    win.editor.name_edit.setText("")
    win._debug()
    assert no_modal_boxes[-1][0] == "warning"
    win.service.stop()


# ------------------------------------------------------------- breakpoints
@pytest.fixture
def ed(qapp):
    e = MacroEditor()
    e.load(Macro("m", [A("type_text", text="a"), A("if", kind="app", value="x",
                                                   children=[A("type_text", text="t")],
                                                   else_children=[A("type_text", text="e")]),
                       A("type_text", text="c")]))
    return e


def item_texts(ed):
    out = []

    def walk(item):
        out.append(item.text(0))
        for i in range(item.childCount()):
            walk(item.child(i))

    for i in range(ed.tree.topLevelItemCount()):
        walk(ed.tree.topLevelItem(i))
    return out


def test_toggle_breakpoints(ed):
    assert not ed.breakpoint_btn.isEnabled()
    ed.select_step([1, "else_children", 0])
    assert ed.breakpoint_btn.isEnabled()
    ed.toggle_breakpoint()
    assert ed.breakpoint_paths() == {(1, "else_children", 0)}
    assert BREAKPOINT_MARK + 'Type "e"' in item_texts(ed)
    assert not ed.dirty  # breakpoints are not part of the macro
    ed.select_step([0])
    ed.toggle_breakpoint()
    assert ed.breakpoint_paths() == {(0,), (1, "else_children", 0)}
    ed.toggle_breakpoint()  # again: off
    assert ed.breakpoint_paths() == {(1, "else_children", 0)}
    ed.clear_breakpoints()
    assert ed.breakpoint_paths() == set() and not any(BREAKPOINT_MARK in t for t in item_texts(ed))


def test_toggle_on_several_steps(ed):
    ed.select_step([0])
    ed.toggle_breakpoint()
    ed.tree.selectAll()
    ed.toggle_breakpoint()  # not all had one: all get one
    assert ed.breakpoint_paths() == {(0,), (1,), (2,)}
    ed.toggle_breakpoint()  # now all have one: all removed
    assert ed.breakpoint_paths() == set()


def test_f9_toggles_a_breakpoint(ed):
    ed.select_step([2])
    f9 = [a for a in ed.tree.actions() if a.shortcut().toString() == "F9"]
    assert len(f9) == 1
    f9[0].trigger()
    assert ed.breakpoint_paths() == {(2,)}


def test_breakpoints_follow_their_step(ed):
    ed.select_step([2])
    ed.toggle_breakpoint()
    ed.select_step([0])
    ed._delete()  # the step before it goes away: the breakpoint moves along with its step
    assert ed.breakpoint_paths() == {(1,)}
    ed.select_step([1])
    ed._delete()  # its own step is deleted: the breakpoint is gone
    assert ed.breakpoint_paths() == set()


def test_breakpoints_survive_saving_and_undo(ed):
    ed.select_step([2])
    ed.toggle_breakpoint()
    ed.load(ed.build_macro(), keep_history=True)  # what saving does
    assert ed.breakpoint_paths() == {(2,)}
    ed.select_step([0])
    ed._move(1)
    ed.undo_stack.undo()
    assert ed.breakpoint_paths() == {(2,)}
    ed.load(Macro("other", [A("type_text", text="x"), A("type_text", text="y"), A("type_text", text="z")]))
    assert ed.breakpoint_paths() == set()  # another macro: no breakpoints


# --------------------------------------------------------------- when busy
def test_when_busy_field(ed):
    assert ed.busy_combo.currentData() == "skip" and ed.build_macro().when_busy == "skip"
    ed.busy_combo.setCurrentIndex(ed.busy_combo.findData("queue"))
    assert ed.dirty and ed.build_macro().when_busy == "queue"
    ed.load(Macro("q", when_busy="queue"))
    assert ed.busy_combo.currentData() == "queue" and not ed.dirty


def test_when_busy_is_saved(win, db):
    saved = open_macro(win, db, Macro("Q", [A("type_text", text="x")]))
    win.editor.busy_combo.setCurrentIndex(win.editor.busy_combo.findData("queue"))
    assert win.save_current()
    assert db.get_macro(saved.id).when_busy == "queue"


# ---------------------------------------------------------------- triggers
def test_trigger_dialog_builds_each_kind(qapp):
    dialog = TriggerDialog(None, ["Build", "Notify"])
    dialog.name_edit.setText("after build")
    dialog.macro_combo.setCurrentText("Notify")
    dialog.after_combo.setCurrentText("Build")
    dialog.result_combo.setCurrentText("error")
    dialog.queue_check.setChecked(True)
    rule = dialog.rule()
    assert (rule.kind, rule.params, rule.macro, rule.queue, rule.enabled) == (
        "macro_finished", {"macro": "Build", "result": "error"}, "Notify", True, True)
    for kind, setup, params in (
        ("clipboard", lambda: dialog.contains_edit.setText("http"), {"contains": "http"}),
        ("file", lambda: dialog.path_edit.setText(" /tmp/x "), {"path": "/tmp/x"}),
        ("becomes_true", lambda: dialog.expression_edit.setText("n > 1"), {"expression": "n > 1"}),
    ):
        dialog.kind_combo.setCurrentIndex(dialog.kind_combo.findData(kind))
        assert dialog.pages.currentIndex() == dialog.kind_combo.currentIndex()
        setup()
        assert dialog.rule().params == params and dialog.rule().kind == kind
    dialog.kind_combo.setCurrentIndex(dialog.kind_combo.findData("clipboard"))
    dialog.contains_edit.setText("")
    assert dialog.rule().params == {}


def test_trigger_dialog_shows_an_existing_rule(qapp):
    rule = TriggerRule("r", "becomes_true", "M", {"expression": "x > 1"}, condition="y", queue=True,
                       enabled=False, id=7)
    dialog = TriggerDialog(rule, ["M"])
    assert dialog.kind_combo.currentData() == "becomes_true" and dialog.expression_edit.text() == "x > 1"
    assert dialog.condition_edit.text() == "y" and dialog.queue_check.isChecked()
    assert not dialog.enabled_check.isChecked()
    assert dialog.rule().to_dict() == rule.to_dict() and dialog.rule().id == 7


def test_trigger_dialog_validates(qapp, no_modal_boxes):
    dialog = TriggerDialog(None, [])
    dialog._accept()
    assert dialog.result() != QDialog.DialogCode.Accepted
    assert no_modal_boxes[-1][0] == "warning" and "name" in no_modal_boxes[-1][1][2]
    dialog.name_edit.setText("n")
    dialog.macro_combo.setCurrentText("M")
    dialog.after_combo.setCurrentText("X")
    dialog.condition_edit.setText("1 +")
    dialog._accept()
    assert "syntax" in no_modal_boxes[-1][1][2]
    dialog.condition_edit.setText("")
    dialog._accept()
    assert dialog.result() == QDialog.DialogCode.Accepted


@pytest.fixture
def dialog_answers(monkeypatch):
    """TriggerDialog.exec fills in the dialog with ``fill`` and accepts."""
    state = {"fill": None, "accept": True}

    def exec_(self):
        if state["fill"]:
            state["fill"](self)
        return QDialog.DialogCode.Accepted if state["accept"] else QDialog.DialogCode.Rejected

    monkeypatch.setattr(TriggerDialog, "exec", exec_)
    return state


def test_triggers_tab_add_edit_toggle_delete(win, db, qapp, dialog_answers, no_modal_boxes):
    db.save_macro(Macro("Build", [A("type_text", text="b")]))
    win._reload_macros()
    view = win.triggers_view
    win.show()
    win.tabs.setCurrentWidget(view)
    qapp.processEvents()
    assert view.timer.isActive() and not view.edit_btn.isEnabled()
    win.tabs.setCurrentWidget(win.editor)
    qapp.processEvents()
    assert not view.timer.isActive()  # polls only while visible
    win.tabs.setCurrentWidget(view)

    def fill(d):
        d.name_edit.setText("copy")
        d.kind_combo.setCurrentIndex(d.kind_combo.findData("clipboard"))
        d.macro_combo.setCurrentText("Build")

    dialog_answers["fill"] = fill
    view.add_btn.click()
    [rule] = db.list_triggers()
    assert view.table.rowCount() == 1
    assert [view.table.item(0, c).text() for c in range(5)] == ["\u2713", "copy", "the clipboard changes",
                                                                "Build", ""]
    view.table.selectRow(0)
    assert view.edit_btn.isEnabled() and view.toggle_btn.text() == "Disable"
    dialog_answers["fill"] = lambda d: d.condition_edit.setText("true")
    view.edit_btn.click()
    assert db.list_triggers()[0].condition == "true" and len(db.list_triggers()) == 1
    view.toggle_btn.click()
    assert not db.list_triggers()[0].enabled and view.table.item(0, 0).text() == ""
    assert view.table.item(0, 5).text() == "disabled" and view.toggle_btn.text() == "Enable"
    view.toggle_btn.click()
    assert db.list_triggers()[0].enabled
    dialog_answers["accept"] = False
    dialog_answers["fill"] = lambda d: d.name_edit.setText("changed")
    view.edit_btn.click()
    assert db.list_triggers()[0].name == "copy"  # cancelled
    view.delete_btn.click()  # confirmed (the message boxes answer Yes)
    assert db.list_triggers() == [] and view.table.rowCount() == 0


def test_triggers_tab_status_and_missing_macro(win, db, qapp):
    rule = win.service.save_trigger(TriggerRule("t", "clipboard", "Ghost"))
    view = win.triggers_view
    view.refresh()
    assert view.table.item(0, 3).text() == "Ghost  (no such macro)"
    win.service._fire_rule(rule, "trigger", 0)
    view.show_status()
    assert 'no macro named "Ghost"' in view.table.item(0, 5).text()
    win.autorun_action.setChecked(False)
    view.show_status()
    assert "Auto-run is off" in view.state_label.text()


def test_status_bar_counts_triggers(win, db):
    db.save_macro(Macro("Build", [A("type_text", text="b")]))
    win.service.save_trigger(TriggerRule("t", "clipboard", "Build"))
    win._reload_macros()
    win._update_status()
    assert "1 trigger" in win.status_label.text()


def test_renaming_a_macro_updates_its_triggers(win, db, qapp):
    open_macro(win, db, Macro("Old", [A("type_text", text="x")]))
    win.service.save_trigger(TriggerRule("t", "macro_finished", "Old", {"macro": "Old"}))
    win.editor.name_edit.setText("New")
    assert win.save_current()
    [rule] = db.list_triggers()
    assert rule.macro == "New" and rule.params["macro"] == "New"
    assert "1 trigger(s) updated" in win.statusBar().currentMessage()


def test_check_tab_lists_trigger_problems(win, db):
    win.service.save_trigger(TriggerRule("t", "clipboard", "Ghost"))
    win.check_view.refresh()
    assert any(i.macro_name == "trigger: t" and "Ghost" in i.message for i in win.check_view.issues)
    row = next(r for r, i in enumerate(win.check_view.issues) if i.macro_name == "trigger: t")
    win.check_view._open(win.check_view.table.item(row, 3))  # no macro to open: nothing happens


def test_trigger_started_run_shows_in_the_run_view(win, db, backend, qapp):
    db.save_macro(Macro("Build", [A("type_text", text="b")]))
    db.save_macro(Macro("After", [A("delay", ms=5000)]))
    win.service.save_trigger(TriggerRule("t", "macro_finished", "After", {"macro": "Build"}))
    win.service.run(win.service.find_macro("Build"))
    assert pump(qapp, lambda: win.run_view.title.text() == '"After" - running')
    assert 'trigger "t"' in win.run_view.details.text()
    win.service.stop()


# ------------------------------------------------------------- expressions
def test_expression_tester(qapp, db, backend):
    service = MacroService(db, backend, scheduler_thread=False)
    service.saved_variables.set("n", "20")
    view = VariablesView(service.saved_variables, evaluate=service.evaluate)
    view.expression_edit.setText("n / 8")
    view.evaluate_expression()
    assert view.result_label.text() == "= 2.5"
    view.expression_edit.setText("nope + 1")
    view.evaluate_expression()
    assert "unknown variable nope" in view.result_label.text()
    view.expression_edit.setText("import os")
    view.evaluate_expression()
    assert view.result_label.text().startswith("\u26a0")
    view.expression_edit.setText("  ")
    view.evaluate_expression()
    assert view.result_label.text() == ""


def test_variables_view_without_an_evaluator(qapp):
    from keyboardmanager.core.variables import SavedVariables

    view = VariablesView(SavedVariables(load=lambda: "", save=lambda text: None))
    assert not hasattr(view, "expression_edit")


@pytest.mark.parametrize("step_type, commands", [("script", True), ("if", False), ("set_variable", False)])
def test_action_dialog_shows_the_language_reference(qapp, step_type, commands):
    dialog = ActionDialog(catalog.new_action(step_type))
    text = dialog._reference.text()
    assert "Functions:" in text and ("Commands:" in text) is commands


def test_action_dialog_for_scripts(qapp, no_modal_boxes):
    action = catalog.new_action("script")
    dialog = ActionDialog(action)
    editor = dialog._widgets["code"]
    editor.setPlainText("import os")
    dialog._accept()
    assert no_modal_boxes[-1][0] == "warning" and "import" in no_modal_boxes[-1][1][2]
    editor.setPlainText('x = 1\ntype(x)')
    dialog._accept()
    assert action.params["code"] == 'x = 1\ntype(x)'
    assert not any(isinstance(w, QLabel) and w.text() == "" for w in [dialog._reference])


def test_action_dialog_without_reference(qapp):
    dialog = ActionDialog(Action("delay", {"ms": 5}))
    assert not hasattr(dialog, "_reference")


def test_triggers_view_standalone(qapp, db, backend, dialog_answers, no_modal_boxes):
    service = MacroService(db, backend, scheduler_thread=False)
    view = TriggersView(service, lambda: ["M"])
    view._add()  # an invalid rule (here: no name) is never saved
    assert db.list_triggers() == [] and no_modal_boxes[-1][0] == "warning"
    assert triggers_gui.COLUMNS[-1] == "Last"
