"""Strava as a `Provider`.

Thin wrapper around the existing `StravaClient`. The point of this file
is *not* to reimplement Strava access — it's to declare Strava's
capabilities, translate Strava's raw shape into a `CanonicalPatch`,
and recognize when a Strava activity is actually a cross-system
duplicate of a Hevy workout (Hevy's share-to-Strava flow).

See PROVIDER_ARCHITECTURE.md §6 and §10. For Strava API specifics,
see `strava_client.py`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from canonical import CanonicalActivity, CanonicalPatch, FieldValue, HRSample
from provider import ExternalActivity, NotSupported, ProviderCaps
from strava_client import StravaClient, StravaError


PROVIDER_NAME = "strava"


# Capability declaration. Strava is rich on the read side (HR, power,
# distance, calories, GPS, device) but write-thin: only name,
# description, and a small handful of flags can be updated, and only
# with the `activity:write` OAuth scope which the current deployment
# does not request. Until that scope is added, `update` is a no-op /
# raises — we still declare writable_fields for clarity, gated on the
# scope being added in a follow-up.
#
# `can_create` is False by deliberate choice. Strava technically accepts
# manual activity creation, but we don't author Strava activities from
# Hevy data in this product. The asymmetry is by design.
#
# `can_list_by_window` is True: `get_activities(after=…)` cuts pagination
# cost on the polling path.
STRAVA_CAPS = ProviderCaps(
    readable_fields=frozenset(
        {
            "title",
            "description",
            "is_private",
            "device_name",
            "calories",
            "distance_meters",
            "moving_seconds",
            "hr_samples",
            "power_samples",
        }
    ),
    writable_fields=frozenset({"title", "description", "is_private"}),
    can_list_by_window=True,
    can_create=False,
)


class StravaProvider:
    """`Provider` implementation backed by `StravaClient`.

    Holds no state of its own; everything goes through the wrapped
    client and the shared `State`.
    """

    name = PROVIDER_NAME

    def __init__(self, client: StravaClient):
        self._client = client

    def capabilities(self) -> ProviderCaps:
        return STRAVA_CAPS

    def list_recent(self, since: datetime) -> Iterable[ExternalActivity]:
        """Yield Strava activities updated at or after `since`.

        Defers to the existing `StravaClient.recent_activities`, which
        already respects the user's enabled-types filter and the
        Hevy-user-id alias for VirtualRide.
        """
        # Strava client expects lookback in hours; convert from `since`.
        now = datetime.now(timezone.utc)
        delta = now - since
        lookback_hours = max(1, int(delta.total_seconds() // 3600) + 1)
        hevy_user_id = None  # orchestrator passes this when wired; safe default
        rows = self._client.recent_activities(
            hevy_user_id=hevy_user_id, limit=50, lookback_hours=lookback_hours
        )
        for r in rows:
            yield ExternalActivity(
                provider=PROVIDER_NAME,
                external_id=str(r["id"]),
                start_ts=_iso_to_epoch(r.get("start_date")),
                updated_at_ts=_iso_to_epoch(r.get("start_date")),
                raw=r,
            )

    def fetch(self, external_id: str) -> ExternalActivity:
        """Full-detail fetch. The existing client doesn't expose a clean
        'fetch one activity' separate from the import path, so for the
        spike we use the list path with a tight window and pick out the
        match. The orchestrator should prefer `list_recent` rows it
        already has."""
        raise NotImplementedError(
            "StravaProvider.fetch is not used in the spike — the "
            "orchestrator works from list_recent rows. A real "
            "implementation would call client.get_activity() directly."
        )

    def to_canonical(self, ext: ExternalActivity) -> CanonicalPatch:
        """Translate a Strava activity into a CanonicalPatch.

        Strava is the authority for distance, moving time, HR, and
        power — when present. It is *not* the authority for title or
        description (Hevy wins those via `PreferProvider("hevy")`), but
        we still produce FieldValues for them so that if Hevy has no
        title yet, Strava's name fills in.
        """
        raw = ext.raw
        set_at = ext.updated_at_ts or ext.start_ts

        def fv(value: Any) -> FieldValue:
            return FieldValue(value=value, source=PROVIDER_NAME, set_at=set_at)

        distance = raw.get("distance")
        moving = raw.get("moving_time")
        return CanonicalPatch(
            activity_type=raw.get("type"),
            start_ts=ext.start_ts,
            end_ts=(
                ext.start_ts + int(moving)
                if moving
                else None
            ),
            title=fv(raw.get("name")) if raw.get("name") else None,
            description=(
                fv(raw.get("description")) if raw.get("description") else None
            ),
            distance_meters=fv(float(distance)) if distance else None,
            moving_seconds=fv(int(moving)) if moving else None,
            # HR / power streams are fetched via a separate call in the
            # existing client; the spike doesn't pull streams. When the
            # merge writer ships, this is where sample tuples land.
            hr_samples=(),
            power_samples=(),
        )

    def origin_link(self, ext: ExternalActivity) -> tuple[str, str] | None:
        """Detect Strava activities that originated from a Hevy share.

        Strava's upload API lets the uploader set `external_id` on the
        activity. Hevy's share-to-Strava flow sets this to a stable
        marker derived from the Hevy workout id. The marker format used
        in the field has been observed as variants like the bare
        workout id, `hevy-<id>`, or `<hevy-user>-<workout-id>`.

        Until we have a verified sample of a Hevy-shared Strava
        activity in production, we recognize the conservative subset:

        * `external_id` startswith `hevy-`  → strip the prefix to get
          the Hevy workout id.
        * `external_id` is a UUID-shaped string (32 hex digits with
          dashes) AND `device_name` matches a Hevy app heuristic →
          take `external_id` as the Hevy workout id.

        Anything ambiguous returns None and falls back to the matcher.
        False positives here are the worst possible outcome (a
        cross-system link gets made to the wrong canonical), so we err
        toward returning None.
        """
        raw = ext.raw
        external_id = (raw.get("external_id") or "").strip()
        device_name = (raw.get("device_name") or "").lower()

        if external_id.startswith("hevy-"):
            return ("hevy", external_id.removeprefix("hevy-"))

        if (
            _looks_like_uuid(external_id)
            and "hevy" in device_name
        ):
            return ("hevy", external_id)

        return None

    def create(self, canonical: CanonicalActivity) -> str:
        raise NotSupported(
            "Strava is read-only in this product — we do not author Strava "
            "activities from canonical data."
        )

    def update(self, external_id: str, patch: CanonicalPatch) -> None:
        """Push title/description/privacy back to Strava.

        Requires the `activity:write` OAuth scope, which is added in a
        follow-up. For the spike, this method is wired up but raises
        `NotSupported` so callers get a clear failure instead of a
        silent no-op.
        """
        raise NotSupported(
            "StravaProvider.update requires the activity:write OAuth scope "
            "(not requested by the current deployment). Adding the scope "
            "and the PUT /activities/{id} call is a follow-up to this PR."
        )


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _iso_to_epoch(value: Any) -> int:
    if not value:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return 0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    return 0


def _looks_like_uuid(value: str) -> bool:
    """Cheap shape check for a UUIDv4 string. Not a strict validator —
    we use it only to gate the second-pass origin marker heuristic."""
    if len(value) != 36:
        return False
    parts = value.split("-")
    if len(parts) != 5:
        return False
    if [len(p) for p in parts] != [8, 4, 4, 4, 12]:
        return False
    return all(all(c in "0123456789abcdefABCDEF" for c in p) for p in parts)


__all__ = ["StravaProvider", "STRAVA_CAPS", "PROVIDER_NAME"]
