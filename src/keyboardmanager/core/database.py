"""SQLite persistence for macros, execution history and settings."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from keyboardmanager.core.events import validate_event
from keyboardmanager.core.models import Action, HistoryEntry, Macro, utc_now
from keyboardmanager.core.limits import Limits
from keyboardmanager.core.organize import normalize_tags, validate_speed
from keyboardmanager.core.schedule import Schedule

SCHEMA = """
CREATE TABLE IF NOT EXISTS macros (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    hotkey        TEXT NOT NULL DEFAULT '',
    app_filter    TEXT NOT NULL DEFAULT '',
    window_filter TEXT NOT NULL DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 1,
    actions       TEXT NOT NULL DEFAULT '[]',
    schedule      TEXT NOT NULL DEFAULT '',
    event         TEXT NOT NULL DEFAULT '',
    abbreviation  TEXT NOT NULL DEFAULT '',
    folder        TEXT NOT NULL DEFAULT '',
    tags          TEXT NOT NULL DEFAULT '',
    favorite      INTEGER NOT NULL DEFAULT 0,
    speed         REAL NOT NULL DEFAULT 1.0,
    limits        TEXT NOT NULL DEFAULT '',
    when_busy     TEXT NOT NULL DEFAULT 'skip',
    profiles      TEXT NOT NULL DEFAULT '',
    layer         TEXT NOT NULL DEFAULT '',
    uid           TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    macro_id    INTEGER,
    macro_name  TEXT NOT NULL,
    trigger     TEXT NOT NULL DEFAULT '',
    started_at  TEXT NOT NULL,
    status      TEXT NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    actions_run INTEGER NOT NULL DEFAULT 0,
    message     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_history_macro ON history(macro_id);
CREATE TABLE IF NOT EXISTS history_steps (
    history_id  INTEGER NOT NULL,
    seq         INTEGER NOT NULL,
    depth       INTEGER NOT NULL DEFAULT 0,
    action_type TEXT NOT NULL,
    description TEXT NOT NULL,
    offset_ms   INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'ok',
    detail      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_steps_history ON history_steps(history_id);
CREATE TABLE IF NOT EXISTS triggers (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL,
    kind      TEXT NOT NULL,
    macro     TEXT NOT NULL,
    params    TEXT NOT NULL DEFAULT '{}',
    condition TEXT NOT NULL DEFAULT '',
    queue     INTEGER NOT NULL DEFAULT 0,
    enabled   INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_state (
    scope TEXT NOT NULL,
    key   TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);
"""


def _utc_iso(timestamp: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")


def _tags_from_text(text: str) -> list[str]:
    try:
        data = json.loads(text) if text else []
        return normalize_tags(data) if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def _limits_from_text(text: str) -> Limits:
    try:
        return Limits.from_dict(json.loads(text)) if text else Limits()
    except (ValueError, TypeError):
        return Limits()


def _profiles_from_text(text: str) -> list[str]:
    from keyboardmanager.core.profiles import normalize_profiles

    try:
        data = json.loads(text or "[]")
    except ValueError:
        return []
    try:
        return normalize_profiles([p for p in data if isinstance(p, str)])
    except ValueError:
        return []


def _speed_from_value(value: object) -> float:
    try:
        return validate_speed(value)
    except ValueError:
        return 1.0  # tolerate rows written by other versions


def default_db_path() -> Path:
    import os
    import sys

    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "KeyboardManager" / "keyboardmanager.db"


class Database:
    """Thread-safe wrapper (macros run on worker threads and write history)."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Bring databases created by older versions up to the current schema."""
        columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(macros)")}
        added = {
            "schedule": "TEXT NOT NULL DEFAULT ''", "event": "TEXT NOT NULL DEFAULT ''",
            "abbreviation": "TEXT NOT NULL DEFAULT ''", "folder": "TEXT NOT NULL DEFAULT ''",
            "tags": "TEXT NOT NULL DEFAULT ''", "favorite": "INTEGER NOT NULL DEFAULT 0",
            "speed": "REAL NOT NULL DEFAULT 1.0", "limits": "TEXT NOT NULL DEFAULT ''",
            "when_busy": "TEXT NOT NULL DEFAULT 'skip'",
            "profiles": "TEXT NOT NULL DEFAULT ''", "layer": "TEXT NOT NULL DEFAULT ''",
            "uid": "TEXT NOT NULL DEFAULT ''", "source": "TEXT NOT NULL DEFAULT ''",
        }
        for name, definition in added.items():  # columns added by later versions
            if name not in columns:
                self._conn.execute(f"ALTER TABLE macros ADD COLUMN {name} {definition}")
        # every macro has a stable id across devices (sync, team libraries)
        self._conn.execute("UPDATE macros SET uid = lower(hex(randomblob(16))) WHERE uid = ''")
        self._conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_macros_uid ON macros(uid)")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ macros
    @staticmethod
    def _macro_from_row(row: sqlite3.Row) -> Macro:
        try:
            actions = [Action.from_dict(a) for a in json.loads(row["actions"])]
        except (ValueError, TypeError):
            actions = []
        try:
            schedule = Schedule.from_dict(json.loads(row["schedule"])) if row["schedule"] else Schedule()
        except (ValueError, TypeError):
            schedule = Schedule()
        macro = Macro(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            hotkey=row["hotkey"],
            app_filter=row["app_filter"],
            window_filter=row["window_filter"],
            enabled=bool(row["enabled"]),
            schedule=schedule,
            event=row["event"],
            abbreviation=row["abbreviation"],
            folder=row["folder"],
            tags=_tags_from_text(row["tags"]),
            favorite=bool(row["favorite"]),
            speed=_speed_from_value(row["speed"]),
            limits=_limits_from_text(row["limits"]),
            when_busy=row["when_busy"] if row["when_busy"] in ("skip", "queue") else "skip",
            profiles=_profiles_from_text(row["profiles"]),
            layer=row["layer"],
            uid=row["uid"],
            source=row["source"],
            actions=actions,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        try:
            validate_event(macro.event, macro)
        except ValueError:
            macro.event = ""  # tolerate rows written by other versions
        return macro

    def list_macros(self) -> list[Macro]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM macros ORDER BY name COLLATE NOCASE, id").fetchall()
        return [self._macro_from_row(r) for r in rows]

    def get_macro(self, macro_id: int) -> Macro | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM macros WHERE id = ?", (macro_id,)).fetchone()
        return self._macro_from_row(row) if row else None

    def save_macro(self, macro: Macro) -> Macro:
        """Insert or update ``macro`` (sets ``id`` on insert) and return it. A new macro
        gets a ``uid``; an update without one keeps the stored uid."""
        import uuid

        now = utc_now()
        if macro.id is None and not macro.uid:
            macro.uid = uuid.uuid4().hex
        actions = json.dumps([a.to_dict() for a in macro.actions])
        schedule = json.dumps(macro.schedule.to_dict()) if macro.schedule.active else ""
        values = (
            macro.name, macro.description, macro.hotkey, macro.app_filter,
            macro.window_filter, int(macro.enabled), actions, schedule, macro.event,
            macro.abbreviation, macro.folder, json.dumps(macro.tags, ensure_ascii=False) if macro.tags else "",
            int(macro.favorite), float(macro.speed),
            json.dumps(macro.limits.to_dict()) if macro.limits.active else "",
            macro.when_busy,
            json.dumps(macro.profiles, ensure_ascii=False) if macro.profiles else "",
            macro.layer,
            macro.source,
        )
        with self._lock:
            if macro.id is None:
                cur = self._conn.execute(
                    "INSERT INTO macros (name, description, hotkey, app_filter, window_filter,"
                    " enabled, actions, schedule, event, abbreviation, folder, tags, favorite, speed,"
                    " limits, when_busy, profiles, layer, source, uid, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*values, macro.uid, now, now),
                )
                macro.id = cur.lastrowid
                macro.created_at = now
            else:
                self._conn.execute(
                    "UPDATE macros SET name=?, description=?, hotkey=?, app_filter=?,"
                    " window_filter=?, enabled=?, actions=?, schedule=?, event=?, abbreviation=?,"
                    " folder=?, tags=?, favorite=?, speed=?, limits=?, when_busy=?, profiles=?, layer=?,"
                    " source=?, uid=CASE WHEN ?='' THEN uid ELSE ? END, updated_at=? WHERE id=?",
                    (*values, macro.uid, macro.uid, now, macro.id),
                )
                if not macro.uid:
                    row = self._conn.execute("SELECT uid FROM macros WHERE id=?", (macro.id,)).fetchone()
                    macro.uid = row["uid"] if row else ""
            self._conn.commit()
        macro.updated_at = now
        return macro

    def delete_macro(self, macro_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM macros WHERE id = ?", (macro_id,))
            self._conn.commit()

    def get_macro_by_uid(self, uid: str) -> Macro | None:
        if not uid:
            return None
        with self._lock:
            row = self._conn.execute("SELECT * FROM macros WHERE uid = ?", (uid,)).fetchone()
        return self._macro_from_row(row) if row else None

    # ------------------------------------------------------------- sync state
    def sync_get(self, scope: str, key: str) -> str | None:
        """What was last agreed with a sync folder / team library (e.g. a hash)."""
        with self._lock:
            row = self._conn.execute("SELECT value FROM sync_state WHERE scope=? AND key=?",
                                     (scope, key)).fetchone()
        return row["value"] if row else None

    def sync_set(self, scope: str, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO sync_state (scope, key, value) VALUES (?, ?, ?)"
                " ON CONFLICT(scope, key) DO UPDATE SET value=excluded.value", (scope, key, value))
            self._conn.commit()

    def sync_delete(self, scope: str, key: str | None = None) -> None:
        """Forget one key, or (``key=None``) the whole scope."""
        with self._lock:
            if key is None:
                self._conn.execute("DELETE FROM sync_state WHERE scope=?", (scope,))
            else:
                self._conn.execute("DELETE FROM sync_state WHERE scope=? AND key=?", (scope, key))
            self._conn.commit()

    def sync_all(self, scope: str) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM sync_state WHERE scope=?", (scope,)).fetchall()
        return {r["key"]: r["value"] for r in rows}

    def unique_name(self, base: str) -> str:
        """``base`` or ``base (2)``, ``base (3)`` ... so imports never collide."""
        with self._lock:
            names = {r[0].lower() for r in self._conn.execute("SELECT name FROM macros")}
        if base.lower() not in names:
            return base
        n = 2
        while f"{base} ({n})".lower() in names:
            n += 1
        return f"{base} ({n})"

    # ----------------------------------------------------------------- history
    def start_history(self, macro: Macro, trigger: str, started: float | None = None) -> int:
        """``started``: the start time (POSIX timestamp); now when omitted."""
        when = utc_now() if started is None else _utc_iso(started)
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO history (macro_id, macro_name, trigger, started_at, status)"
                " VALUES (?,?,?,?, 'running')",
                (macro.id, macro.name, trigger, when),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def finish_history(
        self, history_id: int, status: str, duration_ms: int, actions_run: int, message: str = ""
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE history SET status=?, duration_ms=?, actions_run=?, message=? WHERE id=?",
                (status, duration_ms, actions_run, message, history_id),
            )
            self._conn.commit()

    def list_history(
        self, limit: int = 500, macro_id: int | None = None, status: str | None = None
    ) -> list[HistoryEntry]:
        sql = "SELECT * FROM history"
        clauses, args = [], []
        if macro_id is not None:
            clauses.append("macro_id = ?")
            args.append(macro_id)
        if status:
            clauses.append("status = ?")
            args.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [
            HistoryEntry(
                id=r["id"], macro_id=r["macro_id"], macro_name=r["macro_name"],
                trigger=r["trigger"], started_at=r["started_at"], status=r["status"],
                duration_ms=r["duration_ms"], actions_run=r["actions_run"], message=r["message"],
            )
            for r in rows
        ]

    def clear_history(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM history")
            self._conn.execute("DELETE FROM history_steps")
            self._conn.commit()

    # ---------------------------------------------------------------- triggers
    def list_triggers(self) -> list:
        from keyboardmanager.core.triggers import rule_from_row

        with self._lock:
            rows = self._conn.execute("SELECT * FROM triggers ORDER BY name COLLATE NOCASE, id").fetchall()
        return [rule_from_row(r) for r in rows]

    def save_trigger(self, rule):
        values = (rule.name, rule.kind, rule.macro, json.dumps(rule.params, ensure_ascii=False),
                  rule.condition, int(rule.queue), int(rule.enabled))
        with self._lock:
            if rule.id is None:
                cur = self._conn.execute(
                    "INSERT INTO triggers (name, kind, macro, params, condition, queue, enabled)"
                    " VALUES (?,?,?,?,?,?,?)", values)
                rule.id = cur.lastrowid
            else:
                self._conn.execute(
                    "UPDATE triggers SET name=?, kind=?, macro=?, params=?, condition=?, queue=?, enabled=?"
                    " WHERE id=?", (*values, rule.id))
            self._conn.commit()
        return rule

    def delete_trigger(self, rule_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM triggers WHERE id=?", (rule_id,))
            self._conn.commit()

    # --------------------------------------------------------------- step logs
    def add_history_steps(self, history_id: int, steps: list, not_logged: int = 0) -> None:
        """Store a run's step log (``executor.StepLog`` entries)."""
        rows = [(history_id, s.seq, s.depth, s.action_type, s.description, s.offset_ms,
                 s.duration_ms, s.status, s.detail) for s in steps]
        if not_logged:
            last = steps[-1] if steps else None
            rows.append((history_id, (last.seq if last else 0) + 1, 0, "", f"... {not_logged} more steps "
                         "were run but not logged", last.offset_ms if last else 0, 0, "ok", ""))
        with self._lock:
            self._conn.executemany(
                "INSERT INTO history_steps (history_id, seq, depth, action_type, description, offset_ms,"
                " duration_ms, status, detail) VALUES (?,?,?,?,?,?,?,?,?)", rows)
            self._conn.commit()

    def history_steps(self, history_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM history_steps WHERE history_id=? ORDER BY seq", (history_id,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ for limits
    def runs_since(self, macro_id: int, since: float) -> int:
        """How often the macro started (skipped runs not counted) since a timestamp."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM history WHERE macro_id=? AND status != 'skipped' AND started_at >= ?",
                (macro_id, _utc_iso(since))).fetchone()
        return int(row[0])

    def last_run(self, macro_id: int) -> float | None:
        """When the macro last started (timestamp), skipped runs not counted."""
        with self._lock:
            row = self._conn.execute(
                "SELECT started_at FROM history WHERE macro_id=? AND status != 'skipped'"
                " ORDER BY id DESC LIMIT 1", (macro_id,)).fetchone()
        if row is None:
            return None
        from datetime import datetime

        return datetime.fromisoformat(row[0]).timestamp()

    def mark_interrupted(self) -> None:
        """Entries left 'running' by a crash can never finish; flag them."""
        with self._lock:
            self._conn.execute(
                "UPDATE history SET status='error', message='Interrupted (application closed)'"
                " WHERE status='running'"
            )
            self._conn.commit()

    def add_skipped(self, macro: Macro, trigger: str, message: str) -> None:
        """Record a scheduled run that did not happen."""
        history_id = self.start_history(macro, trigger)
        self.finish_history(history_id, "skipped", 0, 0, message)

    # ---------------------------------------------------------------- settings
    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self._conn.commit()
