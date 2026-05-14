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

                /*
                 * Canonical activities and provider links — the hub-and-spoke
                 * model documented in PROVIDER_ARCHITECTURE.md.
                 *
                 * `canonical_activities` is the merged truth for one
                 * real-world training session. `fields_json` carries the
                 * FieldValue-wrapped scalars (title, description, …) and
                 * `samples_json` carries HR / power sample arrays with
                 * lineage. JSON columns are deliberate (see §8.2 of the
                 * design doc): we always load a canonical as a whole and
                 * never query "all canonicals where title.source = X".
                 */
                CREATE TABLE IF NOT EXISTS canonical_activities (
                    id              TEXT PRIMARY KEY,
                    activity_type   TEXT NOT NULL,
                    start_ts        INTEGER NOT NULL,
                    end_ts          INTEGER,
                    fields_json     TEXT NOT NULL,
                    samples_json    TEXT NOT NULL,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_canonical_start
                    ON canonical_activities(start_ts);

                /*
                 * One row per (provider, external activity). Multiple rows
                 * may share a `canonical_id` when one canonical session
                 * appears as several external activities in one provider
                 * — brick workouts on Strava are the canonical case.
                 * That's why there is NO UNIQUE (canonical_id, provider).
                 *
                 * `link_source` values:
                 *   'auto'     — matcher above auto_merge threshold
                 *   'user'     — explicit user confirmation (sticky)
                 *   'origin'   — recognized via Provider.origin_link
                 *   'backfill' — created by the one-shot migration from
                 *                imported_activities / merged_workouts
                 */
                CREATE TABLE IF NOT EXISTS provider_links (
                    canonical_id        TEXT NOT NULL,
                    provider            TEXT NOT NULL,
                    external_id         TEXT NOT NULL,
                    role                TEXT,
                    segment_label       TEXT,
                    confidence          REAL NOT NULL
                        CHECK (confidence BETWEEN 0 AND 1),
                    link_source         TEXT NOT NULL
                        CHECK (link_source IN ('auto','user','origin','backfill')),
                    last_pulled_at      TEXT,
                    last_pushed_at      TEXT,
                    last_push_hash      TEXT,
                    external_etag       TEXT,
                    skip_pulls_until    INTEGER,
                    PRIMARY KEY (provider, external_id),
                    FOREIGN KEY (canonical_id)
                        REFERENCES canonical_activities(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_provider_links_canonical
                    ON provider_links(canonical_id);

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

    # ── Canonical activities + provider links ─────────────────────────────
    #
    # The accessors below are the only code in the project that reads or
    # writes the new hub-and-spoke tables. The legacy `imported_activities`
    # and `merged_workouts` accessors above remain in place; the two
    # systems coexist for the duration of the migration (see
    # PROVIDER_ARCHITECTURE.md §9). Backfill from old to new lives in
    # backfill.py; no code in this module reads the legacy tables.

    def upsert_canonical(self, canonical_json: dict) -> None:
        """Insert or replace a canonical activity. Accepts the dict
        produced by `CanonicalActivity.to_jsonable()`.

        `canonical.py` is intentionally not imported here — `state.py`
        works only with serialized dicts so the storage layer doesn't
        depend on the model layer. The caller serializes; we persist.
        """
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO canonical_activities "
                "(id, activity_type, start_ts, end_ts, fields_json, "
                " samples_json, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "    activity_type = excluded.activity_type, "
                "    start_ts      = excluded.start_ts, "
                "    end_ts        = excluded.end_ts, "
                "    fields_json   = excluded.fields_json, "
                "    samples_json  = excluded.samples_json, "
                "    updated_at    = excluded.updated_at",
                (
                    str(canonical_json["id"]),
                    str(canonical_json["activity_type"]),
                    int(canonical_json["start_ts"]),
                    (
                        int(canonical_json["end_ts"])
                        if canonical_json.get("end_ts") is not None
                        else None
                    ),
                    json.dumps(canonical_json.get("fields", {})),
                    json.dumps(canonical_json.get("samples", {})),
                    _epoch_to_iso(canonical_json.get("created_at", 0)),
                    _epoch_to_iso(canonical_json.get("updated_at", 0)),
                ),
            )

    def get_canonical(self, canonical_id: str) -> dict | None:
        """Return the dict for a canonical, or None if not found. The
        dict round-trips through `CanonicalActivity.from_jsonable`."""
        with self._conn() as c:
            row = c.execute(
                "SELECT id, activity_type, start_ts, end_ts, fields_json, "
                "samples_json, created_at, updated_at "
                "FROM canonical_activities WHERE id=?",
                (str(canonical_id),),
            ).fetchone()
        if row is None:
            return None
        return _row_to_canonical_dict(row)

    def canonicals_in_window(
        self, start_ts: int, end_ts: int
    ) -> list[dict]:
        """Return canonicals whose `start_ts` falls inside [start_ts, end_ts].

        Used by the matcher integration to find candidates for an
        incoming external activity. The orchestrator widens the window
        per provider needs (e.g. ±24h for cross-day workouts).
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, activity_type, start_ts, end_ts, fields_json, "
                "samples_json, created_at, updated_at "
                "FROM canonical_activities "
                "WHERE start_ts BETWEEN ? AND ? "
                "ORDER BY start_ts ASC",
                (int(start_ts), int(end_ts)),
            ).fetchall()
        return [_row_to_canonical_dict(r) for r in rows]

    def delete_canonical(self, canonical_id: str) -> int:
        """Remove a canonical and all its links. Returns rows deleted
        from `canonical_activities` (links go via FK cascade)."""
        with self._lock, self._conn() as c:
            cursor = c.execute(
                "DELETE FROM canonical_activities WHERE id=?",
                (str(canonical_id),),
            )
            return cursor.rowcount

    def link_external(
        self,
        canonical_id: str,
        provider: str,
        external_id: str,
        *,
        confidence: float,
        link_source: str,
        role: str | None = None,
        segment_label: str | None = None,
    ) -> None:
        """Create or update a provider_links row.

        `link_source` of `'user'` is sticky in the same way that
        `merged_workouts.source` is on the legacy table: an existing
        user link cannot be downgraded to 'auto' by a subsequent
        automated rematch. The other link sources can be overwritten
        freely (auto can update auto, origin can update origin, etc).
        """
        if link_source not in ("auto", "user", "origin", "backfill"):
            raise ValueError(f"invalid link_source: {link_source!r}")
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO provider_links "
                "(canonical_id, provider, external_id, role, segment_label, "
                " confidence, link_source) "
                "VALUES(?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(provider, external_id) DO UPDATE SET "
                "    canonical_id   = excluded.canonical_id, "
                "    role           = excluded.role, "
                "    segment_label  = excluded.segment_label, "
                "    confidence     = excluded.confidence, "
                "    link_source    = CASE "
                "        WHEN provider_links.link_source = 'user' THEN 'user' "
                "        ELSE excluded.link_source "
                "    END",
                (
                    str(canonical_id),
                    str(provider),
                    str(external_id),
                    role,
                    segment_label,
                    float(confidence),
                    link_source,
                ),
            )

    def unlink_external(self, provider: str, external_id: str) -> int:
        """Remove a single link by (provider, external_id). Returns rows
        deleted. Does NOT delete the canonical even if this was its only
        link — deciding when a stranded canonical should be removed is
        a policy question the orchestrator owns."""
        with self._lock, self._conn() as c:
            cursor = c.execute(
                "DELETE FROM provider_links "
                "WHERE provider=? AND external_id=?",
                (str(provider), str(external_id)),
            )
            return cursor.rowcount

    def lookup_link(
        self, provider: str, external_id: str
    ) -> dict | None:
        """Return the link row for a given (provider, external_id), or None."""
        with self._conn() as c:
            row = c.execute(
                "SELECT canonical_id, provider, external_id, role, "
                "segment_label, confidence, link_source, last_pulled_at, "
                "last_pushed_at, last_push_hash, external_etag, "
                "skip_pulls_until "
                "FROM provider_links WHERE provider=? AND external_id=?",
                (str(provider), str(external_id)),
            ).fetchone()
        return _row_to_link_dict(row) if row else None

    def links_for_canonical(self, canonical_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT canonical_id, provider, external_id, role, "
                "segment_label, confidence, link_source, last_pulled_at, "
                "last_pushed_at, last_push_hash, external_etag, "
                "skip_pulls_until "
                "FROM provider_links WHERE canonical_id=? "
                "ORDER BY provider ASC, external_id ASC",
                (str(canonical_id),),
            ).fetchall()
        return [_row_to_link_dict(r) for r in rows]

    def mark_link_pulled(
        self, provider: str, external_id: str, etag: str | None = None
    ) -> None:
        """Update timestamps after a successful pull. The orchestrator
        calls this once per (provider, external_id) per poll cycle."""
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE provider_links SET last_pulled_at=?, external_etag=? "
                "WHERE provider=? AND external_id=?",
                (_now_iso(), etag, str(provider), str(external_id)),
            )

    def mark_link_pushed(
        self,
        provider: str,
        external_id: str,
        push_hash: str,
        skip_pulls_for_seconds: int = 60,
    ) -> None:
        """Update timestamps and the skip-pulls window after a successful
        push. `push_hash` is the hash of the writable subset of the
        canonical at the moment of the write — used as the dedupe key
        the next time we consider a pull from this provider. The
        skip-pulls window is the second guard against echo loops
        (PROVIDER_ARCHITECTURE.md §7)."""
        now = int(datetime.now(timezone.utc).timestamp())
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE provider_links SET "
                "  last_pushed_at=?, last_push_hash=?, skip_pulls_until=? "
                "WHERE provider=? AND external_id=?",
                (
                    _now_iso(),
                    str(push_hash),
                    now + max(0, int(skip_pulls_for_seconds)),
                    str(provider),
                    str(external_id),
                ),
            )

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


def _epoch_to_iso(epoch: int) -> str:
    """Convert an int epoch second into an ISO-8601 UTC string. Used by
    the canonical-activities accessors so the on-disk timestamp format
    stays consistent with the rest of the tables in this DB."""
    if not epoch:
        return _now_iso()
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _iso_to_epoch(iso: str | None) -> int:
    if not iso:
        return 0
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _row_to_canonical_dict(row) -> dict:
    """Convert a canonical_activities SELECT row tuple into the dict
    shape that `CanonicalActivity.from_jsonable` expects."""
    return {
        "id": row[0],
        "activity_type": row[1],
        "start_ts": row[2],
        "end_ts": row[3],
        "fields": json.loads(row[4]),
        "samples": json.loads(row[5]),
        "created_at": _iso_to_epoch(row[6]),
        "updated_at": _iso_to_epoch(row[7]),
    }


def _row_to_link_dict(row) -> dict:
    """Convert a provider_links SELECT row tuple into a dict. The column
    order must match the SELECT lists in `lookup_link` and
    `links_for_canonical` — if you change one, change both."""
    return {
        "canonical_id": row[0],
        "provider": row[1],
        "external_id": row[2],
        "role": row[3],
        "segment_label": row[4],
        "confidence": row[5],
        "link_source": row[6],
        "last_pulled_at": row[7],
        "last_pushed_at": row[8],
        "last_push_hash": row[9],
        "external_etag": row[10],
        "skip_pulls_until": row[11],
    }


def build_state() -> State:
    db_path = os.environ.get("DATA_DIR", "/data") + "/state.db"
    return State(db_path)
