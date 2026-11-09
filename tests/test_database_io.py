import json

import pytest

from keyboardmanager.core import actions as catalog
from keyboardmanager.core import io as macro_io
from keyboardmanager.core.models import Macro


def _macro(name="m", hotkey="", **kw):
    return Macro(name=name, hotkey=hotkey, actions=[catalog.new_action("key_press")], **kw)


def test_save_load_update_delete(db):
    m = db.save_macro(_macro("one", "ctrl+1", description="d", app_filter="code"))
    assert m.id is not None
    loaded = db.get_macro(m.id)
    assert (loaded.name, loaded.hotkey, loaded.app_filter) == ("one", "ctrl+1", "code")
    assert loaded.actions[0].type == "key_press"
    loaded.name = "renamed"
    db.save_macro(loaded)
    assert [x.name for x in db.list_macros()] == ["renamed"]
    db.delete_macro(m.id)
    assert db.list_macros() == []


def test_unique_name(db):
    db.save_macro(_macro("a"))
    assert db.unique_name("A") == "A (2)"
    db.save_macro(_macro("a (2)"))
    assert db.unique_name("a") == "a (3)"
    assert db.unique_name("fresh") == "fresh"


def test_history_lifecycle(db):
    m = db.save_macro(_macro("a"))
    h1 = db.start_history(m, "manual")
    db.finish_history(h1, "success", 12, 3)
    h2 = db.start_history(m, "hotkey ctrl+1")
    db.finish_history(h2, "error", 5, 1, "boom")
    h3 = db.start_history(m, "manual")  # never finished
    entries = db.list_history()
    assert [e.id for e in entries] == [h3, h2, h1]
    assert db.list_history(status="error")[0].message == "boom"
    db.mark_interrupted()
    assert db.list_history(limit=1)[0].status == "error"
    db.clear_history()
    assert db.list_history() == []


def test_settings(db):
    assert db.get_setting("k", "d") == "d"
    db.set_setting("k", "1")
    db.set_setting("k", "2")
    assert db.get_setting("k") == "2"


def test_persists_on_disk(tmp_path):
    from keyboardmanager.core.database import Database

    path = tmp_path / "sub" / "x.db"
    d = Database(path)
    d.save_macro(_macro("disk"))
    d.close()
    d2 = Database(path)
    assert d2.list_macros()[0].name == "disk"
    d2.close()


def test_export_import_roundtrip(db):
    original = [_macro("a", "ctrl+1"), _macro("b", "ctrl+2", app_filter="code")]
    text = json.dumps(macro_io.export_to_dict(original))
    result = macro_io.import_from_text(text, db)
    assert len(result.imported) == 2 and not result.skipped
    assert [m.to_dict() for m in db.list_macros()] == [m.to_dict() for m in original]


def test_import_conflicts_and_invalid_entries(db):
    db.save_macro(_macro("a", "ctrl+1"))
    doc = macro_io.export_to_dict([_macro("a", "ctrl+1"), _macro("scoped", "ctrl+1", app_filter="code")])
    doc["macros"].append({"name": "bad", "actions": [{"type": "key_press", "params": {"keys": "ctrl+bogus"}}]})
    doc["macros"].append("not an object")
    result = macro_io.import_from_text(json.dumps(doc), db)
    assert len(result.imported) == 2
    assert result.renamed == 1 and result.hotkeys_cleared == 1
    assert len(result.skipped) == 2
    by_name = {m.name: m for m in db.list_macros()}
    assert by_name["a (2)"].hotkey == ""          # global clash cleared
    assert by_name["scoped"].hotkey == "ctrl+1"   # scoped macro may share a global hotkey


def test_import_rejects_foreign_files(db):
    for text in ("not json", "[]", '{"format": "other"}', '{"format": "keyboardmanager-macros", "version": 99, "macros": []}'):
        with pytest.raises(macro_io.ImportError_):
            macro_io.import_from_text(text, db)


def test_import_single_bare_macro(db):
    text = json.dumps(_macro("solo").to_dict())
    assert len(macro_io.import_from_text(text, db).imported) == 1
