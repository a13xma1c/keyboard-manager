"""Remapping tab and text snippets in the main window."""

import pytest
from PySide6.QtWidgets import QDialog

from keyboardmanager.core.models import Macro
from keyboardmanager.core.remap import Remap, UnsupportedRemapper
from keyboardmanager.core.service import MacroService
from keyboardmanager.gui import mainwindow as mainwindow_mod
from keyboardmanager.gui import remapping as remapping_mod
from keyboardmanager.gui.mainwindow import MainWindow
from tests_support import FakeHotkeys


@pytest.fixture
def win(qapp, db, backend):
    service = MacroService(db, backend, FakeHotkeys(), scheduler_thread=False)
    window = MainWindow(db, service)
    yield window
    window.editor.dirty = False
    window._quitting = True
    window.close()
    service.shutdown()


def answer_dialog(monkeypatch, cls, result, fill):
    def exec_(self):
        fill(self)
        return result

    monkeypatch.setattr(cls, "exec", exec_)


# ------------------------------------------------------------------- remaps
def test_add_and_remove_remaps(win, monkeypatch, no_modal_boxes):
    view = win.remap_view
    assert "No keys are remapped" in view.status.text() and not view.remove_btn.isEnabled()

    def fill(source, target):
        return lambda d: (d.source.setText(source), d.target.setText(target))

    answer_dialog(monkeypatch, remapping_mod.RemapDialog, QDialog.DialogCode.Accepted, fill("CapsLock", "Escape"))
    view._add()
    answer_dialog(monkeypatch, remapping_mod.RemapDialog, QDialog.DialogCode.Accepted, fill("f13", "cmd"))
    view._add()
    assert [(view.table.item(r, 0).text(), view.table.item(r, 1).text()) for r in range(2)] == [
        ("caps_lock", "esc"), ("f13", "cmd")]
    assert "2 remap(s) active" in view.status.text()
    assert win.service.remapper.applied[-1] == [Remap("caps_lock", "esc"), Remap("f13", "cmd")]

    answer_dialog(monkeypatch, remapping_mod.RemapDialog, QDialog.DialogCode.Accepted, fill("esc", "caps_lock"))
    view._add()  # esc is already a target: chains / swaps are refused
    assert no_modal_boxes[-1][0] == "warning" and view.table.rowCount() == 2
    answer_dialog(monkeypatch, remapping_mod.RemapDialog, QDialog.DialogCode.Accepted, fill("nonsense", "a"))
    view._add()
    assert view.table.rowCount() == 2
    answer_dialog(monkeypatch, remapping_mod.RemapDialog, QDialog.DialogCode.Rejected, fill("a", "b"))
    view._add()
    assert view.table.rowCount() == 2

    view._remove()  # nothing selected: nothing removed
    assert view.table.rowCount() == 2
    view.table.selectRow(0)
    view._remove()
    assert win.service.remaps == [Remap("f13", "cmd")]


def test_switching_remapping_off_and_on(win):
    win.service.set_remaps([Remap("caps_lock", "esc")])
    view = win.remap_view
    view.refresh()
    view.enabled.setChecked(False)
    assert "switched off" in view.status.text() and win.service.remapper.cleared
    view.enabled.setChecked(True)
    assert "1 remap(s) active" in view.status.text()


def test_unavailable_remapping_is_explained(qapp, db, backend):
    service = MacroService(db, backend, FakeHotkeys(), scheduler_thread=False,
                           remapper=UnsupportedRemapper("Key remapping on Linux needs root"))
    window = MainWindow(db, service)
    assert window.remap_view.status.text() == "Not available here: Key remapping on Linux needs root."
    window._quitting = True
    window.close()
    service.shutdown()


def test_remaps_are_applied_when_the_window_starts(qapp, db, backend):
    from tests_support import FakeHotkeys as FH

    db.set_setting("key_remaps", '[{"from": "caps_lock", "to": "esc"}]')
    service = MacroService(db, backend, FH(), scheduler_thread=False)
    window = MainWindow(db, service)
    assert service.remapper.applied == [[Remap("caps_lock", "esc")]]
    window._quitting = True
    window.close()
    service.shutdown()


# ----------------------------------------------------------------- snippets
def test_new_snippet_creates_a_text_expansion(win, db, monkeypatch, no_modal_boxes):
    answer_dialog(monkeypatch, mainwindow_mod.SnippetDialog, QDialog.DialogCode.Accepted,
                  lambda d: (d.abbreviation.setText(" ;sig "), d.text.setPlainText("Best regards,\nAlex")))
    win._new_snippet()
    macro = db.list_macros()[0]
    assert (macro.name, macro.abbreviation) == ("Snippet ;sig", ";sig")
    assert macro.actions[0].type == "type_text" and macro.actions[0].params["text"] == "Best regards,\nAlex"
    assert win.service.expander.abbreviations == [";sig"]
    assert win.list.macro_items()[0].text(0) == 'Snippet ;sig  ";sig"'
    assert "1 abbreviations" in win.status_label.text()


def test_snippet_dialog_insists_on_valid_input(win, db, monkeypatch, no_modal_boxes):
    attempts = iter([
        (QDialog.DialogCode.Accepted, "", "text"),        # no abbreviation
        (QDialog.DialogCode.Accepted, ";x", ""),          # no text
        (QDialog.DialogCode.Accepted, "a b", "text"),     # invalid abbreviation
        (QDialog.DialogCode.Rejected, "", ""),            # the user gives up
    ])

    def exec_(self):
        result, abbreviation, text = next(attempts)
        self.abbreviation.setText(abbreviation)
        self.text.setPlainText(text)
        return result

    monkeypatch.setattr(mainwindow_mod.SnippetDialog, "exec", exec_)
    win._new_snippet()
    assert [k for k, _ in no_modal_boxes] == ["warning"] * 3 and db.list_macros() == []


def test_snippet_abbreviation_conflicts(win, db, monkeypatch, no_modal_boxes):
    db.save_macro(Macro("existing", abbreviation=";sig"))
    win._reload_macros()
    attempts = iter([(QDialog.DialogCode.Accepted, ";si"), (QDialog.DialogCode.Rejected, "")])

    def exec_(self):
        result, abbreviation = next(attempts)
        self.abbreviation.setText(abbreviation)
        self.text.setPlainText("x")
        return result

    monkeypatch.setattr(mainwindow_mod.SnippetDialog, "exec", exec_)
    win._new_snippet()
    assert "overlaps" in no_modal_boxes[-1][1][2] and len(db.list_macros()) == 1


# ------------------------------------------------------------------ editor
def test_editor_abbreviation_field(win, db, no_modal_boxes):
    db.save_macro(Macro("other", abbreviation=";adr"))
    win._reload_macros()
    win._new_macro()
    ed = win.editor
    ed.abbreviation_edit.setText("with space")
    assert not win.save_current() and "Invalid abbreviation" in no_modal_boxes[-1][1][2]
    ed.abbreviation_edit.setText(";adr")
    assert not win.save_current() and "already used" in no_modal_boxes[-1][1][2]
    ed.app_edit.setText("mail")  # fine in another scope
    assert win.save_current()
    saved = win._macros[win._current_id]
    assert saved.abbreviation == ";adr"
    win.search.setText(";adr")  # search finds abbreviations too
    assert len(win.list.macro_items()) == 2
    win._duplicate_macro()
    assert win._macros[win._current_id].abbreviation == ""  # a copy must not steal it


def test_import_reports_cleared_abbreviations(win, db, tmp_path, monkeypatch, no_modal_boxes):
    import json

    from PySide6.QtWidgets import QFileDialog

    db.save_macro(Macro("mine", abbreviation=";sig"))
    win._reload_macros()
    file = tmp_path / "in.json"
    file.write_text(json.dumps({"format": "keyboardmanager-macros", "version": 1,
                                "macros": [{"name": "theirs", "actions": [], "abbreviation": ";sig"}]}))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(file), ""))
    win._import()
    assert "1 abbreviation(s) removed" in no_modal_boxes[-1][1][2]
    assert {m.name: m.abbreviation for m in db.list_macros()} == {"mine": ";sig", "theirs": ""}
