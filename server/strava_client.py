"""Strava API operations for the always-on import service.

Ported from strava_api.py with GUI/desktop-only bits removed and all state
flowing through the State object. Token rotation is persisted on every refresh.
"""

from __future__ import annotations

import copy
import random
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from stravalib import Client

from state import State


# Mirrors strava_api.ALL_ACTIVITY_TYPES — Hevy exercise template IDs.
@dataclass(frozen=True)
class ActivityType:
    type: str
    title: str
    id: str

    def matches(self, activity_type) -> bool:
        return activity_type == self.type or str(activity_type) == f"root='{self.type}'"


ALL_ACTIVITY_TYPES: list[ActivityType] = [
    ActivityType("Run", "Running", "AC1BB830"),
    ActivityType("Ride", "Cycling", "D8F7F851"),
    ActivityType("Walk", "Walking", "33EDD7DB"),
    ActivityType("Hike", "Hiking", "1C34A172"),
]

# Optional: matches the per-user VirtualRide alias from strava_api.py.
VIRTUAL_RIDE_TYPE = ActivityType(
    "VirtualRide", "Cycling (Virtual)", "89f3ed93-5418-4cc6-a114-0590f2977ae8"
)
VIRTUAL_RIDE_OWNER_HEVY_USER_ID = "f21f5af1-a602-48f0-82fb-ed09bc984326"


class StravaError(Exception):
    pass


class StravaClient:
    """Strava operations wrapped around a State for persistent tokens."""

    SCOPES = ["activity:read"]

    def __init__(self, state: State):
        self.state = state

    # ── Credentials ───────────────────────────────────────────────────────
    def has_credentials(self) -> bool:
        return bool(self.state.get("strava_client_id")) and bool(
            self.state.get("strava_client_secret")
        )

    def authorization_url(self, redirect_uri: str) -> str:
        client_id = self.state.get("strava_client_id")
        if not client_id:
            raise StravaError("Strava client ID not configured")
        return Client().authorization_url(
            client_id=int(client_id),
            redirect_uri=redirect_uri,
            scope=self.SCOPES,
        )

    def exchange_code(self, code: str) -> None:
        client_id = self.state.get("strava_client_id")
        client_secret = self.state.get("strava_client_secret")
        if not client_id or not client_secret:
            raise StravaError("Strava client ID/secret not configured")
        resp = Client().exchange_code_for_token(
            client_id=int(client_id),
            client_secret=client_secret,
            code=code,
        )
        self._store_token(resp)
        self.state.log("INFO", "Strava OAuth completed; refresh token stored")

    # ── Authenticated client ──────────────────────────────────────────────
    def _refresh_if_needed(self) -> str:
        """Ensure a non-expired access token is cached; return it."""
        client_id = self.state.get("strava_client_id")
        client_secret = self.state.get("strava_client_secret")
        refresh_token = self.state.get("strava_refresh_token")
        if not (client_id and client_secret and refresh_token):
            raise StravaError("Strava not authorized — complete OAuth first")

        access_token = self.state.get("strava_access_token")
        expires_at = self.state.get("strava_token_expires_at")
        now = int(datetime.now(timezone.utc).timestamp())
        # Refresh if missing, expired, or within 60 seconds of expiry.
        if not access_token or not expires_at or int(expires_at) - now < 60:
            resp = Client().refresh_access_token(
                client_id=int(client_id),
                client_secret=client_secret,
                refresh_token=refresh_token,
            )
            self._store_token(resp)
            access_token = resp["access_token"]
        return access_token

    def _store_token(self, resp: dict) -> None:
        self.state.set_many(
            {
                "strava_access_token": resp["access_token"],
                "strava_refresh_token": resp["refresh_token"],
                "strava_token_expires_at": str(resp["expires_at"]),
            }
        )

    def _client(self) -> Client:
        return Client(access_token=self._refresh_if_needed())

    def is_authorized(self) -> bool:
        return bool(self.state.get("strava_refresh_token"))

    # ── Activity discovery / import ───────────────────────────────────────
    def submittable_types(self, hevy_user_id: str | None) -> list[ActivityType]:
        enabled = set(self.state.get_json("enabled_types", []))
        types = [at for at in ALL_ACTIVITY_TYPES if at.type in enabled]
        if (
            hevy_user_id == VIRTUAL_RIDE_OWNER_HEVY_USER_ID
            and VIRTUAL_RIDE_TYPE.type in enabled
        ):
            types.append(VIRTUAL_RIDE_TYPE)
        return types

    def recent_activities(
        self, hevy_user_id: str | None, limit: int = 5, lookback_hours: int = 168
    ) -> list[dict]:
        """Return up to `limit` recent activities matching enabled types."""
        client = self._client()
        submittable = self.submittable_types(hevy_user_id)
        if not submittable:
            return []

        # `after` ts trims pagination cost on the polling path.
        after = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
        try:
            activities = client.get_activities(after=after, limit=20)
        except TypeError:
            activities = client.get_activities(limit=20)

        matching: list[dict] = []
        for activity in activities:
            for at in submittable:
                if at.matches(activity.type):
                    matching.append(
                        {
                            "id": str(activity.id),
                            "name": activity.name,
                            "type": at.type,
                            "type_title": at.title,
                            "start_date": _iso(activity.start_date),
                            "distance": float(activity.distance) if activity.distance else 0,
                            "moving_time": int(activity.moving_time) if activity.moving_time else 0,
                        }
                    )
                    break
            if len(matching) >= limit:
                break
        return matching

    def build_hevy_workout(
        self,
        activity_id: str,
        hevy_user_id: str | None,
        is_private: bool,
    ) -> tuple[dict, str]:
        """Return (workout_payload, hevy_workout_id) for a Strava activity.

        Mirrors strava_api.import_activity:225-308 but does not POST to Hevy —
        the caller submits the payload via HevyClient. Raises StravaError if
        the activity does not match an enabled type.
        """
        client = self._client()
        submittable = self.submittable_types(hevy_user_id)
        activity = client.get_activity(activity_id)

        matched = next(
            (at for at in submittable if at.matches(activity.type)), None
        )
        if matched is None:
            raise StravaError(
                f"Activity type {activity.type} is not in enabled types"
            )

        payload = copy.deepcopy(_RUN_TEMPLATE)
        payload["workout"]["title"] = activity.name
        payload["workout"]["exercises"][0]["title"] = matched.title
        payload["workout"]["exercises"][0]["exercise_template_id"] = matched.id
        payload["workout"]["is_private"] = bool(is_private)

        start_ts = int(activity.start_date.timestamp())
        payload["workout"]["start_time"] = start_ts
        payload["workout"]["end_time"] = start_ts + int(activity.moving_time or 0)
        payload["strava_activity_local_time"] = activity.start_date.strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        set0 = payload["workout"]["exercises"][0]["sets"][0]
        set0["duration_seconds"] = int(activity.moving_time or 0)
        set0["distance_meters"] = int(activity.distance or 0)
        set0["completed_at"] = (
            activity.start_date + timedelta(seconds=int(activity.moving_time or 0))
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        description = payload["workout"]["description"]
        if activity.description:
            description = f"{activity.description}\n\n{description}"
        if activity.device_name:
            description = f"{description}({activity.device_name})"
        payload["workout"]["description"] = description

        notes = ""
        if activity.average_heartrate:
            notes = (
                f"Heartrate Avg: {activity.average_heartrate}bpm, "
                f"Max: {activity.max_heartrate}bpm."
            )
            try:
                streams = client.get_activity_streams(
                    activity.id, types=["time", "heartrate"]
                )
                samples = []
                for i in range(len(streams["time"].data)):
                    samples.append(
                        {
                            "timestamp_ms": int(
                                (activity.start_date.timestamp() + streams["time"].data[i])
                                * 1000
                            ),
                            "bpm": streams["heartrate"].data[i],
                        }
                    )
                payload["workout"]["biometrics"] = {
                    "total_calories": activity.calories,
                    "heart_rate_samples": samples,
                }
            except Exception as e:  # streams optional
                self.state.log("WARN", f"HR streams fetch failed for {activity_id}: {e}")

        if activity.average_watts:
            notes = (
                (notes + "\n" if notes else "")
                + f"Power Avg: {activity.average_watts}W, Max: {activity.max_watts}W."
            )

        payload["workout"]["exercises"][0]["notes"] = notes

        # Deterministic Hevy workout id from start time (same as strava_api:328-330).
        rnd = random.Random()
        rnd.seed(start_ts)
        local_id = str(uuid.UUID(int=rnd.getrandbits(128), version=4))
        payload["workout"]["workout_id"] = local_id
        return payload, local_id


def _iso(dt) -> str:
    if dt is None:
        return ""
    if isinstance(dt, str):
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


_RUN_TEMPLATE: dict[str, Any] = {
    "workout": {
        "workout_id": "00000000-0000-0000-0000-000000000000",
        "title": "Running (import)",
        "description": "(Import from Strava)",
        "exercises": [
            {
                "title": "Running",
                "exercise_template_id": "AC1BB830",
                "rest_timer_seconds": 0,
                "notes": "",
                "volume_doubling_enabled": False,
                "sets": [
                    {
                        "index": 0,
                        "type": "normal",
                        "distance_meters": 0,
                        "duration_seconds": 0,
                        "completed_at": "1970-01-01T00:00:00Z",
                    }
                ],
            }
        ],
        "start_time": 0,
        "end_time": 0,
        "apple_watch": False,
        "wearos_watch": False,
        "is_private": False,
        "is_biometrics_public": True,
    },
    "share_to_strava": False,
    "strava_activity_local_time": "1970-01-01T00:00:00Z",
}


def test_credentials(client_id: str, client_secret: str) -> tuple[bool, str]:
    """Mirrors StravaTestWorker (utb_page_setting.py:526-551)."""
    if not client_id or not client_secret:
        return False, "Missing credentials"
    try:
        resp = requests.post(
            "https://www.strava.com/oauth/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "client_credentials",
            },
            timeout=10,
        )
        if resp.status_code == 401:
            return False, "Invalid client ID or secret"
        return True, "Credentials OK"
    except requests.RequestException as e:
        return False, f"Error: {e}"
