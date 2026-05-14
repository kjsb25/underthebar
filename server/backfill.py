"""One-shot migration: legacy `imported_activities` + `merged_workouts`
into the new `canonical_activities` + `provider_links` tables.

The legacy tables encode pairwise Strava↔Hevy state. The new tables
encode a canonical hub with one link per provider. Backfilling is
deterministic — given the same legacy rows, the same canonical IDs
fall out — but it is NOT idempotent across runs by design: a re-run
would create new canonicals for the same legacy rows. The orchestrator
calls `backfill_once()` on initialization, which writes a flag into
the config table so subsequent calls early-return.

See PROVIDER_ARCHITECTURE.md §9 for what coexistence with the legacy
tables looks like during this transitional period.

What this module deliberately does NOT do:

* Touch the legacy tables in any way (read-only access). The legacy
  tables remain authoritative for the existing poller until the
  orchestrator ships.
* Pull live data from Strava or Hevy. The backfill is purely a schema
  migration — what we already know about, in the form the new tables
  expect. Streams, full titles, and other details that were never
  persisted in the legacy schema are populated by the orchestrator on
  the next pull cycle.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from canonical import (
    CanonicalActivity,
    FieldValue,
    MergePolicy,
    PreferProvider,
)
from state import State


# Config key used to mark backfill complete. Idempotency is achieved by
# checking and setting this flag rather than by attempting to detect
# whether canonicals already exist for legacy rows (which would be
# fragile if a user has used both legacy and new pathways).
BACKFILL_FLAG = "backfill_v1_complete"


def backfill_once(state: State) -> dict:
    """Run the legacy → new migration if it hasn't run yet.

    Returns a summary dict of what was migrated. Safe to call on every
    boot: the first call does the work, subsequent calls early-return.
    The flag write is part of the same transaction as the migration so
    a crash partway through doesn't leave us claiming completion.
    """
    if state.get(BACKFILL_FLAG) == "1":
        return {"skipped": True, "reason": "already complete"}

    canonicals_created = 0
    strava_links_created = 0
    hevy_links_created = 0
    merges_applied = 0

    # Snapshot the legacy data first; we'll do all writes inside the
    # state accessor locks. Direct sqlite3 connection is fine for the
    # read-only snapshot — we don't write through this connection.
    with sqlite3.connect(state.db_path) as raw:
        imports = raw.execute(
            "SELECT strava_activity_id, activity_name, activity_type, "
            "imported_at, hevy_workout_id "
            "FROM imported_activities"
        ).fetchall()
        merges = raw.execute(
            "SELECT strava_activity_id, hevy_workout_id, merged_at, "
            "confidence, source "
            "FROM merged_workouts"
        ).fetchall()

    # Pass 1: every imported_activities row becomes a canonical with
    # Strava and (if present) Hevy links. The Strava import created the
    # Hevy workout, so the Hevy workout id is the same record — we link
    # both to the same canonical.
    strava_to_canonical: dict[str, str] = {}
    hevy_to_canonical: dict[str, str] = {}

    for strava_id, name, atype, imported_at, hevy_id in imports:
        canonical = _make_canonical_from_import(
            name=name,
            activity_type=atype or "",
            imported_at_iso=imported_at,
        )
        state.upsert_canonical(canonical.to_jsonable())
        canonicals_created += 1

        state.link_external(
            canonical_id=canonical.id,
            provider="strava",
            external_id=str(strava_id),
            confidence=1.0,
            link_source="backfill",
        )
        strava_links_created += 1
        strava_to_canonical[str(strava_id)] = canonical.id

        if hevy_id:
            state.link_external(
                canonical_id=canonical.id,
                provider="hevy",
                external_id=str(hevy_id),
                confidence=1.0,
                link_source="backfill",
            )
            hevy_links_created += 1
            hevy_to_canonical[str(hevy_id)] = canonical.id

    # Pass 2: merged_workouts rows. Each one links a Strava activity to
    # an existing Hevy workout. Three cases:
    #
    #   (a) Both already have canonicals (from imports), but they're
    #       different canonicals. The merge says they're the same
    #       session. Reuse the older canonical and add a Hevy link to
    #       it. The newer canonical becomes stranded; we delete it.
    #   (b) Only one side has a canonical. Add a link to that canonical
    #       for the other side.
    #   (c) Neither side has a canonical. Create one with both links.
    for strava_id, hevy_id, merged_at, confidence, source in merges:
        s_canon = strava_to_canonical.get(str(strava_id))
        h_canon = hevy_to_canonical.get(str(hevy_id))

        if s_canon and h_canon and s_canon != h_canon:
            # Case (a): collapse onto the earlier canonical.
            keep, drop = _pick_canonical_to_keep(state, s_canon, h_canon)
            # Re-link any external ids on the dropped canonical to the
            # kept one. (Today this is at most one each, but the loop
            # is forward-safe for brick scenarios.)
            for link in state.links_for_canonical(drop):
                state.link_external(
                    canonical_id=keep,
                    provider=link["provider"],
                    external_id=link["external_id"],
                    confidence=link["confidence"],
                    link_source=link["link_source"],
                    role=link["role"],
                    segment_label=link["segment_label"],
                )
            state.delete_canonical(drop)
            # Bookkeeping so case-(a) collapses stay coherent if the
            # same id appears in later iterations.
            for k, v in list(strava_to_canonical.items()):
                if v == drop:
                    strava_to_canonical[k] = keep
            for k, v in list(hevy_to_canonical.items()):
                if v == drop:
                    hevy_to_canonical[k] = keep
            canonical_id = keep
        elif s_canon and not h_canon:
            canonical_id = s_canon
            state.link_external(
                canonical_id=canonical_id,
                provider="hevy",
                external_id=str(hevy_id),
                confidence=float(confidence),
                link_source=_legacy_source_to_link_source(source),
            )
            hevy_to_canonical[str(hevy_id)] = canonical_id
            hevy_links_created += 1
        elif h_canon and not s_canon:
            canonical_id = h_canon
            state.link_external(
                canonical_id=canonical_id,
                provider="strava",
                external_id=str(strava_id),
                confidence=float(confidence),
                link_source=_legacy_source_to_link_source(source),
            )
            strava_to_canonical[str(strava_id)] = canonical_id
            strava_links_created += 1
        elif not s_canon and not h_canon:
            canonical = _make_canonical_from_merge(merged_at_iso=merged_at)
            state.upsert_canonical(canonical.to_jsonable())
            canonicals_created += 1
            state.link_external(
                canonical_id=canonical.id,
                provider="strava",
                external_id=str(strava_id),
                confidence=float(confidence),
                link_source=_legacy_source_to_link_source(source),
            )
            state.link_external(
                canonical_id=canonical.id,
                provider="hevy",
                external_id=str(hevy_id),
                confidence=float(confidence),
                link_source=_legacy_source_to_link_source(source),
            )
            strava_to_canonical[str(strava_id)] = canonical.id
            hevy_to_canonical[str(hevy_id)] = canonical.id
            strava_links_created += 1
            hevy_links_created += 1
        else:
            # s_canon == h_canon: already merged in pass 1, nothing to do.
            pass

        merges_applied += 1

    state.set(BACKFILL_FLAG, "1")
    state.log(
        "INFO",
        f"Backfill complete: {canonicals_created} canonicals, "
        f"{strava_links_created} strava links, "
        f"{hevy_links_created} hevy links, "
        f"{merges_applied} merges applied",
    )
    return {
        "skipped": False,
        "canonicals_created": canonicals_created,
        "strava_links_created": strava_links_created,
        "hevy_links_created": hevy_links_created,
        "merges_applied": merges_applied,
    }


def _make_canonical_from_import(
    name: str | None, activity_type: str, imported_at_iso: str | None
) -> CanonicalActivity:
    """Build a canonical from an `imported_activities` row.

    The legacy table didn't persist start_ts — we use the import
    timestamp as a stand-in. The next live pull will correct it via
    `to_canonical()` and the matcher's start_ts handling.
    """
    set_at = _iso_to_epoch(imported_at_iso)
    canonical = CanonicalActivity.new(
        activity_type=activity_type or "Unknown",
        start_ts=set_at,
        end_ts=None,
    )
    canonical.title = (
        FieldValue(value=name, source="backfill", set_at=set_at)
        if name
        else None
    )
    return canonical


def _make_canonical_from_merge(merged_at_iso: str | None) -> CanonicalActivity:
    """Build a canonical from a `merged_workouts` row where neither side
    had a prior canonical. We have no name and no type to draw on, so
    the canonical is mostly empty; live pulls will populate it."""
    set_at = _iso_to_epoch(merged_at_iso)
    return CanonicalActivity.new(
        activity_type="Unknown",
        start_ts=set_at,
        end_ts=None,
    )


def _pick_canonical_to_keep(
    state: State, a: str, b: str
) -> tuple[str, str]:
    """Choose which of two canonicals survives a case-(a) collapse.

    Preference: lower start_ts wins (older record). Falls back to
    lexicographic id comparison if start_ts is equal, so the choice is
    deterministic regardless of insertion order.
    """
    ca = state.get_canonical(a) or {}
    cb = state.get_canonical(b) or {}
    sa = int(ca.get("start_ts", 0))
    sb = int(cb.get("start_ts", 0))
    if sa < sb:
        return a, b
    if sb < sa:
        return b, a
    return (a, b) if a < b else (b, a)


def _legacy_source_to_link_source(legacy: str | None) -> str:
    """Map `merged_workouts.source` ('auto' | 'user') onto the wider
    link_source enum used by `provider_links`. User overrides become
    user links (preserving their stickiness); everything else collapses
    onto 'backfill' to mark its origin clearly in audit log queries."""
    if legacy == "user":
        return "user"
    return "backfill"


def _iso_to_epoch(iso: str | None) -> int:
    if not iso:
        return int(datetime.now(timezone.utc).timestamp())
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return int(datetime.now(timezone.utc).timestamp())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


# These imports are not used; reference them so unused-import linters
# don't strip the documentation of which policy types matter for the
# backfilled FieldValues. Backfill produces only `backfill`-sourced
# FieldValues, so neither MergePolicy nor PreferProvider is invoked
# here — but they govern what happens on the next live pull, which is
# the more interesting story.
_ = (MergePolicy, PreferProvider)


__all__ = ["backfill_once", "BACKFILL_FLAG"]
