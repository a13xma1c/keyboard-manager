"""End-to-end: the real window, service, scheduler, executor and database together;
only the OS boundary (input injection, hotkey hooks, clock) is faked."""

from datetime import datetime

import pytest

from keyboardmanager.core.database import Database
from keyboardmanager.core.schedule import Schedule
from keyboardmanager.core.service import MacroService
from keyboardmanager.gui.mainwindow import MainWindow
from tests_support import FakeHotkeys, wait_until


@pytest.fixture
def app(qapp, tmp_path, backend):
    db = Database(tmp_path / "km.db")
    hk = FakeHotkeys()
    now = [datetime(2030, 1, 2, 8, 0).timestamp()]
    service = MacroService(db, backend, hk, clock=lambda: now[0], scheduler_thread=False)
    win = MainWindow(db, service)

    def pump(predicate):
        return wait_until(lambda: (qapp.processEvents(), predicate())[1])

    yield win, service, hk, now, pump
    win._quitting = True
    win.close()
    service.shutdown()
    db.close()


def build_in_editor(win, name, hotkey="", text="", steps=()):
    win._new_macro()
    ed = win.editor
    ed.name_edit.setText(name)
    ed.hotkey_edit.setText(hotkey)
    if text:
        ed.add_step("type_text", edit=False).params["text"] = text
    for step_type, params in steps:
        ed.tree.clearSelection()
        ed.add_step(step_type, edit=False).params.update(params)
    assert win.save_current()
    return win._macros[win._current_id]


def test_hotkey_macro_from_editor_to_history(app, backend):
    win, service, hk, _, pump = app
    build_in_editor(win, "Sign off", "ctrl+alt+s", text="Best regards")
    hk.mapping["ctrl+alt+s"]()  # the user presses the hotkey
    assert pump(lambda: service.running_macro is None and ("type", "Best regards") in backend.calls)
    win.tabs.setCurrentWidget(win.history)
    assert pump(lambda: win.history.table.rowCount() == 1)
    row = [win.history.table.item(0, c).text() for c in (1, 2, 3)]
    assert row == ["Sign off", "hotkey ctrl+alt+s", "success"]


def test_pause_hotkey_and_stop_hotkey_while_running(app, backend):
    win, service, hk, _, pump = app
    build_in_editor(win, "Slow", "ctrl+alt+w", steps=[
        ("key_down", {"key": "shift"}), ("delay", {"ms": 10_000}), ("key_press", {"keys": "x"})])
    hk.mapping["ctrl+alt+w"]()
    assert pump(lambda: ("down", "shift") in backend.calls and win.pause_action.isEnabled())
    hk.mapping[service.pause_hotkey]()
    assert pump(lambda: win.pause_action.text() == "Resume" and ("up", "shift") in backend.calls)
    hk.mapping[service.pause_hotkey]()
    assert pump(lambda: win.pause_action.text() == "Pause" and backend.calls.count(("down", "shift")) == 2)
    hk.mapping[service.stop_hotkey]()
    assert pump(lambda: service.running_macro is None and not win.stop_action.isEnabled())
    assert ("down", "x") not in backend.calls and backend.calls[-1] == ("up", "shift")
    assert service.db.list_history()[0].status == "stopped"


def test_scheduled_macro_runs_and_updates_the_window(app, backend):
    win, service, _, now, pump = app
    build_in_editor(win, "Morning", text="good morning")
    win.editor.schedule_edit.set_schedule(Schedule("weekly", time="09:00"))
    assert win.save_current()
    assert win.editor.schedule_status.text() == "Next run: today 09:00"
    now[0] = datetime(2030, 1, 2, 9, 0).timestamp()
    service.scheduler.tick()
    assert pump(lambda: ("type", "good morning") in backend.calls and service.running_macro is None)
    assert pump(lambda: win.editor.schedule_status.text() == "Next run: tomorrow 09:00")
    assert service.db.list_history()[0].trigger == "schedule: Daily at 09:00"


def test_data_survives_a_restart(app, tmp_path, backend):
    win, service, _, _, _ = app
    build_in_editor(win, "Keep me", "ctrl+alt+k", text="persisted")
    service.set_stop_hotkey("ctrl+shift+f12")
    service.db.close()
    db = Database(tmp_path / "km.db")
    again = MacroService(db, backend, FakeHotkeys(), scheduler_thread=False)
    again.reload()
    assert [m.name for m in db.list_macros()] == ["Keep me"]
    assert set(again.hotkeys.mapping) >= {"ctrl+alt+k", "ctrl+shift+f12"}
    service.db = db  # let the fixture close the reopened connection
