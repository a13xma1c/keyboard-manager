"""SQLite persistence for macros, execution history and settings."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from keyboardmanager.core.models import Action, HistoryEntry, Macro, utc_now
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
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


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
        if "schedule" not in columns:
            self._conn.execute("ALTER TABLE macros ADD COLUMN schedule TEXT NOT NULL DEFAULT ''")

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
        return Macro(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            hotkey=row["hotkey"],
            app_filter=row["app_filter"],
            window_filter=row["window_filter"],
            enabled=bool(row["enabled"]),
            schedule=schedule,
            actions=actions,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_macros(self) -> list[Macro]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM macros ORDER BY name COLLATE NOCASE, id").fetchall()
        return [self._macro_from_row(r) for r in rows]

    def get_macro(self, macro_id: int) -> Macro | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM macros WHERE id = ?", (macro_id,)).fetchone()
        return self._macro_from_row(row) if row else None

    def save_macro(self, macro: Macro) -> Macro:
        """Insert or update ``macro`` (sets ``id`` on insert) and return it."""
        now = utc_now()
        actions = json.dumps([a.to_dict() for a in macro.actions])
        schedule = json.dumps(macro.schedule.to_dict()) if macro.schedule.active else ""
        values = (
            macro.name, macro.description, macro.hotkey, macro.app_filter,
            macro.window_filter, int(macro.enabled), actions, schedule,
        )
        with self._lock:
            if macro.id is None:
                cur = self._conn.execute(
                    "INSERT INTO macros (name, description, hotkey, app_filter, window_filter,"
                    " enabled, actions, schedule, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (*values, now, now),
                )
                macro.id = cur.lastrowid
                macro.created_at = now
            else:
                self._conn.execute(
                    "UPDATE macros SET name=?, description=?, hotkey=?, app_filter=?,"
                    " window_filter=?, enabled=?, actions=?, schedule=?, updated_at=? WHERE id=?",
                    (*values, now, macro.id),
                )
            self._conn.commit()
        macro.updated_at = now
        return macro

    def delete_macro(self, macro_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM macros WHERE id = ?", (macro_id,))
            self._conn.commit()

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
    def start_history(self, macro: Macro, trigger: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO history (macro_id, macro_name, trigger, started_at, status)"
                " VALUES (?,?,?,?, 'running')",
                (macro.id, macro.name, trigger, utc_now()),
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
            self._conn.commit()

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
