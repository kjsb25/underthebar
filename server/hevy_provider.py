"""Hevy as a `Provider`.

Thin wrapper around the existing `HevyClient`. The shape mirrors
`strava_provider.py` — declare capabilities, translate Hevy's payload
into a `CanonicalPatch`, and detect cross-system origin markers
(`strava_activity_local_time` on a Hevy workout is the giveaway that
Hevy created the record from a Strava activity it imported, which is
exactly what the existing import flow does).

See PROVIDER_ARCHITECTURE.md §6 and §10.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from canonical import CanonicalActivity, CanonicalPatch, FieldValue
from hevy_client import HevyClient, HevyError
from matcher import summarize_hevy
from provider import ExternalActivity, NotSupported, ProviderCaps


PROVIDER_NAME = "hevy"


# Hevy is the authority for title / description / privacy and the
# write target for biometrics merges. It is *not* the authority for
# distance / moving / HR streams — those are imported from Strava.
#
# `can_list_by_window` is False: Hevy's public v2 API has no
# documented date-range query. The orchestrator must maintain a local
# mirror (a forthcoming `hevy_workouts` table) sourced from the
# internal `/workouts_sync_batch` and `/workouts_batch/{index}` endpoints
# the desktop client already uses. The spike does not implement the
# mirror; `list_recent` here returns an empty iterable until then.
#
# `can_create` is True: the existing import path's POST /v2/workout is
# exactly the create operation.
HEVY_CAPS = ProviderCaps(
    readable_fields=frozenset(
        {
            "title",
            "description",
            "is_private",
            "distance_meters",
            "moving_seconds",
            "hr_samples",
            "power_samples",
            "calories",
        }
    ),
    writable_fields=frozenset(
        {
            "title",
            "description",
            "is_private",
            "hr_samples",
            "power_samples",
            "calories",
        }
    ),
    can_list_by_window=False,
    can_create=True,
)


class HevyProvider:
    """`Provider` implementation backed by `HevyClient`."""

    name = PROVIDER_NAME

    def __init__(self, client: HevyClient):
        self._client = client

    def capabilities(self) -> ProviderCaps:
        return HEVY_CAPS

    def list_recent(self, since: datetime) -> Iterable[ExternalActivity]:
        """No-op until the local Hevy mirror lands.

        Returning an empty iterable rather than raising is deliberate:
        the orchestrator should be able to call `list_recent` on every
        registered provider without special-casing capabilities. The
        empty result means "no candidates from this side this tick,"
        which is the correct behavior pre-mirror.
        """
        return ()

    def fetch(self, external_id: str) -> ExternalActivity:
        """Not implemented in the spike. A real implementation would
        GET /v2/workout/{external_id} and wrap the response."""
        raise NotImplementedError(
            "HevyProvider.fetch awaits the GET /v2/workout/{id} accessor "
            "on HevyClient; not part of the spike."
        )

    def to_canonical(self, ext: ExternalActivity) -> CanonicalPatch:
        """Translate a Hevy workout payload into a CanonicalPatch.

        Defers to the existing `summarize_hevy` for the duration /
        distance accumulation (which already handles brick-workout
        per-exercise splits and the int/float/ISO timestamp polymorphism).

        Hevy is the authority for `title` / `description` / `is_private`
        via the `PreferProvider("hevy")` merge policy — those values
        always win when Hevy supplies them.
        """
        raw = ext.raw
        workout = raw.get("workout") if "workout" in raw else raw
        if not workout:
            workout = {}
        set_at = ext.updated_at_ts or ext.start_ts

        def fv(value: Any) -> FieldValue:
            return FieldValue(value=value, source=PROVIDER_NAME, set_at=set_at)

        # summary gives us the type-aware totals already.
        summary = summarize_hevy(raw)
        total_duration = sum(e.duration_seconds for e in summary.exercises)
        total_distance = sum(e.distance_meters for e in summary.exercises)

        biometrics = workout.get("biometrics") or {}
        calories = biometrics.get("total_calories")

        return CanonicalPatch(
            start_ts=summary.start_ts,
            end_ts=summary.end_ts,
            title=fv(workout.get("title")) if workout.get("title") else None,
            description=(
                fv(workout.get("description"))
                if workout.get("description")
                else None
            ),
            is_private=(
                fv(bool(workout.get("is_private")))
                if "is_private" in workout
                else None
            ),
            calories=fv(int(calories)) if calories is not None else None,
            distance_meters=(
                fv(float(total_distance)) if total_distance > 0 else None
            ),
            moving_seconds=(
                fv(int(total_duration)) if total_duration > 0 else None
            ),
            # Translating heart_rate_samples into HRSample requires
            # knowing the *origin* of those samples — which Hevy
            # doesn't track (Hevy just stores a flat list). For the
            # spike we don't ingest Hevy-side HR samples; doing so
            # safely would require either an origin convention or a
            # heuristic, both of which deserve their own design pass.
            hr_samples=(),
            power_samples=(),
        )

    def origin_link(self, ext: ExternalActivity) -> tuple[str, str] | None:
        """Detect Hevy workouts that originated from a Strava import.

        The existing import path (`strava_client.build_hevy_workout`)
        sets `strava_activity_local_time` on the outbound payload. The
        Hevy workout stored on Hevy's side carries this field, and so
        does the payload we get back on a re-pull. Its presence is a
        strong signal that this Hevy workout was created from a Strava
        activity that we (or the user, via the desktop client) imported.

        We can't recover the Strava activity id from the field alone —
        only the local time — so this returns `("strava", "")` to
        signal "Strava is the origin, look it up by start_ts" rather
        than `("strava", "<id>")`. The orchestrator handles the empty
        external_id by falling through to the matcher *but constrained
        to Strava candidates*, which is the correct behavior:
        cross-system duplicate detection narrowed the search space
        without exactly identifying the origin.

        Future hardening: stash the Strava activity id in the Hevy
        workout's description or notes as a hidden marker, so we can
        recover an exact origin link instead of a partial one.
        """
        raw = ext.raw
        workout = raw.get("workout") if "workout" in raw else raw
        if not workout:
            return None
        # The field is set at the envelope level in the existing import
        # payload, but we check both locations defensively.
        local_marker = (
            raw.get("strava_activity_local_time")
            or workout.get("strava_activity_local_time")
        )
        if local_marker:
            return ("strava", "")
        return None

    def create(self, canonical: CanonicalActivity) -> str:
        """Not implemented in the spike. The current create path lives
        in `strava_client.build_hevy_workout` + `hevy_client.submit_workout`
        and is invoked by the legacy poller. Migrating that into this
        method is part of the orchestrator wiring, not this PR."""
        raise NotImplementedError(
            "HevyProvider.create is not part of the spike. The legacy "
            "import path still owns Hevy workout creation."
        )

    def update(self, external_id: str, patch: CanonicalPatch) -> None:
        """Not implemented in the spike. This is where the additive HR /
        biometrics merge writer will live; see PROVIDER_ARCHITECTURE.md
        §11 for what's deliberately deferred."""
        raise NotImplementedError(
            "HevyProvider.update is not part of the spike. The additive "
            "merge writer (HR samples → existing Hevy biometrics, with "
            "lineage dedupe) lands in a follow-up."
        )


__all__ = ["HevyProvider", "HEVY_CAPS", "PROVIDER_NAME"]
