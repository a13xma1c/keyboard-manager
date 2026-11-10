"""Import / export of macro configurations as JSON."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from keyboardmanager.core import actions as action_catalog
from keyboardmanager.core import keys
from keyboardmanager.core.database import Database
from keyboardmanager.core.expander import abbreviations_conflict
from keyboardmanager.core.models import Macro

FORMAT_NAME = "keyboardmanager-macros"
FORMAT_VERSION = 1


class ImportError_(ValueError):
    """Raised when a file is not a valid macro configuration."""


@dataclass
class ImportResult:
    imported: list[Macro] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # human readable reasons
    renamed: int = 0
    hotkeys_cleared: int = 0
    abbreviations_cleared: int = 0
    scheduled: int = 0  # imported macros that will run by themselves (schedule or event)


def export_to_dict(macros: list[Macro]) -> dict:
    return {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "macros": [m.to_dict() for m in macros],
    }


def export_to_file(macros: list[Macro], path: str | Path) -> None:
    Path(path).write_text(json.dumps(export_to_dict(macros), indent=2, ensure_ascii=False), encoding="utf-8")


def parse_document(text: str) -> list[dict]:
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ImportError_(f"Not valid JSON: {exc}") from exc
    if isinstance(doc, dict) and doc.get("format") == FORMAT_NAME:
        if doc.get("version", 0) > FORMAT_VERSION:
            raise ImportError_("File was created by a newer version of Keyboard Manager")
        macros = doc.get("macros")
        if not isinstance(macros, list):
            raise ImportError_("'macros' must be a list")
        return macros
    if isinstance(doc, dict) and "name" in doc and "actions" in doc:  # a single bare macro
        return [doc]
    raise ImportError_("This file is not a Keyboard Manager macro configuration")


def import_from_text(text: str, db: Database) -> ImportResult:
    """Validate and store every macro in ``text``.

    Invalid macros are skipped (and reported) instead of aborting the whole import.
    Name clashes are resolved by renaming; a hotkey already used by an existing macro
    with an overlapping scope is cleared so imports never silently steal shortcuts.
    """
    result = ImportResult()
    existing = db.list_macros()
    for index, raw in enumerate(parse_document(text), 1):
        label = raw.get("name", f"#{index}") if isinstance(raw, dict) else f"#{index}"
        try:
            macro = Macro.from_dict(raw)
            macro.name = macro.name.strip()
            if not macro.name:
                raise ValueError("name is empty")
            problems = action_catalog.validate_actions(macro.actions)
            if problems:
                raise ValueError(problems[0])
            if macro.hotkey.strip():
                macro.hotkey = keys.normalize_hotkey_sequence(macro.hotkey)
            else:
                macro.hotkey = ""
        except ValueError as exc:
            result.skipped.append(f"{label}: {exc}")
            continue

        unique = db.unique_name(macro.name)
        if unique != macro.name:
            result.renamed += 1
            macro.name = unique
        if macro.hotkey and any(
            o.hotkey and keys.sequence_prefix_conflict(o.hotkey, macro.hotkey) and scopes_conflict(o, macro)
            for o in existing
        ):
            macro.hotkey = ""
            result.hotkeys_cleared += 1
        if macro.abbreviation and any(
            o.abbreviation and abbreviations_conflict(o.abbreviation, macro.abbreviation)
            and scopes_conflict(o, macro)
            for o in existing
        ):
            macro.abbreviation = ""
            result.abbreviations_cleared += 1
        db.save_macro(macro)
        existing.append(macro)
        result.imported.append(macro)
        if (macro.schedule.active or macro.event) and macro.enabled:
            result.scheduled += 1
    return result


def import_from_file(path: str | Path, db: Database) -> ImportResult:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ImportError_(f"Cannot read file: {exc}") from exc
    return import_from_text(text, db)


def scopes_conflict(a: Macro, b: Macro) -> bool:
    """Would ``a`` and ``b`` compete for the same hotkey press?

    A global macro and an application-specific one may share a hotkey (the specific
    one wins while its application is focused), as may macros on different keymap
    layers (the active layer's macro wins) or restricted to profiles that are never
    active together. Two global macros, or two with identical filters, may not.
    """
    if a.layer.strip().lower() != b.layer.strip().lower():
        return False
    if a.profiles and b.profiles and not (
        {p.lower() for p in a.profiles} & {p.lower() for p in b.profiles}
    ):
        return False  # under "All profiles" both are active: the first by name wins
    return (a.app_filter.strip().lower(), a.window_filter.strip().lower()) == (
        b.app_filter.strip().lower(),
        b.window_filter.strip().lower(),
    )
