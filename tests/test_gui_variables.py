"""GUI side of the new actions: the thread bridge, the Variables tab, dialog fields."""

import threading

import pytest
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox, QPlainTextEdit, QComboBox, QWidget

from keyboardmanager.core import actions as catalog
from keyboardmanager.core.models import Macro
from keyboardmanager.core.service import MacroService
from keyboardmanager.core.variables import SavedVariables
from keyboardmanager.gui.uibridge import QtUserInterface
from keyboardmanager.gui.variables import VariablesView
from keyboardmanager.gui.widgets import ActionDialog
from tests_support import FakeHotkeys, wait_until


@pytest.fixture
def bridge(qapp):
    window = QWidget()
    notes = []
    ui = QtUserInterface(window, notify=lambda t, m: notes.append((t, m)))
    ui.notes = notes
    yield ui
    window.deleteLater()


def test_bridge_calls_from_a_worker_thread_run_on_the_gui_thread(bridge, qapp, monkeypatch):
    """What a running macro does: ask / clipboard / notify from its own thread."""
    asked_on = []

    def fake_dialog(parent, title, prompt, text=""):
        asked_on.append(threading.current_thread() is threading.main_thread())
        return f"answer to {prompt}", True

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(fake_dialog))
    results = {}

    def worker():
        results["answer"] = bridge.ask("Name?", "x")
        bridge.set_clipboard("from worker")
        results["clipboard"] = bridge.get_clipboard()
        bridge.notify("Title", "Message")

    thread = threading.Thread(target=worker)
    thread.start()
    assert wait_until(lambda: (qapp.processEvents(), not thread.is_alive())[1], timeout=5)
    assert results == {"answer": "answer to Name?", "clipboard": "from worker"}
    assert asked_on == [True]  # the dialog ran on the GUI thread
    assert bridge.notes == [("Title", "Message")]
    assert QApplication.clipboard().text() == "from worker"


def test_bridge_on_the_gui_thread_calls_directly(bridge, monkeypatch):
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("", False)))
    assert bridge.ask("?", "") is None  # cancelled; and no deadlock calling from the GUI thread


def test_bridge_passes_errors_back_to_the_worker(bridge, qapp, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("dialog failed")

    monkeypatch.setattr(QInputDialog, "getText", staticmethod(broken))
    errors = []
    thread = threading.Thread(target=lambda: errors.append(pytest.raises(RuntimeError, bridge.ask, "?", "")))
    thread.start()
    assert wait_until(lambda: (qapp.processEvents(), not thread.is_alive())[1], timeout=5)
    assert errors and "dialog failed" in str(errors[0].value)


def test_main_window_installs_the_bridge_and_notifications(qapp, db, backend):
    from keyboardmanager.gui.mainwindow import MainWindow

    service = MacroService(db, backend, FakeHotkeys(), scheduler_thread=False)
    window = MainWindow(db, service)
    assert isinstance(service.ui, QtUserInterface)
    window.tray = None
    service.ui.notify("Done", "all good")
    assert window.statusBar().currentMessage() == "Done: all good"
    # a macro that asks, run for real through the service and the bridge
    ask = catalog.new_action("ask")
    ask.params.update(prompt="Who?", variable="who")
    say = catalog.new_action("type_text")
    say.params["text"] = "hi {who}"
    import unittest.mock as mock

    with mock.patch.object(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Ada", True))):
        service.run(db.save_macro(Macro("asker", [ask, say])))
        assert wait_until(lambda: (qapp.processEvents(), ("type", "hi Ada") in backend.calls)[1], timeout=5)
    window._quitting = True
    window.close()
    service.shutdown()


# -------------------------------------------------------------- variables tab
@pytest.fixture
def view(qapp, db):
    store = SavedVariables(load=lambda: db.get_setting("v", ""), save=lambda t: db.set_setting("v", t))
    return VariablesView(store)


def test_variables_tab(view, monkeypatch, no_modal_boxes):
    answers = iter([("count", True), ("3", True)])
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: next(answers)))
    view._add()
    assert view.store.all() == {"count": "3"} and view.table.rowCount() == 1
    view.table.selectRow(0)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("4", True)))
    view._edit()
    assert view.store.get("count") == "4" and view.table.item(0, 1).text() == "4"
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("bad name", True)))
    view._add()
    assert no_modal_boxes[-1][0] == "warning" and view.store.all() == {"count": "4"}
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    view.table.selectRow(0)
    view._delete()
    assert view.store.all() == {} and not view.delete_btn.isEnabled()


def test_variables_tab_refreshes_after_a_run(qapp, db, backend):
    from keyboardmanager.gui.mainwindow import MainWindow

    service = MacroService(db, backend, FakeHotkeys(), scheduler_thread=False)
    window = MainWindow(db, service)
    step = catalog.new_action("set_variable")
    step.params.update(name="visits", value="1", mode="add", scope="saved")
    service.run(db.save_macro(Macro("count", [step])))
    assert wait_until(lambda: (qapp.processEvents(), window.variables_view.table.rowCount() == 1)[1])
    assert window.variables_view.table.item(0, 1).text() == "1"
    window._quitting = True
    window.close()
    service.shutdown()


# ------------------------------------------------------------- dialog fields
def test_multiline_text_field(qapp):
    action = catalog.new_action("type_text")
    action.params["text"] = "line 1\nline 2"
    dlg = ActionDialog(action)
    widget = dlg._widgets["text"]
    assert isinstance(widget, QPlainTextEdit) and widget.toPlainText() == "line 1\nline 2"
    widget.setPlainText("a\nb\nc")
    dlg._accept()
    assert action.params["text"] == "a\nb\nc"  # newlines survive editing


def test_run_macro_offers_existing_macros(qapp):
    action = catalog.new_action("run_macro")
    dlg = ActionDialog(action, macro_names=["Greet", "Sign off"])
    combo = dlg._widgets["name"]
    assert isinstance(combo, QComboBox) and combo.isEditable()
    assert [combo.itemText(i) for i in range(combo.count())] == ["Greet", "Sign off"]
    combo.setCurrentIndex(1)
    dlg._accept()
    assert action.params["name"] == "Sign off"


def test_editor_passes_macro_names_to_dialogs(qapp, monkeypatch):
    from keyboardmanager.gui.editor import MacroEditor

    seen = []
    monkeypatch.setattr(ActionDialog, "exec", lambda self: seen.append(self._macro_names) or 0)
    ed = MacroEditor()
    ed.load(Macro("m"))
    ed.macro_names = lambda: ["A", "B"]
    ed.add_step("run_macro")
    assert seen == [["A", "B"]]


def test_add_menu_has_the_new_categories(qapp):
    from keyboardmanager.gui.editor import MacroEditor

    ed = MacroEditor()
    categories = [a.text() for a in ed.add_btn.menu().actions()]
    assert categories == list(catalog.CATEGORIES) == ["Keyboard", "Mouse", "Flow", "Data", "Vision", "System"]
