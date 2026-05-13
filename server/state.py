"""SQLite-backed persistent state for the Strava import service.

Holds rotating tokens, user-configured settings, imported activity IDs, and a
ring-buffered event log. All access goes through this module so the schema
and write semantics stay in one place.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULTS = {
    "enabled_types": '["Run", "Ride", "Walk", "Hike"]',
    "import_private": "0",
    "polling_enabled": "0",
    "poll_interval_seconds": "600",
    "import_lookback_hours": "24",
}

LOG_RETAIN_ROWS = 500


class State:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()
        self._seed_defaults()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self):
        with self._lock, self._conn() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS config (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS imported_activities (
                    strava_activity_id TEXT PRIMARY KEY,
                    activity_name TEXT,
                    activity_type TEXT,
                    imported_at TEXT NOT NULL,
                    hevy_workout_id TEXT
                );
                CREATE TABLE IF NOT EXISTS merged_workouts (
                    strava_activity_id TEXT NOT NULL,
                    hevy_workout_id TEXT NOT NULL,
                    merged_at TEXT NOT NULL,
                    confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
                    source TEXT NOT NULL DEFAULT 'auto'
                        CHECK (source IN ('auto', 'user')),
                    PRIMARY KEY (strava_activity_id, hevy_workout_id)
                );
                CREATE INDEX IF NOT EXISTS idx_merged_hevy
                    ON merged_workouts(hevy_workout_id);
                CREATE TABLE IF NOT EXISTS log_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_log_events_ts ON log_events(ts DESC);
                """
            )

    def _seed_defaults(self):
        with self._lock, self._conn() as c:
            for k, v in DEFAULTS.items():
                c.execute(
                    "INSERT OR IGNORE INTO config(key, value) VALUES(?, ?)", (k, v)
                )

    # ── Config primitives ─────────────────────────────────────────────────
    def get(self, key: str, default: str | None = None) -> str | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT value FROM config WHERE key=?", (key,)
            ).fetchone()
            return row[0] if row else default

    def set(self, key: str, value: str | None):
        with self._lock, self._conn() as c:
            if value is None:
                c.execute("DELETE FROM config WHERE key=?", (key,))
            else:
                c.execute(
                    "INSERT INTO config(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )

    def set_many(self, items: dict[str, str | None]):
        with self._lock, self._conn() as c:
            for k, v in items.items():
                if v is None:
                    c.execute("DELETE FROM config WHERE key=?", (k,))
                else:
                    c.execute(
                        "INSERT INTO config(key, value) VALUES(?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (k, v),
                    )

    def get_bool(self, key: str, default: bool = False) -> bool:
        v = self.get(key)
        if v is None:
            return default
        return v == "1"

    def set_bool(self, key: str, value: bool):
        self.set(key, "1" if value else "0")

    def get_int(self, key: str, default: int = 0) -> int:
        v = self.get(key)
        try:
            return int(v) if v is not None else default
        except ValueError:
            return default

    def get_json(self, key: str, default: Any) -> Any:
        v = self.get(key)
        if v is None:
            return default
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return default

    def set_json(self, key: str, value: Any):
        self.set(key, json.dumps(value))

    # ── Imported activity tracking ────────────────────────────────────────
    def is_imported(self, activity_id: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM imported_activities WHERE strava_activity_id=?",
                (str(activity_id),),
            ).fetchone()
            return row is not None

    def mark_imported(
        self,
        activity_id: str,
        name: str,
        activity_type: str,
        hevy_workout_id: str | None = None,
    ):
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO imported_activities "
                "(strava_activity_id, activity_name, activity_type, imported_at, hevy_workout_id) "
                "VALUES(?, ?, ?, ?, ?)",
                (
                    str(activity_id),
                    name,
                    activity_type,
                    _now_iso(),
                    hevy_workout_id,
                ),
            )

    def recent_imports(self, limit: int = 20) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT strava_activity_id, activity_name, activity_type, imported_at, hevy_workout_id "
                "FROM imported_activities ORDER BY imported_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "strava_activity_id": r[0],
                "activity_name": r[1],
                "activity_type": r[2],
                "imported_at": r[3],
                "hevy_workout_id": r[4],
            }
            for r in rows
        ]

    def import_counts(self) -> dict:
        with self._conn() as c:
            total = c.execute(
                "SELECT COUNT(*) FROM imported_activities"
            ).fetchone()[0]
            today = c.execute(
                "SELECT COUNT(*) FROM imported_activities WHERE imported_at >= ?",
                (_today_iso(),),
            ).fetchone()[0]
        return {"total": total, "today": today}

    # ── Merged workout tracking ───────────────────────────────────────────
    def is_merged(self, strava_activity_id: str, hevy_workout_id: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM merged_workouts "
                "WHERE strava_activity_id=? AND hevy_workout_id=?",
                (str(strava_activity_id), str(hevy_workout_id)),
            ).fetchone()
            return row is not None

    def mark_merged(
        self,
        strava_activity_id: str,
        hevy_workout_id: str,
        confidence: float,
        source: str = "auto",
    ):
        """Record a Strava→Hevy merge. A 'user' source is sticky: once a user
        has explicitly confirmed a merge, a later automated rematch cannot
        silently downgrade it back to 'auto'. The auto path can still update
        confidence on an existing auto row."""
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO merged_workouts "
                "(strava_activity_id, hevy_workout_id, merged_at, confidence, source) "
                "VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(strava_activity_id, hevy_workout_id) DO UPDATE SET "
                "    merged_at = excluded.merged_at, "
                "    confidence = excluded.confidence, "
                "    source = CASE "
                "        WHEN merged_workouts.source = 'user' THEN 'user' "
                "        ELSE excluded.source "
                "    END",
                (
                    str(strava_activity_id),
                    str(hevy_workout_id),
                    _now_iso(),
                    float(confidence),
                    source,
                ),
            )

    def unmerge(self, strava_activity_id: str, hevy_workout_id: str) -> int:
        """Remove a merge record. Returns the number of rows deleted."""
        with self._lock, self._conn() as c:
            cursor = c.execute(
                "DELETE FROM merged_workouts "
                "WHERE strava_activity_id=? AND hevy_workout_id=?",
                (str(strava_activity_id), str(hevy_workout_id)),
            )
            return cursor.rowcount

    def merges_for_hevy(self, hevy_workout_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT strava_activity_id, merged_at, confidence, source "
                "FROM merged_workouts WHERE hevy_workout_id=? ORDER BY merged_at DESC",
                (str(hevy_workout_id),),
            ).fetchall()
        return [
            {
                "strava_activity_id": r[0],
                "merged_at": r[1],
                "confidence": r[2],
                "source": r[3],
            }
            for r in rows
        ]

    def merges_for_strava(self, strava_activity_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT hevy_workout_id, merged_at, confidence, source "
                "FROM merged_workouts WHERE strava_activity_id=? ORDER BY merged_at DESC",
                (str(strava_activity_id),),
            ).fetchall()
        return [
            {
                "hevy_workout_id": r[0],
                "merged_at": r[1],
                "confidence": r[2],
                "source": r[3],
            }
            for r in rows
        ]

    def recent_merges(self, limit: int = 20) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT strava_activity_id, hevy_workout_id, merged_at, confidence, source "
                "FROM merged_workouts ORDER BY merged_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "strava_activity_id": r[0],
                "hevy_workout_id": r[1],
                "merged_at": r[2],
                "confidence": r[3],
                "source": r[4],
            }
            for r in rows
        ]

    # ── Event log ─────────────────────────────────────────────────────────
    def log(self, level: str, message: str):
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO log_events(ts, level, message) VALUES(?, ?, ?)",
                (_now_iso(), level, message),
            )
            c.execute(
                "DELETE FROM log_events WHERE id NOT IN ("
                "SELECT id FROM log_events ORDER BY id DESC LIMIT ?)",
                (LOG_RETAIN_ROWS,),
            )

    def recent_logs(self, limit: int = 100) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT ts, level, message FROM log_events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [{"ts": r[0], "level": r[1], "message": r[2]} for r in rows]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT00:00:00Z")


def build_state() -> State:
    db_path = os.environ.get("DATA_DIR", "/data") + "/state.db"
    return State(db_path)
