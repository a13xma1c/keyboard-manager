"""Action dialog, recording UI, key capture and the history view."""

import pytest
from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from keyboardmanager.core import actions as catalog
from keyboardmanager.core.models import Action, Macro
from keyboardmanager.gui import recording as recording_mod
from keyboardmanager.gui.history import HistoryView, format_duration, format_time
from keyboardmanager.gui.widgets import ActionDialog, KeyCaptureEdit


# --------------------------------------------------------------- key capture
@pytest.fixture
def pc_keyboard(monkeypatch):
    """These tests use the Windows / Linux modifier mapping on every OS (macOS maps
    Qt's Control to Command; that is covered in test_macos.py)."""
    from keyboardmanager.gui import widgets

    monkeypatch.setattr(widgets, "_IS_MAC", False)


def test_capture_hotkey_with_modifiers(qapp, pc_keyboard):
    w = KeyCaptureEdit("hotkey")
    w.show()
    w.button.setChecked(True)
    assert w.button.text() == "Press keys..."
    QTest.keyPress(w.button, Qt.Key.Key_Control, Qt.KeyboardModifier.ControlModifier)
    assert w.button.text() == "ctrl+..."
    QTest.keyClick(w.button, Qt.Key.Key_F5, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
    QTest.keyRelease(w.button, Qt.Key.Key_Control)  # or Qt believes Ctrl stays held for later tests
    assert w.text() == "ctrl+shift+f5" and w.button.isChecked()  # a sequence may continue
    w.button.setChecked(False)  # click again to finish
    assert w.text() == "ctrl+shift+f5"


def test_capture_single_key_and_lone_modifier(qapp, pc_keyboard):
    w = KeyCaptureEdit("key")
    w.show()
    w.button.setChecked(True)
    QTest.keyClick(w.button, Qt.Key.Key_Escape)
    assert w.text() == "esc"
    w.button.setChecked(True)
    QTest.keyPress(w.button, Qt.Key.Key_Shift, Qt.KeyboardModifier.ShiftModifier)
    QTest.keyRelease(w.button, Qt.Key.Key_Shift)
    assert w.text() == "shift"


def test_capture_ignores_unknown_keys_and_cancels_on_focus_loss(qapp):
    w = KeyCaptureEdit("hotkey")
    w.show()
    w.button.setChecked(True)
    QTest.keyClick(w.button, Qt.Key.Key_VolumeUp)  # no canonical name
    assert w.button.isChecked() and w.text() == ""
    QApplication.sendEvent(w.button, __import__("PySide6.QtGui", fromlist=["QFocusEvent"]).QFocusEvent(
        __import__("PySide6.QtCore", fromlist=["QEvent"]).QEvent.Type.FocusOut))
    assert not w.button.isChecked()


def test_combo_and_key_modes_normalize(qapp):
    w = KeyCaptureEdit("combo")
    w.setText("Shift+Ctrl+Tab")
    assert w.normalized() == "ctrl+shift+tab"
    k = KeyCaptureEdit("key")
    k.setText("Return")
    assert k.normalized() == "enter"


# ------------------------------------------------------------- action dialog
@pytest.mark.parametrize("action_type", sorted(catalog.SPECS))
def test_action_dialog_accepts_defaults_or_explains(qapp, action_type, no_modal_boxes):
    action = catalog.new_action(action_type)
    dlg = ActionDialog(action)
    dlg._accept()
    needs_input = ("type_text", "if", "while", "paste_text", "run_macro", "wait_for_window", "launch",
                   "wait_for_image", "click_image", "focus_window")
    if action_type in needs_input:  # need input from the user
        assert no_modal_boxes and dlg.result() != dlg.DialogCode.Accepted
    else:
        assert not no_modal_boxes and dlg.result() == dlg.DialogCode.Accepted


def test_action_dialog_normalizes_keys(qapp):
    action = catalog.new_action("key_press")
    dlg = ActionDialog(action)
    dlg._widgets["keys"].setText("Shift+Ctrl+T")
    dlg._accept()
    assert action.params["keys"] == "ctrl+shift+t"


def test_action_dialog_pick_position(qapp, monkeypatch):
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QCursor

    from keyboardmanager.gui import widgets

    monkeypatch.setattr(QTimer, "singleShot", staticmethod(lambda ms, fn: fn()))
    monkeypatch.setattr(QCursor, "pos", staticmethod(lambda: QPoint(321, 123)))
    dlg = ActionDialog(catalog.new_action("mouse_move"))
    dlg._pick.click()
    assert (dlg._widgets["x"].value(), dlg._widgets["y"].value()) == (321, 123)
    assert dlg._pick.isEnabled() and isinstance(widgets, object)


def test_action_dialog_float_and_choice_fields(qapp):
    for t, spec in catalog.SPECS.items():
        kinds = {f.kind for f in spec.fields}
        if kinds & {"float", "choice"}:
            dlg = ActionDialog(catalog.new_action(t))
            assert set(dlg.values()) == {f.name for f in spec.fields}


# ----------------------------------------------------------------- recording
def test_record_options_dialog(qapp):
    dlg = recording_mod.RecordOptionsDialog()
    dlg.moves.setChecked(True)
    dlg.delays.setChecked(False)
    dlg.min_delay.setValue(120)
    dlg.stop_key.setText("F10")
    o = dlg.options()
    assert (o.record_moves, o.record_delays, o.min_delay_ms, o.stop_key) == (True, False, 120, "f10")
    dlg.stop_key.setText("not a key")
    assert dlg.options().stop_key == "f9"


class _FakeRecorder:
    def __init__(self, options, on_stopped=None, on_event=None):
        self.on_stopped, self.on_event = on_stopped, on_event
        self.started = False
        self.fail = False
        self.paused = False
        self.on_pause_changed = None

    def start(self):
        if self.fail:
            raise OSError("not trusted")
        self.started = True

    def stop(self):
        return [Action("type_text", {"text": "rec"})]


@pytest.fixture
def session(qapp, monkeypatch):
    monkeypatch.setattr(recording_mod, "Recorder", _FakeRecorder)
    monkeypatch.setattr(QTimer, "singleShot", staticmethod(lambda ms, fn: fn()))
    from keyboardmanager.core.recorder import RecordOptions

    s = recording_mod.RecordingSession(RecordOptions(stop_key="f9"))
    results = []
    s.finished.connect(results.append)
    return s, results


def test_recording_session_hides_windows_and_restores_them(session):
    s, results = session
    window = QWidget()
    window.show()
    s.start()
    assert not window.isVisible() and s._recorder.started
    s._recorder.on_event(3)
    assert "3 events" in s._overlay.text() and "F9 stops" in s._overlay.text() and "F8 pauses" in s._overlay.text()
    s._recorder.paused = True
    s._recorder.on_pause_changed(True)
    assert s._overlay.text() == "Paused (3 events) - F8 resumes, F9 stops"
    s._recorder.on_stopped()
    assert window.isVisible() and not s._overlay.isVisible()
    assert [a.params["text"] for a in results[0]] == ["rec"]
    s._recorder.on_stopped()  # a second stop is ignored
    assert len(results) == 1
    window.close()


def test_recording_session_start_failure_restores_windows(session):
    s, results = session
    window = QWidget()
    window.show()
    s._recorder.fail = True
    with pytest.raises(OSError):
        s.start()
    assert window.isVisible() and results == [[]]
    window.close()


# ------------------------------------------------------------------ history
def test_history_view_filters_and_clears(qapp, db, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    m = db.save_macro(Macro("m"))
    for status in ("success", "error", "success"):
        h = db.start_history(m, "manual")
        db.finish_history(h, status, 1500, 2, "boom" if status == "error" else "")
    db.add_skipped(m, "schedule: Daily at 09:00", "Skipped: missed")
    view = HistoryView(db)
    assert view.table.rowCount() == 4 and "2 successful" in view.summary.text()
    view.status_filter.setCurrentText("skipped")
    assert view.table.rowCount() == 1 and view.table.item(0, 6).text() == "Skipped: missed"
    view.status_filter.setCurrentText("error")
    assert view.table.item(0, 4).text() == "1.5 s"
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    view._clear()
    assert db.list_history()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    view._clear()
    assert db.list_history() == [] and view.table.rowCount() == 0


def test_history_formatting():
    assert format_duration(999) == "999 ms" and format_duration(61_000) == "61.0 s"
    assert format_time("garbage") == "garbage"
    assert format_time("2030-01-02T03:04:05+00:00").startswith("2030-01-0")


def test_capture_a_hotkey_sequence_chord_by_chord(qapp, pc_keyboard):
    from keyboardmanager.gui.widgets import KeyCaptureEdit

    w = KeyCaptureEdit("hotkey")
    w.show()
    w.button.setChecked(True)
    QTest.keyClick(w.button, Qt.Key.Key_K, Qt.KeyboardModifier.ControlModifier)
    assert w.button.isChecked() and "next chord" in w.button.text()
    QTest.keyClick(w.button, Qt.Key.Key_S, Qt.KeyboardModifier.ControlModifier)
    w.button.setChecked(False)
    assert w.text() == "ctrl+k ctrl+s" and w.normalized() == "ctrl+k ctrl+s"
    # capturing again starts over instead of appending
    w.button.setChecked(True)
    QTest.keyClick(w.button, Qt.Key.Key_F2)
    w.button.setChecked(False)
    assert w.text() == "f2"


def test_capture_stops_at_the_sequence_limit(qapp, pc_keyboard):
    from keyboardmanager.core import keys
    from keyboardmanager.gui.widgets import KeyCaptureEdit

    w = KeyCaptureEdit("hotkey")
    w.show()
    w.button.setChecked(True)
    for key in (Qt.Key.Key_A, Qt.Key.Key_B, Qt.Key.Key_C, Qt.Key.Key_D):
        QTest.keyClick(w.button, key, Qt.KeyboardModifier.ControlModifier)
    assert not w.button.isChecked()  # finished by itself at the limit
    assert w.text() == "ctrl+a ctrl+b ctrl+c" and len(w.text().split()) == keys.MAX_SEQUENCE_CHORDS
