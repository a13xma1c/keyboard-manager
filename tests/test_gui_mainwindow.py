"""Main window: list, CRUD, running, hotkey settings, tray, quitting, import/export."""

import json

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMessageBox, QSystemTrayIcon

from keyboardmanager.core import actions as catalog
from keyboardmanager.core.executor import ExecutionResult
from keyboardmanager.core.models import Macro
from keyboardmanager.core.service import MacroService
from keyboardmanager.gui.mainwindow import MainWindow
from tests_support import FakeHotkeys, wait_until


def typing_macro(name="Greet", text="hi", **kw):
    m = Macro(name, [catalog.new_action("type_text")], **kw)
    m.actions[0].params["text"] = text
    return m


@pytest.fixture
def hk():
    return FakeHotkeys()


@pytest.fixture
def win(qapp, db, backend, hk):
    service = MacroService(db, backend, hk, scheduler_thread=False)
    window = MainWindow(db, service)
    yield window
    window.editor.dirty = False
    window._quitting = True
    window.close()
    service.shutdown()


def pump(qapp, predicate, timeout=3.0):
    return wait_until(lambda: (qapp.processEvents(), predicate())[1], timeout)


def answer(monkeypatch, button):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: button)


# --------------------------------------------------------------------- list
def test_list_shows_hotkeys_schedules_and_disabled_and_filters(win, db):
    from keyboardmanager.core.schedule import Schedule

    db.save_macro(typing_macro("Alpha", hotkey="ctrl+alt+a"))
    db.save_macro(typing_macro("Beta", enabled=False, schedule=Schedule("interval", minutes=5)))
    win._reload_macros()
    texts = [i.text(0) for i in win.list.macro_items()]
    assert texts == ["Alpha  [ctrl+alt+a]", "Beta  (scheduled)  (disabled)"]
    assert "Schedule: Every 5 min" in win.list.macro_items()[1].toolTip(0)
    win.search.setText("alt+a")  # search matches names and hotkeys
    assert len(win.list.macro_items()) == 1 and win.list.macro_items()[0].text(0).startswith("Alpha")
    win.search.setText("nothing")
    assert len(win.list.macro_items()) == 0


def test_selection_and_unsaved_changes(win, db, monkeypatch):
    a, b = db.save_macro(typing_macro("A")), db.save_macro(typing_macro("B"))
    win._reload_macros()
    win._select(a.id)
    assert win.editor.name_edit.text() == "A" and win.run_btn.isEnabled()
    win.editor.name_edit.setText("A edited")
    answer(monkeypatch, QMessageBox.StandardButton.Cancel)
    win.list.setCurrentItem(win.list.macro_items()[1])  # switching away is refused: the edit is kept
    assert win._current_id == a.id and win.editor.name_edit.text() == "A edited"
    answer(monkeypatch, QMessageBox.StandardButton.Discard)
    win.list.setCurrentItem(win.list.macro_items()[1])
    assert win._current_id == b.id and win.editor.name_edit.text() == "B"
    win._select(None)
    assert not win.editor.isEnabled() and not win.run_btn.isEnabled() and win.export_btn.isEnabled()


def test_new_duplicate_delete(win, db, hk, monkeypatch):
    win._new_macro()
    win._new_macro()
    assert sorted(m.name for m in db.list_macros()) == ["New macro", "New macro (2)"]
    win.editor.hotkey_edit.setText("ctrl+alt+n")
    assert win.save_current()
    assert "ctrl+alt+n" in hk.mapping
    win._duplicate_macro()
    assert win.editor.name_edit.text() == "New macro (2) copy" and win.editor.hotkey_edit.text() == ""
    answer(monkeypatch, QMessageBox.StandardButton.No)
    win._delete_macro()
    assert len(db.list_macros()) == 3
    answer(monkeypatch, QMessageBox.StandardButton.Yes)
    win._delete_macro()
    assert len(db.list_macros()) == 2 and win._current_id is None


def test_save_validation(win, db, no_modal_boxes):
    db.save_macro(typing_macro("Taken", hotkey="ctrl+alt+t"))
    win._reload_macros()
    win._new_macro()
    ed = win.editor
    ed.name_edit.setText("")
    assert not win.save_current()
    ed.name_edit.setText("taken")
    assert not win.save_current()  # names are unique, ignoring case
    ed.name_edit.setText("Fresh")
    ed.hotkey_edit.setText("ctrl+alt+t")
    assert not win.save_current()  # hotkey used by another global macro
    ed.app_edit.setText("chrome")
    assert win.save_current()  # ...but fine for an app-specific one
    ed.hotkey_edit.setText("ctrl+alt+f12")
    assert not win.save_current()  # reserved (stop)
    ed.hotkey_edit.setText("ctrl")
    assert not win.save_current()
    assert [k for k, _ in no_modal_boxes].count("warning") == 5


def test_save_reports_hotkey_registration_failure(win, hk, no_modal_boxes):
    hk.failures = {"ctrl+alt+x": "already grabbed by another program"}
    win._new_macro()
    win.editor.hotkey_edit.setText("ctrl+alt+x")
    assert win.save_current()
    assert no_modal_boxes[-1][0] == "warning" and "already grabbed" in no_modal_boxes[-1][1][2]
    assert "problem" in win.status_label.text()


# --------------------------------------------------------------------- run
def test_run_selected_and_busy(win, db, backend, qapp, monkeypatch, no_modal_boxes):
    win.TEST_DELAY_S = 0
    m = db.save_macro(typing_macro(text="hello"))
    win._reload_macros()
    win._select(m.id)
    win._run_selected()
    assert pump(qapp, lambda: '"Greet": success' in win.statusBar().currentMessage())
    assert ("type", "hello") in backend.calls and not win.stop_action.isEnabled()
    # a second run while one is active is not started at once: the user may queue it
    calls = []
    monkeypatch.setattr(win.service, "run", lambda *a, **k: calls.append(k.get("queue")) or k.get("queue"))
    win._run_selected()
    assert no_modal_boxes[-1][0] == "question" and "Queue" in no_modal_boxes[-1][1][2]
    assert calls == [False, True]
    assert "Queued" in win.statusBar().currentMessage()


def test_test_run_uses_the_unsaved_editor_content(win, db, backend, qapp):
    win.TEST_DELAY_S = 0
    m = db.save_macro(typing_macro(text="saved"))
    win._reload_macros()
    win._select(m.id)
    win.editor.actions[0].params["text"] = "edited"
    win._test_run()
    assert pump(qapp, lambda: ("type", "edited") in backend.calls)
    assert db.list_history()[0].trigger == "test run"


def test_invalid_test_run_is_refused(win, no_modal_boxes):
    win._new_macro()
    win.editor.name_edit.setText("")
    win._test_run()
    assert no_modal_boxes[-1][0] == "warning"


def test_error_result_notifies_via_tray_when_hidden(win, qapp):
    shown = []
    win.tray = type("Tray", (), {"showMessage": lambda self, *a: shown.append(a), "setToolTip": lambda *a: None})()
    win.hide()
    win._on_service_event("finished", (Macro("m"), ExecutionResult("error", message="denied")))
    assert shown and shown[0][:2] == ("m", "denied")
    assert "error - denied" in win.statusBar().currentMessage()


# ----------------------------------------------------------- hotkey settings
def test_change_stop_and_pause_hotkeys(win, db, hk, monkeypatch, no_modal_boxes):
    db.save_macro(typing_macro(hotkey="ctrl+alt+u"))
    win._reload_macros()

    def dialog(text, ok=True):
        monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: (text, ok))

    dialog("Ctrl+Shift+F1")
    win._change_stop_hotkey()
    assert win.service.stop_hotkey == "ctrl+shift+f1" and "ctrl+shift+f1" in hk.mapping
    dialog("ctrl+alt+u")
    win._change_stop_hotkey()  # used by a macro
    dialog("nonsense+")
    win._change_stop_hotkey()
    dialog("ctrl+shift+f1")
    win._change_pause_hotkey()  # same as stop
    assert [k for k, _ in no_modal_boxes] == ["warning"] * 3
    dialog("ctrl+alt+p")
    win._change_pause_hotkey()
    assert win.service.pause_hotkey == "ctrl+alt+p" and "pause = ctrl+alt+p" in win.status_label.text()
    dialog("")
    win._change_pause_hotkey()  # empty = no pause hotkey
    assert win.service.pause_hotkey == "" and "pause =" not in win.status_label.text()
    dialog("ctrl+alt+q", ok=False)  # cancelled
    win._change_pause_hotkey()
    assert win.service.pause_hotkey == ""


def test_toggles(win, hk, db):
    db.save_macro(typing_macro(hotkey="ctrl+alt+u"))
    win._reload_macros()
    win.service.reload()
    win.hotkeys_action.setChecked(False)
    assert "ctrl+alt+u" not in hk.mapping and "Global hotkeys: off" in win.status_label.text()
    win.hotkeys_action.setChecked(True)
    assert "ctrl+alt+u" in hk.mapping
    win.autorun_action.setChecked(False)
    assert "Auto-run: off" in win.status_label.text()


# ----------------------------------------------------------- tray & quitting
@pytest.fixture
def tray_win(qapp, db, backend, monkeypatch):
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    messages = []
    monkeypatch.setattr(QSystemTrayIcon, "showMessage", lambda self, *a: messages.append(a))
    quits = []
    monkeypatch.setattr(QApplication, "quit", staticmethod(lambda: quits.append(1)))
    service = MacroService(db, backend, FakeHotkeys(), scheduler_thread=False)
    window = MainWindow(db, service)
    window.show()
    yield window, messages, quits
    window._quitting = True
    window.close()
    service.shutdown()


def test_closing_the_window_keeps_running_in_the_tray(tray_win):
    win, messages, quits = tray_win
    assert win.tray is not None and win.tray.contextMenu().actions()
    win.close()
    assert not win.isVisible() and quits == [] and "Still running" in messages[0][1]
    win._show_window()
    assert win.isVisible()


def test_tray_quit(tray_win):
    win, _, quits = tray_win
    win.quit()
    assert quits == [1] and win._quitting and win.service.hotkeys.stopped


def test_application_quit_event(tray_win, monkeypatch):
    """Cmd+Q / Dock > Quit (macOS) and logging out send a Quit event to the application."""
    win, messages, _ = tray_win
    win._new_macro()
    win.editor.name_edit.setText("unsaved edit")
    answer(monkeypatch, QMessageBox.StandardButton.Cancel)
    event = QEvent(QEvent.Type.Quit)
    QApplication.sendEvent(QApplication.instance(), event)
    assert not event.isAccepted() and not win._quitting  # quit (or logout) cancelled

    answer(monkeypatch, QMessageBox.StandardButton.Discard)
    event = QEvent(QEvent.Type.Quit)
    QApplication.sendEvent(QApplication.instance(), event)
    assert win._quitting
    win.close()
    assert messages == []  # really quitting: no "still running in the tray" notice


def test_close_without_tray_quits_after_confirmation(win, monkeypatch):
    quits = []
    monkeypatch.setattr(QApplication, "quit", staticmethod(lambda: quits.append(1)))
    win.tray = None
    win.show()
    win._new_macro()
    win.editor.name_edit.setText("unsaved")
    answer(monkeypatch, QMessageBox.StandardButton.Cancel)
    win.close()
    assert win.isVisible() and quits == []
    answer(monkeypatch, QMessageBox.StandardButton.Discard)
    win.close()
    assert quits == [1] and win._quitting


# ------------------------------------------------------------ import/export
def test_export_choices(win, db, tmp_path, monkeypatch, no_modal_boxes):
    win._export()
    assert no_modal_boxes[-1][0] == "information"  # nothing to export
    a = db.save_macro(typing_macro("A"))
    db.save_macro(typing_macro("B"))
    win._reload_macros()
    win._select(a.id)
    out = tmp_path / "one.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: self.buttons()[0])  # "Selected macro"
    win._export()
    assert [m["name"] for m in json.loads(out.read_text())["macros"]] == ["A"]
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: None)  # cancelled
    out.unlink()
    win._export()
    assert not out.exists()


def test_export_write_error(win, db, tmp_path, monkeypatch, no_modal_boxes):
    db.save_macro(typing_macro("A"))
    win._reload_macros()
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(tmp_path / "no" / "dir" / "x.json"), ""))
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    monkeypatch.setattr(QMessageBox, "clickedButton", lambda self: self.buttons()[1])
    win._export()
    assert no_modal_boxes[-1][0] == "critical"


def test_import_results_and_errors(win, db, tmp_path, monkeypatch, no_modal_boxes):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(bad), ""))
    win._import()
    assert no_modal_boxes[-1][0] == "critical"

    good = tmp_path / "good.json"
    good.write_text(json.dumps({"format": "keyboardmanager-macros", "version": 1, "macros": [
        {"name": "Timed", "actions": [], "schedule": {"kind": "interval", "minutes": 10}},
        {"name": "Broken", "actions": [{"type": "nope"}]},
    ]}))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(good), ""))
    win._import()
    kind, args = no_modal_boxes[-1]
    assert kind == "information"
    assert "Imported 1 macro(s)" in args[2] and "will run by themselves" in args[2] and "Broken" in args[2]
    assert win.editor.name_edit.text() == "Timed"

    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: ("", ""))  # cancelled
    win._import()
    assert len(db.list_macros()) == 1


def test_quit_while_hidden_in_the_tray_cleans_up(tray_win, db, backend):
    """Cmd+Q / Dock > Quit / logout with the window hidden: no closeEvent happens and on
    macOS exec() never returns, so the Quit event itself must shut everything down."""
    win, _, _ = tray_win
    m = Macro("hold", [catalog.new_action("key_down"), catalog.new_action("delay")])
    m.actions[0].params["key"] = "alt"
    m.actions[1].params["ms"] = 10_000
    win.service.run(db.save_macro(m))
    assert wait_until(lambda: ("down", "alt") in backend.calls)
    win.close()  # hidden in the tray, still running
    event = QEvent(QEvent.Type.Quit)
    QApplication.sendEvent(QApplication.instance(), event)
    assert win._quitting and win.service.hotkeys.stopped
    assert ("up", "alt") in backend.calls and win.service.running_macro is None


# ------------------------------------------- quitting on macOS (AppKit exit())
@pytest.fixture
def filters_removed(monkeypatch):
    removed = []
    original = QApplication.removeEventFilter
    monkeypatch.setattr(QApplication, "removeEventFilter",
                        lambda self, obj: (removed.append(obj), original(self, obj)))
    return removed


def send_quit():
    event = QEvent(QEvent.Type.Quit)
    QApplication.sendEvent(QApplication.instance(), event)
    return event


def test_quit_on_macos_cleans_up_then_ends_the_process(tray_win, db, backend, monkeypatch,
                                                       no_process_exit, filters_removed):
    """AppKit would end the process with exit(), whose C++ teardown crashes in any Python
    callback (such as this window's application-wide event filter): clean up first,
    remove the filter, and end the process directly."""
    import sys

    win, _, _ = tray_win
    monkeypatch.setattr(sys, "platform", "darwin")
    m = Macro("hold", [catalog.new_action("key_down"), catalog.new_action("delay")])
    m.actions[0].params["key"] = "cmd"
    m.actions[1].params["ms"] = 10_000
    win.service.run(db.save_macro(m))
    assert wait_until(lambda: ("down", "cmd") in backend.calls)
    from keyboardmanager.gui import mainwindow

    at_exit = {}  # a real exit ends everything, so check the state at that moment
    monkeypatch.setattr(mainwindow, "EXIT_PROCESS", lambda code: (no_process_exit.append(code), at_exit.update(
        filter_removed=win in filters_removed, keys_released=("up", "cmd") in backend.calls,
        hotkeys_stopped=win.service.hotkeys.stopped, macro_done=win.service.running_macro is None)))
    send_quit()
    assert at_exit == {"filter_removed": True, "keys_released": True,
                       "hotkeys_stopped": True, "macro_done": True}
    assert no_process_exit == [0]  # the process ends, with success
    import sqlite3

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        db.list_macros()  # the database was closed before exiting
    send_quit()
    assert no_process_exit == [0]  # only once


def test_tray_quit_on_macos_ends_the_process(tray_win, monkeypatch, no_process_exit):
    import sys

    win, _, quits = tray_win
    monkeypatch.setattr(sys, "platform", "darwin")
    win.quit()  # QApplication.quit() would now send the Quit event
    assert quits == [1] and no_process_exit == []
    send_quit()
    assert no_process_exit == [0]


def test_cancelled_quit_keeps_everything_running(tray_win, monkeypatch, no_process_exit, filters_removed):
    import sys

    win, _, _ = tray_win
    monkeypatch.setattr(sys, "platform", "darwin")
    win._new_macro()
    win.editor.name_edit.setText("unsaved")
    answer(monkeypatch, QMessageBox.StandardButton.Cancel)
    assert not send_quit().isAccepted()
    assert no_process_exit == [] and filters_removed == [] and not win.service.hotkeys.stopped


def test_quit_elsewhere_lets_the_event_loop_finish(tray_win, monkeypatch, no_process_exit, filters_removed):
    import sys

    win, _, _ = tray_win
    monkeypatch.setattr(sys, "platform", "linux")
    send_quit()
    assert win in filters_removed and win.service.hotkeys.stopped
    assert no_process_exit == []  # exec() returns normally there; main() finishes up


# --------------------------------------------------- sequences and event triggers
def test_sequence_and_event_markers_in_the_list(win, db):
    db.save_macro(Macro("Seq", hotkey="ctrl+k ctrl+s"))
    db.save_macro(Macro("Evt", app_filter="mail", event="focus_gained"))
    win._reload_macros()
    texts = {i.text(0) for i in win.list.macro_items()}
    assert texts == {"Seq  [ctrl+k ctrl+s]", "Evt  (on event)"}
    assert "Event: When its application gains focus" in win.list.macro_items()[0].toolTip(0)
    assert "2 on events" not in win.status_label.text()
    win._update_status()
    assert "1 on events" in win.status_label.text()


def test_sequence_hotkey_conflicts_on_save(win, db, no_modal_boxes):
    db.save_macro(typing_macro("Long", hotkey="ctrl+k ctrl+s"))
    win._reload_macros()
    win._new_macro()
    ed = win.editor
    ed.hotkey_edit.setText("ctrl+k")  # how "Long" starts: would always fire first
    assert not win.save_current()
    assert "overlaps" in no_modal_boxes[-1][1][2]
    ed.hotkey_edit.setText("ctrl+k ctrl+s x")
    assert not win.save_current()
    ed.hotkey_edit.setText("ctrl+k ctrl+a")  # same first chord alone is fine
    assert win.save_current()
    ed.hotkey_edit.setText("ctrl+k ctrl+alt+f12")  # contains the stop hotkey chord
    assert not win.save_current()
    assert "reserved" in no_modal_boxes[-1][1][2]


def test_stop_hotkey_cannot_be_a_chord_of_a_macro_sequence(win, db, monkeypatch, no_modal_boxes):
    db.save_macro(typing_macro(hotkey="ctrl+k ctrl+u"))
    win._reload_macros()
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("ctrl+u", True))
    win._change_stop_hotkey()
    assert no_modal_boxes[-1][0] == "warning" and "ctrl+k ctrl+u" in no_modal_boxes[-1][1][2]
    assert win.service.stop_hotkey == "ctrl+alt+f12"
