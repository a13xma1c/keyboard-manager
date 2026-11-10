"""Recording refinements: pausing, ignoring our own input, what to record, clean-ups."""

import pytest
from pynput.keyboard import Key, KeyCode
from pynput.mouse import Button
from PySide6.QtCore import Qt

from keyboardmanager.core import recorder as recorder_mod
from keyboardmanager.core.actions import describe
from keyboardmanager.core.models import Action
from keyboardmanager.core.recorder import RecordOptions, Recorder, RefineOptions, build_actions, refine


def text(t):
    return Action("type_text", {"text": t, "interval_ms": 0})


def pause(ms):
    return Action("delay", {"ms": ms, "jitter_ms": 0})


ENTER = Action("key_press", {"keys": "enter"})
MOVE = Action("mouse_move", {"x": 1, "y": 1, "relative": False})
CLICK = Action("mouse_click", {"button": "left", "state": "click", "count": 1, "move_to": True, "x": 1, "y": 1})


def shown(actions):
    return [describe(a) for a in actions]


# ------------------------------------------------------------------ refine
def test_refine_shortens_or_removes_pauses_and_moves():
    steps = [text("a"), pause(5000), MOVE, pause(200), CLICK]
    assert shown(refine(steps, RefineOptions(max_pause_ms=1000))) == [
        'Type "a"', "Wait 1000 ms", "Move mouse to (1, 1)", "Wait 200 ms", "Left click at (1, 1)"]
    assert shown(refine(steps, RefineOptions(remove_pauses=True, remove_moves=True))) == [
        'Type "a"', "Left click at (1, 1)"]
    assert steps[1].params["ms"] == 5000  # the recording itself is untouched


def test_join_lines_across_the_pauses_of_typing():
    steps = [text("Dear Ada,"), pause(300), ENTER, pause(150), text("thanks!"), ENTER, pause(2000), CLICK]
    assert shown(refine(steps, RefineOptions(join_lines=True))) == [
        'Type "Dear Ada, \u23ce thanks! \u23ce "', "Wait 2000 ms", "Left click at (1, 1)"]
    joined = refine(steps, RefineOptions(join_lines=True))[0]
    assert joined.params["text"] == "Dear Ada,\nthanks!\n"


def test_join_lines_leaves_a_lone_enter_and_other_keys_alone():
    steps = [ENTER, text("x"), Action("key_press", {"keys": "tab"}), text("y")]
    assert shown(refine(steps, RefineOptions(join_lines=True))) == ["Press enter", 'Type "x"', "Press tab", 'Type "y"']


# --------------------------------------------------------------- recorder
@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(recorder_mod.time, "monotonic", lambda: now[0])
    return now


def started(**options):
    rec = Recorder(RecordOptions(min_delay_ms=50, **options))
    rec._active = True
    return rec


def tap(rec, key, injected=False):
    rec._press(key, injected)
    rec._release(key, injected)


def test_paused_time_is_not_recorded(clock):
    rec = started()
    changes = []
    rec.on_pause_changed = changes.append
    tap(rec, Key.enter)
    clock[0] += 1.0
    tap(rec, Key.f8)   # pause
    clock[0] += 60     # a minute away...
    tap(rec, KeyCode.from_char("x"))  # ...typing while paused is not recorded
    tap(rec, Key.f8)   # resume
    clock[0] += 0.5
    tap(rec, Key.tab)
    assert changes == [True, False]
    assert shown(rec.stop()) == ["Press enter", "Wait 1500 ms", "Press tab"]  # not 61.5 s


def test_own_input_is_not_recorded(clock):
    rec = started()
    tap(rec, KeyCode.from_char("a"), injected=True)  # e.g. a snippet being typed
    rec._click(5, 5, Button.left, True, True)
    rec._click(5, 5, Button.left, False, True)
    rec._scroll(0, 0, 0, -1, True)
    rec._move(9, 9, True)
    tap(rec, KeyCode.from_char("b"))
    assert shown(rec.stop()) == ['Type "b"']


def test_choose_what_to_record(clock):
    rec = started(record_keyboard=False)
    tap(rec, KeyCode.from_char("a"))
    rec._click(5, 5, Button.left, True)
    rec._click(5, 5, Button.left, False)
    assert shown(rec.stop()) == ["Left click at (5, 5)"]
    rec = started(record_clicks=False)
    tap(rec, KeyCode.from_char("a"))
    rec._click(5, 5, Button.left, True)
    rec._click(5, 5, Button.left, False)
    rec._scroll(0, 0, 0, -1)
    assert shown(rec.stop()) == ['Type "a"']


def test_stop_still_works_while_paused(clock):
    stops = []
    rec = Recorder(RecordOptions(), on_stopped=lambda: stops.append(1))
    rec._active = True
    tap(rec, Key.f8)
    tap(rec, Key.f9)
    assert stops == [1] and rec.paused


def test_toggle_pause_with_real_listeners_signature():
    rec = started()
    rec._press(Key.f8)  # one-argument call (older pynput) still works
    assert rec.paused


def test_options_dialog(qapp):
    from keyboardmanager.gui.recording import RecordOptionsDialog

    dlg = RecordOptionsDialog()
    dlg.keyboard.setChecked(False)
    dlg.pause_key.setText("F7")
    o = dlg.options()
    assert (o.pause_key, o.record_keyboard, o.record_clicks) == ("f7", False, True)
    dlg.pause_key.setText("f9")  # the stop key cannot also pause
    assert dlg.options().pause_key == ""


# ------------------------------------------------------------ review dialog
def test_review_dialog(qapp):
    from keyboardmanager.gui.recording import RecordReviewDialog

    ev = [(0, "key_down", ("h", "H")), (0.02, "key_up", "h"), (0.5, "key_down", "enter"), (0.51, "key_up", "enter"),
          (0.7, "key_down", ("i", "i")), (0.71, "key_up", "i"), (4.0, "click", (3, 3, "left", True)),
          (4.05, "click", (3, 3, "left", False))]
    dlg = RecordReviewDialog(build_actions(ev))
    assert dlg.list.count() == 7 and "7 of 7 steps" in dlg.summary.text()
    dlg.join.setChecked(True)
    assert [dlg.list.item(i).text() for i in range(dlg.list.count())] == [
        'Type "H \u23ce i"', "Wait 3300 ms", "Left click at (3, 3)"]
    dlg.max_pause.setValue(500)
    assert dlg.list.item(1).text() == "Wait 500 ms" and "about 0.5 s of pauses" in dlg.summary.text()
    dlg.no_pauses.setChecked(True)
    assert not dlg.max_pause.isEnabled() and dlg.list.count() == 2
    dlg.list.item(1).setCheckState(Qt.CheckState.Unchecked)  # drop the click
    assert [a.type for a in dlg.actions()] == ["type_text"] and "1 of 2 steps" in dlg.summary.text()
    dlg.list.item(0).setCheckState(Qt.CheckState.Unchecked)
    assert not dlg.add_btn.isEnabled()


def test_editor_adds_only_the_reviewed_steps(qapp, monkeypatch):
    from keyboardmanager.core.models import Macro
    from keyboardmanager.gui.editor import MacroEditor
    from keyboardmanager.gui.recording import RecordReviewDialog

    ed = MacroEditor()
    ed.load(Macro("m"))

    def review(dialog):
        dialog.no_pauses.setChecked(True)
        return RecordReviewDialog.DialogCode.Accepted

    monkeypatch.setattr(RecordReviewDialog, "exec", review)
    ed._recorded([text("hi"), pause(900), ENTER])
    assert shown(ed.actions) == ['Type "hi"', "Press enter"] and ed.undo_stack.canUndo()
    monkeypatch.setattr(RecordReviewDialog, "exec", lambda d: RecordReviewDialog.DialogCode.Rejected)
    ed._recorded([text("discarded")])
    assert len(ed.actions) == 2
