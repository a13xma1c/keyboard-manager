"""Sync between your own computers through a shared folder (no Qt here).

Point every computer at the same folder - a Dropbox / iCloud Drive / OneDrive /
Syncthing / network folder - and Keyboard Manager keeps their macros (and optionally
profiles, context rules and trigger rules) the same. Layout inside the folder::

    keyboardmanager-sync/
        macros/<uid>.json     one file per macro (or a tombstone once deleted)
        settings.json         profiles, context rules, trigger rules
        devices/<name>.json   which computer synced when (shown in the Sync tab)

Each file carries a hash of its content. For every macro the engine compares three
versions - this computer's, the folder's, and the *base* (what both agreed on at the
last sync, kept in the database) - so it knows which side changed:

* only this computer changed it  -> it is written to the folder
* only the folder changed it     -> it is copied into this computer
* both changed it differently    -> a conflict: this computer's version wins in the
  folder, the other one is kept as a copy named "... (conflict from <device>)"

Deletions travel as tombstones. A file that is simply *missing* (e.g. a sync client
that has not finished downloading) never deletes anything: the macro is written
again. Files that cannot be read are skipped until the next round. Macros copied
from a team library are not synced (each computer subscribes itself).
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from keyboardmanager.core import actions as action_catalog
from keyboardmanager.core.database import Database
from keyboardmanager.core.models import Macro

SYNC_DIR = "keyboardmanager-sync"
ENTRY_FORMAT = "keyboardmanager-sync"
FORMAT_VERSION = 1
SETTINGS_KEY = "__settings__"

FOLDER_SETTING = "sync_folder"
DEVICE_SETTING = "sync_device"
INTERVAL_SETTING = "sync_interval_min"
SETTINGS_SYNC_SETTING = "sync_settings"  # "1": also profiles, context rules, triggers
LAST_SYNC_SETTING = "sync_last"          # JSON: when and what the last sync did


class SyncError(Exception):
    """The sync could not run at all (e.g. the folder is not there)."""


def canonical(data: Any) -> str:
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def content_hash(data: Any) -> str:
    return hashlib.sha256(canonical(data).encode("utf-8")).hexdigest()[:32]


def macro_hash(macro: Macro) -> str:
    """Identifies a macro's content (not its local id, uid or time stamps)."""
    return content_hash(macro.to_dict())


def default_device_name() -> str:
    return socket.gethostname().split(".")[0] or "this computer"


def write_json_atomic(path: Path, data: Any) -> None:
    """Write via a temporary file and rename, so a sync client never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class SyncReport:
    uploaded: list[str] = field(default_factory=list)
    downloaded: list[str] = field(default_factory=list)
    deleted_here: list[str] = field(default_factory=list)     # deleted on another computer
    deleted_there: list[str] = field(default_factory=list)    # deleted here, tombstone written
    conflicts: list[str] = field(default_factory=list)
    adjusted: list[str] = field(default_factory=list)  # renamed / shortcut dropped: a clash
    errors: list[str] = field(default_factory=list)
    settings: str = ""  # "uploaded", "downloaded", "merged" or ""

    @property
    def changed_here(self) -> bool:
        return bool(self.downloaded or self.deleted_here or self.conflicts or self.settings in (
            "downloaded",))

    def summary(self) -> str:
        parts = [f"{len(v)} {label}" for label, v in (
            ("sent", self.uploaded), ("received", self.downloaded), ("deleted here", self.deleted_here),
            ("deletion(s) sent", self.deleted_there), ("conflict(s)", self.conflicts),
            ("adjusted (name or shortcut clash)", self.adjusted), ("problem(s)", self.errors)) if v]
        if self.settings:
            parts.append(f"settings {self.settings}")
        return ", ".join(parts) if parts else "everything was up to date"

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("uploaded", "downloaded", "deleted_here", "deleted_there",
                                              "conflicts", "adjusted", "errors", "settings")}


class SyncFolder:
    """The files of a sync folder."""

    def __init__(self, folder: str | Path) -> None:
        base = Path(os.path.expanduser(str(folder)))
        if not str(folder).strip() or not base.is_dir():
            raise SyncError(f"the sync folder {folder!s} does not exist (is the drive connected?)")
        self.root = base / SYNC_DIR
        self.macros_dir = self.root / "macros"
        self.devices_dir = self.root / "devices"
        self.macros_dir.mkdir(parents=True, exist_ok=True)

    def entries(self, errors: list[str]) -> tuple[dict[str, dict], set[str]]:
        """({uid: entry}, uids of files that could not be read - reported in ``errors``)."""
        out: dict[str, dict] = {}
        unreadable: set[str] = set()
        for path in sorted(self.macros_dir.glob("*.json")):
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(entry, dict) or entry.get("format") != ENTRY_FORMAT:
                    raise ValueError("not a sync entry")
                if entry.get("version", 0) > FORMAT_VERSION:
                    raise ValueError("written by a newer version of Keyboard Manager")
                if entry.get("uid") != path.stem:
                    raise ValueError("the file name does not match its content")
            except (OSError, ValueError) as exc:
                errors.append(f"{path.name}: {exc}")
                unreadable.add(path.stem)
                continue
            out[path.stem] = entry
        return out, unreadable

    def write_macro(self, macro: Macro, device: str) -> None:
        write_json_atomic(self.macros_dir / f"{macro.uid}.json", {
            "format": ENTRY_FORMAT, "version": FORMAT_VERSION, "uid": macro.uid,
            "hash": macro_hash(macro), "deleted": False, "device": device,
            "updated_at": _now_iso(), "macro": macro.to_dict(),
        })

    def write_tombstone(self, uid: str, device: str) -> None:
        write_json_atomic(self.macros_dir / f"{uid}.json", {
            "format": ENTRY_FORMAT, "version": FORMAT_VERSION, "uid": uid, "hash": None,
            "deleted": True, "device": device, "updated_at": _now_iso(),
        })

    def read_settings(self, errors: list[str]) -> dict | None:
        path = self.root / "settings.json"
        if not path.exists():
            return None
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(doc, dict) or not isinstance(doc.get("settings"), dict):
                raise ValueError("not a settings file")
        except (OSError, ValueError) as exc:
            errors.append(f"settings.json: {exc}")
            raise
        return doc

    def write_settings(self, settings: dict, device: str) -> None:
        write_json_atomic(self.root / "settings.json", {
            "format": ENTRY_FORMAT, "version": FORMAT_VERSION, "hash": content_hash(settings),
            "device": device, "updated_at": _now_iso(), "settings": settings,
        })

    def write_device(self, device: str, macros: int) -> None:
        from keyboardmanager import __version__

        write_json_atomic(self.devices_dir / f"{_safe_file_name(device)}.json", {
            "device": device, "last_sync": _now_iso(), "macros": macros, "app_version": __version__,
        })

    def devices(self) -> list[dict]:
        out = []
        for path in sorted(self.devices_dir.glob("*.json")) if self.devices_dir.is_dir() else []:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and "device" in data:
                out.append(data)
        return out


def scope_for(folder: str | Path) -> str:
    """Where the base versions of one sync folder are kept (another folder starts fresh)."""
    path = os.path.abspath(os.path.expanduser(str(folder)))
    return "sync:" + hashlib.sha1(path.encode("utf-8")).hexdigest()[:16]


def sync_folder(
    db: Database,
    folder: str | Path,
    device: str,
    get_settings: Callable[[], dict] | None = None,
    apply_settings: Callable[[dict], None] | None = None,
    reserved: dict[str, str] | None = None,
) -> SyncReport:
    """One sync round with ``folder``; raises ``SyncError`` when it cannot run at all.
    ``reserved``: the application's own hotkeys, which incoming macros may not take."""
    store = SyncFolder(folder)
    scope = scope_for(folder)
    report = SyncReport()
    remote, unreadable = store.entries(report.errors)
    local = {m.uid: m for m in db.list_macros() if not m.source}
    team_copies = {m.uid: m for m in db.list_macros() if m.source}
    base = {k: v for k, v in db.sync_all(scope).items() if k != SETTINGS_KEY}

    for uid in sorted(set(local) | set(remote) | set(base)):
        if uid in unreadable:
            continue  # try again next round: maybe still downloading
        try:
            _sync_one(db, store, scope, device, uid, local.get(uid), remote.get(uid), base.get(uid),
                      team_copies.get(uid), report, reserved or {})
        except (OSError, ValueError) as exc:
            report.errors.append(f"{_label(local.get(uid), remote.get(uid), uid)}: {exc}")
    if report.downloaded or report.conflicts:
        _settle(db, store, scope, device, reserved or {}, report)

    if get_settings is not None and apply_settings is not None:
        try:
            _sync_settings(db, store, scope, device, get_settings, apply_settings, report)
        except (OSError, ValueError) as exc:
            report.errors.append(f"settings: {exc}")
    try:
        store.write_device(device, sum(1 for m in db.list_macros() if not m.source))
    except OSError as exc:
        report.errors.append(f"device file: {exc}")
    return report


def _sync_one(db: Database, store: SyncFolder, scope: str, device: str, uid: str,
              mine: Macro | None, entry: dict | None, base: str | None,
              team_copy: Macro | None, report: SyncReport, reserved: dict[str, str]) -> None:
    local_hash = macro_hash(mine) if mine is not None else None
    if entry is None:
        remote_hash = None
    elif entry.get("deleted"):
        remote_hash = None
    else:
        remote_hash = entry.get("hash") or content_hash(entry.get("macro"))
    missing = entry is None  # not even a tombstone: never treated as a deletion

    if local_hash == remote_hash:
        if local_hash is None:
            db.sync_delete(scope, uid)
        else:
            db.sync_set(scope, uid, local_hash)
        return
    if missing:
        if mine is not None:
            store.write_macro(mine, device)
            db.sync_set(scope, uid, local_hash)  # type: ignore[arg-type]
            report.uploaded.append(mine.name)
        else:
            db.sync_delete(scope, uid)  # deleted here, gone there: forget it
        return

    label = _label(mine, entry, uid)
    local_changed, remote_changed = local_hash != base, remote_hash != base
    if remote_changed and not local_changed:
        _take_remote(db, store, scope, device, uid, mine, entry, remote_hash, team_copy, report, reserved)
    elif local_changed and not remote_changed:
        _send_local(db, store, scope, device, uid, mine, local_hash, report, label)
    elif mine is None:  # deleted here, changed there: the change wins
        _take_remote(db, store, scope, device, uid, None, entry, remote_hash, team_copy, report, reserved)
    elif remote_hash is None:  # deleted there, changed here: the change wins
        _send_local(db, store, scope, device, uid, mine, local_hash, report, label)
    else:  # both changed: keep the other version as a copy, this one wins in the folder
        other = _macro_from_entry(entry)
        other.uid, other.id = "", None
        other.name = db.unique_name(f"{other.name} (conflict from {entry.get('device') or 'another computer'})")
        db.save_macro(other)
        store.write_macro(other, device)  # the copy travels at once, like any new macro
        db.sync_set(scope, other.uid, macro_hash(other))
        report.conflicts.append(mine.name)
        _send_local(db, store, scope, device, uid, mine, local_hash, report, label, count=False)


def _take_remote(db: Database, store: SyncFolder, scope: str, device: str, uid: str, mine: Macro | None,
                 entry: dict, remote_hash: str | None, team_copy: Macro | None, report: SyncReport,
                 reserved: dict[str, str]) -> None:
    if remote_hash is None:  # a tombstone
        if mine is not None:
            db.delete_macro(mine.id)  # type: ignore[arg-type]
            report.deleted_here.append(mine.name)
        db.sync_delete(scope, uid)
        return
    incoming = _macro_from_entry(entry)
    incoming.uid = uid
    target = mine or team_copy  # my own macro also arriving via a team becomes my own again
    incoming.id = target.id if target is not None else None
    db.save_macro(incoming)  # name / shortcut clashes are settled after the whole round
    report.downloaded.append(incoming.name)
    db.sync_set(scope, uid, remote_hash)  # type: ignore[arg-type]


def _send_local(db: Database, store: SyncFolder, scope: str, device: str, uid: str, mine: Macro | None,
                local_hash: str | None, report: SyncReport, label: str, count: bool = True) -> None:
    if mine is None:
        store.write_tombstone(uid, device)
        db.sync_delete(scope, uid)
        report.deleted_there.append(label)
        return
    store.write_macro(mine, device)
    db.sync_set(scope, uid, local_hash)  # type: ignore[arg-type]
    if count:
        report.uploaded.append(mine.name)


def _settle(db: Database, store: SyncFolder, scope: str, device: str, reserved: dict[str, str],
            report: SyncReport) -> None:
    """After a round: no two macros may share a name, and no hotkey / abbreviation may
    clash. Every computer decides the same way (the macro with the smaller uid keeps
    its name and shortcuts), so a clash is settled once instead of bouncing between
    computers. Changed macros are written to the folder straight away."""
    from keyboardmanager.core.conflicts import abbreviation_conflict, hotkey_conflict

    macros = db.list_macros()
    kept = [m for m in macros if m.source]  # team copies stay as they are
    names = {m.name.lower() for m in kept}
    for macro in sorted((m for m in macros if not m.source), key=lambda m: m.uid):
        changed = False
        if macro.name.lower() in names:
            macro.name = _free_name(macro.name, names)
            changed = True
        names.add(macro.name.lower())
        if macro.hotkey and hotkey_conflict(macro, kept, reserved):
            macro.hotkey, changed = "", True
        if macro.abbreviation and abbreviation_conflict(macro, kept):
            macro.abbreviation, changed = "", True
        kept.append(macro)
        if changed:
            db.save_macro(macro)
            store.write_macro(macro, device)
            db.sync_set(scope, macro.uid, macro_hash(macro))
            report.adjusted.append(macro.name)


def _free_name(name: str, taken: set[str]) -> str:
    n = 2
    while f"{name} ({n})".lower() in taken:
        n += 1
    return f"{name} ({n})"


def _macro_from_entry(entry: dict) -> Macro:
    macro = Macro.from_dict(entry.get("macro"))
    problems = action_catalog.validate_actions(macro.actions)
    if problems:
        raise ValueError(problems[0])
    return macro


def _make_safe(db: Database, macro: Macro, reserved: dict[str, str] | None = None) -> None:
    """Never let an incoming macro take a hotkey / abbreviation already in use here."""
    from keyboardmanager.core.conflicts import abbreviation_conflict, hotkey_conflict

    others = [m for m in db.list_macros() if m.uid != macro.uid and (macro.id is None or m.id != macro.id)]
    if macro.hotkey and hotkey_conflict(macro, others, reserved or {}):
        macro.hotkey = ""
    if macro.abbreviation and abbreviation_conflict(macro, others):
        macro.abbreviation = ""


def _sync_settings(db: Database, store: SyncFolder, scope: str, device: str,
                   get_settings: Callable[[], dict], apply_settings: Callable[[dict], None],
                   report: SyncReport) -> None:
    local = get_settings()
    local_hash = content_hash(local)
    try:
        doc = store.read_settings(report.errors)
    except (OSError, ValueError):
        return  # reported; try again next round
    remote_hash = content_hash(doc["settings"]) if doc is not None else None
    base = db.sync_get(scope, SETTINGS_KEY)
    if local_hash == remote_hash:
        db.sync_set(scope, SETTINGS_KEY, local_hash)
        return
    if doc is None or (local_hash != base and remote_hash == base):
        store.write_settings(local, device)
        db.sync_set(scope, SETTINGS_KEY, local_hash)
        report.settings = "uploaded"
        return
    if local_hash == base or not any(local.values()):  # unchanged here, or nothing here yet
        apply_settings(doc["settings"])
        report.settings = "downloaded"
    else:  # both changed (or the first sync of a computer with its own settings): merge
        apply_settings(merge_settings(doc["settings"], local))
        report.settings = "merged"
    now_local = get_settings()
    if content_hash(now_local) != remote_hash:  # merged / normalised here: share the result
        store.write_settings(now_local, device)
    db.sync_set(scope, SETTINGS_KEY, content_hash(now_local))


def merge_settings(remote: dict, local: dict) -> dict:
    """Both sides' profiles, context rules and trigger rules (the folder's version of
    a trigger rule wins when both have one with the same name)."""
    def items(doc: dict, key: str) -> list:
        value = doc.get(key)
        return value if isinstance(value, list) else []

    profiles = list(items(remote, "profiles"))
    lowered = {p.lower() for p in profiles if isinstance(p, str)}
    profiles += [p for p in items(local, "profiles") if isinstance(p, str) and p.lower() not in lowered]
    rules = list(items(remote, "context_rules"))
    rules += [r for r in items(local, "context_rules") if r not in rules]
    triggers = list(items(remote, "triggers"))
    taken = {t.get("name") for t in triggers if isinstance(t, dict)}
    triggers += [t for t in items(local, "triggers") if isinstance(t, dict) and t.get("name") not in taken]
    return {"profiles": profiles, "context_rules": rules, "triggers": triggers}


def _label(mine: Macro | None, entry: dict | None, uid: str) -> str:
    if mine is not None:
        return mine.name
    if entry and isinstance(entry.get("macro"), dict):
        return str(entry["macro"].get("name", uid))
    return uid


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_file_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:80] or "device"

