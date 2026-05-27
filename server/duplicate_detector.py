"""Detect near-duplicate Strava activities before they reach Hevy.

A user can end up with two Strava activities for the same training session:
their watch stopped mid-run and a phone restart picked up the rest, or the
same workout got recorded on two devices, or the auto-pause split a single
ride into two activities. We want to collapse these so only one activity
flows into Hevy, and the second is muted on Strava.

This module is the *detection* half — pure functions over the list of
activity dicts returned by `StravaClient.recent_activities`. The acting
half (PUT to Strava + state writes) lives in `poller.py`.

Detection rules (deliberately simple — see MATCHING_DESIGN.md for the
weighted scorer used for Strava↔Hevy matching, which solves a different
problem):

1. Two activities are duplicates iff
   - same `type` (string equality — Run vs VirtualRun are not aliased)
   - intervals overlap OR the gap between them is ≤ GAP_SECONDS (15 min)

2. The duplicate relation is transitive within a type. A chain of three
   activities A→B→C where each adjacent pair satisfies the rule above
   collapses into one group of three.

3. Inside a group, the survivor is the activity with the best GPS data,
   breaking ties by total recorded distance, then moving_time, then
   earliest start, then activity id.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable


DEFAULT_GAP_SECONDS = 15 * 60


def find_duplicate_groups(
    activities: Iterable[dict],
    gap_seconds: int = DEFAULT_GAP_SECONDS,
) -> list[list[dict]]:
    """Return groups of duplicate activities, largest groups first.

    Singletons (activities with no duplicates) are excluded from the
    result — callers only care about groups of 2+. The input order is
    irrelevant; output is deterministic regardless.
    """
    by_type: dict[str, list[dict]] = {}
    for a in activities:
        t = a.get("type")
        if not t:
            continue
        by_type.setdefault(t, []).append(a)

    groups: list[list[dict]] = []
    for type_acts in by_type.values():
        # Sort by start_ts so a single linear sweep can collapse chains.
        # Tiebreak on id so the sort is deterministic across runs even
        # when two activities share a start timestamp.
        sorted_acts = sorted(
            type_acts,
            key=lambda a: (_start_ts(a), str(a.get("id") or "")),
        )

        current: list[dict] = []
        current_max_end = 0
        for a in sorted_acts:
            start = _start_ts(a)
            end = start + max(0, int(a.get("moving_time") or 0))
            if not current:
                current = [a]
                current_max_end = end
                continue
            # Join the current group if our start is within the gap
            # window of the group's max end so far. Negative values
            # (overlap) trivially satisfy this.
            if start - current_max_end <= gap_seconds:
                current.append(a)
                if end > current_max_end:
                    current_max_end = end
            else:
                if len(current) >= 2:
                    groups.append(current)
                current = [a]
                current_max_end = end
        if len(current) >= 2:
            groups.append(current)

    # Largest groups first; tiebreak by survivor id for deterministic order.
    groups.sort(
        key=lambda g: (-len(g), str(pick_survivor(g).get("id") or "")),
    )
    return groups


def pick_survivor(group: list[dict]) -> dict:
    """The activity to keep, given a duplicate group.

    Tiebreakers, in order — each stage is only consulted if all earlier
    stages tie:

      1. has_gps              True beats False
      2. distance             more recorded distance wins
      3. moving_time          longer recording wins
      4. start_date           earlier wins (most likely the original)
      5. id                   lexicographic, smallest wins
    """
    if not group:
        raise ValueError("pick_survivor called on empty group")

    def key(a: dict):
        return (
            0 if a.get("has_gps") else 1,           # has_gps True sorts first
            -float(a.get("distance") or 0),         # more distance first
            -int(a.get("moving_time") or 0),        # longer first
            a.get("start_date") or "",              # earlier ISO string first
            str(a.get("id") or ""),                 # smallest id first
        )

    return sorted(group, key=key)[0]


def loser_title(original: str | None) -> str:
    """The title to PUT on a loser. Idempotent: applying twice is a no-op,
    so a retry after a partial failure doesn't double-prefix."""
    base = (original or "").strip()
    if base.startswith(LOSER_TITLE_PREFIX):
        return base
    if not base:
        return LOSER_TITLE_PREFIX.strip()
    return f"{LOSER_TITLE_PREFIX}{base}"


LOSER_TITLE_PREFIX = "[merged] "


def _start_ts(a: dict) -> int:
    """Coerce the activity's `start_date` (ISO-8601 string or epoch int)
    to UTC epoch seconds. Returns 0 on anything unparseable so a
    malformed activity won't crash the whole sweep — it just lands at
    the earliest position and may form a stray group, which the writer
    can detect downstream."""
    v = a.get("start_date")
    if v is None or v == "":
        return 0
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return 0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    return 0
