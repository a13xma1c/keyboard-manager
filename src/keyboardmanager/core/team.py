"""Team libraries: macros shared with other people through a shared folder.

A team library is a folder everybody in the team can reach (a network share, a
shared Dropbox / OneDrive / Google Drive folder, a git checkout...)::

    keyboardmanager-team/
        team.json             {"name": "Support team", ...}
        macros/<uid>.json     one published macro: author, version, content

* **Publish** copies one of *your* macros into the library (again after editing it:
  the version goes up). Publishing over a version you have not seen yet - someone
  else changed it - is refused unless forced, so nobody silently overwrites a
  colleague's change.
* **Subscribing** copies the library's macros into this computer as *team macros*:
  read-only, kept in the folder "Team/<name>", updated and removed as the library
  changes (each pull). Their hotkeys / abbreviations are dropped here when they
  would clash with yours. Whether a team macro is enabled is decided locally.
* **Edit locally** turns a team macro into your own (same identity), so you can
  change it and publish the result back.

Who may write to the library is decided by the folder's own permissions.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from keyboardmanager.core import actions as action_catalog
from keyboardmanager.core.database import Database
from keyboardmanager.core.models import Macro
from keyboardmanager.core.sync import _now_iso, write_json_atomic

TEAM_DIR = "keyboardmanager-team"
TEAM_FORMAT = "keyboardmanager-team"
ENTRY_FORMAT = "keyboardmanager-team-macro"
FORMAT_VERSION = 1
TEAMS_SETTING = "teams"              # JSON list of subscriptions
AUTHOR_SETTING = "team_author"       # your name, written into what you publish
TEAM_FOLDER = "Team"                 # team macros live in "Team/<team name>"


class TeamError(Exception):
    pass


class TeamConflict(TeamError):
    """The library changed since this computer last saw it."""


@dataclass
class Subscription:
    id: str
    path: str
    name: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "path": self.path, "name": self.name}

    @property
    def source(self) -> str:
        return f"team:{self.id}"

    @property
    def scope(self) -> str:
        return f"team:{self.id}"


@dataclass
class PullReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    cleared: list[str] = field(default_factory=list)   # hotkey / abbreviation dropped here
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)

    def summary(self) -> str:
        parts = [f"{len(v)} {label}" for label, v in (
            ("new", self.added), ("updated", self.updated), ("removed", self.removed),
            ("shortcut(s) dropped", self.cleared), ("problem(s)", self.errors)) if v]
        return ", ".join(parts) if parts else "up to date"


def subscriptions_from_json(text: str) -> list[Subscription]:
    try:
        data = json.loads(text or "[]")
    except ValueError:
        return []
    return [Subscription(str(d["id"]), str(d["path"]), str(d.get("name", "")))
            for d in data if isinstance(d, dict) and "id" in d and "path" in d]


def subscriptions_to_json(subs: list[Subscription]) -> str:
    return json.dumps([s.to_dict() for s in subs], ensure_ascii=False)


def new_subscription(path: str) -> Subscription:
    library = TeamLibrary(path)
    return Subscription(uuid.uuid4().hex[:12], str(path), library.name)


class TeamLibrary:
    """The files of one team library."""

    def __init__(self, folder: str | Path) -> None:
        self.root = Path(str(folder)).expanduser() / TEAM_DIR
        info = self.root / "team.json"
        try:
            data = json.loads(info.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise TeamError(f"{folder} is not a team library (no {TEAM_DIR}/team.json)") from None
        except (OSError, ValueError) as exc:
            raise TeamError(f"cannot read the team library: {exc}") from None
        if not isinstance(data, dict) or data.get("format") != TEAM_FORMAT:
            raise TeamError(f"{folder} is not a team library")
        self.name = str(data.get("name") or "Team")
        self.macros_dir = self.root / "macros"

    @staticmethod
    def create(folder: str | Path, name: str) -> "TeamLibrary":
        base = Path(str(folder)).expanduser()
        if not base.is_dir():
            raise TeamError(f"the folder {folder} does not exist")
        name = " ".join(name.split())
        if not name:
            raise TeamError("give the team a name")
        if (base / TEAM_DIR / "team.json").exists():
            raise TeamError("there is already a team library in this folder")
        write_json_atomic(base / TEAM_DIR / "team.json",
                          {"format": TEAM_FORMAT, "version": FORMAT_VERSION, "name": name,
                           "created_at": _now_iso()})
        (base / TEAM_DIR / "macros").mkdir(exist_ok=True)
        return TeamLibrary(folder)

    def entries(self, errors: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        if not self.macros_dir.is_dir():
            return out
        for path in sorted(self.macros_dir.glob("*.json")):
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(entry, dict) or entry.get("format") != ENTRY_FORMAT:
                    raise ValueError("not a team macro")
                if entry.get("uid") != path.stem or not isinstance(entry.get("version"), int):
                    raise ValueError("damaged entry")
            except (OSError, ValueError) as exc:
                errors.append(f"{path.name}: {exc}")
                continue
            out[path.stem] = entry
        return out

    def entry(self, uid: str) -> dict | None:
        """The library's entry for ``uid`` (None when there is none); raises TeamError
        when it exists but cannot be read."""
        path = self.macros_dir / f"{uid}.json"
        if not path.exists():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TeamError(f"cannot read {path.name}: {exc}") from None
        if not isinstance(entry, dict) or not isinstance(entry.get("version"), int):
            raise TeamError(f"{path.name} is damaged")
        return entry

    def write(self, macro: Macro, version: int, author: str) -> None:
        data = macro.to_dict()
        data.pop("folder", None)      # where it sits is each subscriber's business
        data.pop("favorite", None)
        write_json_atomic(self.macros_dir / f"{macro.uid}.json", {
            "format": ENTRY_FORMAT, "version": version, "uid": macro.uid, "author": author,
            "updated_at": _now_iso(), "macro": data,
        })

    def remove(self, uid: str) -> None:
        path = self.macros_dir / f"{uid}.json"
        if path.exists():
            path.unlink()


# --------------------------------------------------------------------- actions
def pull(db: Database, sub: Subscription, reserved: dict[str, str] | None = None) -> PullReport:
    """Bring this computer's copies of the team's macros up to date."""
    from keyboardmanager.core.sync import _make_safe

    library = TeamLibrary(sub.path)
    report = PullReport()
    entries = library.entries(report.errors)
    folder = f"{TEAM_FOLDER}/{_folder_safe(library.name)}"
    seen = db.sync_all(sub.scope)
    for uid, entry in entries.items():
        local = db.get_macro_by_uid(uid)
        version = str(entry["version"])
        if local is not None and local.source != sub.source:
            if not local.source:  # my own macro (I published it): just note the version
                db.sync_set(sub.scope, uid, version)
            else:
                report.errors.append(f"{local.name}: already comes from another team library")
            continue
        if local is not None and seen.get(uid) == version:
            continue  # unchanged
        try:
            incoming = Macro.from_dict(entry.get("macro"))
            problems = action_catalog.validate_actions(incoming.actions)
            if problems:
                raise ValueError(problems[0])
        except ValueError as exc:
            report.errors.append(f"{uid}.json: {exc}")
            continue
        incoming.uid, incoming.source, incoming.folder = uid, sub.source, folder
        incoming.description = _with_author(incoming.description, entry.get("author", ""))
        if local is not None:
            incoming.id, incoming.enabled = local.id, local.enabled  # enabling is a local choice
        if any(m.name.lower() == incoming.name.lower() and m.uid != uid for m in db.list_macros()):
            incoming.name = db.unique_name(f"{incoming.name} ({library.name})")
        had = (incoming.hotkey, incoming.abbreviation)
        _make_safe(db, incoming, reserved)
        if had != (incoming.hotkey, incoming.abbreviation):
            report.cleared.append(incoming.name)
        db.save_macro(incoming)
        db.sync_set(sub.scope, uid, version)
        (report.updated if local is not None else report.added).append(incoming.name)
    for macro in db.list_macros():
        if macro.source == sub.source and macro.uid not in entries:
            db.delete_macro(macro.id)  # type: ignore[arg-type]
            db.sync_delete(sub.scope, macro.uid)
            report.removed.append(macro.name)
    return report


def publish(db: Database, sub: Subscription, macro: Macro, author: str, force: bool = False) -> int:
    """Write one of your macros to the library; returns the new version."""
    if macro.source:
        raise TeamError(f'"{macro.name}" is a team macro: use "Edit locally" to make it yours first')
    if macro.id is None or not macro.uid:
        raise TeamError("save the macro first")
    library = TeamLibrary(sub.path)
    current = library.entry(macro.uid)
    known = db.sync_get(sub.scope, macro.uid)
    if current is not None and not force and str(current["version"]) != known:
        who = current.get("author") or "someone"
        raise TeamConflict(f'"{macro.name}" was changed in the library by {who} '
                           f'(version {current["version"]}) since you last saw it')
    version = (current["version"] if current else 0) + 1
    library.write(macro, version, author.strip() or "unknown")
    db.sync_set(sub.scope, macro.uid, str(version))
    return version


def unpublish(db: Database, sub: Subscription, uid: str) -> None:
    TeamLibrary(sub.path).remove(uid)
    db.sync_delete(sub.scope, uid)


def detach(db: Database, macro: Macro) -> Macro:
    """A team macro becomes your own (same identity, so it can be published back)."""
    if not macro.source:
        return macro
    macro.source = ""
    db.save_macro(macro)
    return macro


def unsubscribe(db: Database, sub: Subscription) -> int:
    """Forget the subscription: its team macros are removed from this computer."""
    removed = 0
    for macro in db.list_macros():
        if macro.source == sub.source:
            db.delete_macro(macro.id)  # type: ignore[arg-type]
            removed += 1
    db.sync_delete(sub.scope)
    return removed


def _with_author(description: str, author: str) -> str:
    """The description with one "Published by ..." line (replacing an older one)."""
    lines = [line for line in description.splitlines() if not line.startswith("Published by ")]
    if author:
        lines.append(f"Published by {author}.")
    return "\n".join(lines)


def _folder_safe(name: str) -> str:
    return name.replace("/", "-").strip() or "Team"
