"""Folders, tags, favorites and speed: rules, storage, migration, import / export."""

import json
import sqlite3

import pytest

from keyboardmanager.core import io as macro_io
from keyboardmanager.core.database import Database
from keyboardmanager.core.models import Macro
from keyboardmanager.core.organize import (
    FolderStore,
    ancestors,
    is_inside,
    moved_path,
    normalize_folder,
    normalize_tags,
    parent_folder,
    validate_speed,
)


@pytest.mark.parametrize("text,expected", [
    (" Work / Email/ ", "Work/Email"), ("", ""), ("//a//b/", "a/b"), ("a\\b", "a/b"), ("Ünï", "Ünï")])
def test_normalize_folder(text, expected):
    assert normalize_folder(text) == expected


def test_folder_paths():
    assert ancestors("a/b/c") == ["a", "a/b", "a/b/c"] and ancestors("") == []
    assert parent_folder("a/b") == "a" and parent_folder("a") == ""
    assert is_inside("a/b", "a") and is_inside("a", "a") and not is_inside("ab", "a")
    assert moved_path("a/b", "a", "x/y") == "x/y/b" and moved_path("ab", "a", "x") == "ab"
    with pytest.raises(ValueError):
        normalize_folder("x" * 61)


def test_folder_store(db):
    store = FolderStore(load=lambda: db.get_setting("f", ""), save=lambda t: db.set_setting("f", t))
    store.add("Work/Email")
    store.add("Archive")
    assert store.all() == ["Archive", "Work", "Work/Email"]  # parents included
    assert store.all(["Games/Old"]) == ["Archive", "Games", "Games/Old", "Work", "Work/Email"]
    store.rename("Work", "Job")
    assert store.all() == ["Archive", "Job", "Job/Email"]
    store.remove("Job")
    assert store.all() == ["Archive"]
    with pytest.raises(ValueError):
        store.add(" / ")
    db.set_setting("f", "{garbage")
    assert store.all() == []


@pytest.mark.parametrize("tags,expected", [
    ("Email, #Work, email,  big   deal ", ["email", "work", "big deal"]),
    (["A", "#a", "b"], ["a", "b"]), ("", []), (" , ,", []),
])
def test_normalize_tags(tags, expected):
    assert normalize_tags(tags) == expected


def test_invalid_tags_and_speed():
    with pytest.raises(ValueError):
        normalize_tags(["x" * 33])
    with pytest.raises(ValueError):
        normalize_tags([1])
    for bad in (0, 0.05, 11, "fast", True):
        with pytest.raises(ValueError):
            validate_speed(bad)
    assert validate_speed(2) == 2.0


def test_new_fields_round_trip(db):
    m = Macro("m", folder="Work/Email", tags=["email", "work"], favorite=True, speed=2.5)
    data = m.to_dict()
    assert (data["folder"], data["tags"], data["favorite"], data["speed"]) == ("Work/Email", ["email", "work"], True, 2.5)
    assert Macro.from_dict(data).to_dict() == data
    assert not {"folder", "tags", "favorite", "speed"} & set(Macro("plain").to_dict())  # unchanged format
    saved = db.get_macro(db.save_macro(m).id)
    assert (saved.folder, saved.tags, saved.favorite, saved.speed) == ("Work/Email", ["email", "work"], True, 2.5)


@pytest.mark.parametrize("field,value", [
    ("tags", "not a list"), ("favorite", "yes"), ("speed", 50), ("folder", 3), ("tags", ["x" * 40])])
def test_invalid_fields_are_refused(field, value):
    with pytest.raises(ValueError):
        Macro.from_dict({"name": "m", field: value})


def test_migration_from_the_previous_version(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE macros (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', hotkey TEXT NOT NULL DEFAULT '',
            app_filter TEXT NOT NULL DEFAULT '', window_filter TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 1, actions TEXT NOT NULL DEFAULT '[]',
            schedule TEXT NOT NULL DEFAULT '', event TEXT NOT NULL DEFAULT '',
            abbreviation TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        INSERT INTO macros (name, created_at, updated_at) VALUES ('older', 'x', 'x');
    """)
    conn.commit()
    conn.close()
    db = Database(path)
    m = db.list_macros()[0]
    assert (m.folder, m.tags, m.favorite, m.speed) == ("", [], False, 1.0)
    m.folder, m.tags, m.favorite, m.speed = "A", ["t"], True, 0.5
    db.save_macro(m)
    db.close()
    again = Database(path).list_macros()[0]
    assert (again.folder, again.tags, again.favorite, again.speed) == ("A", ["t"], True, 0.5)


def test_database_tolerates_odd_stored_values(db):
    m = db.save_macro(Macro("m"))
    db._conn.execute("UPDATE macros SET tags='{bad', speed=99 WHERE id=?", (m.id,))
    loaded = db.get_macro(m.id)
    assert loaded.tags == [] and loaded.speed == 1.0


def test_export_and_import_keep_the_organisation(db):
    doc = macro_io.export_to_dict([Macro("m", folder="Work", tags=["a"], favorite=True, speed=3.0)])
    result = macro_io.import_from_text(json.dumps(doc), db)
    imported = result.imported[0]
    assert (imported.folder, imported.tags, imported.favorite, imported.speed) == ("Work", ["a"], True, 3.0)
