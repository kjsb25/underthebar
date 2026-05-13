"""Hevy API operations for the always-on import service.

Hevy has no public OAuth flow. Tokens are bootstrapped by pasting an access /
refresh token pair from the desktop session.json. The refresh token rotates
on every refresh — we persist the new one each time.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import requests

from state import State


BASIC_HEADERS = {
    "x-api-key": "with_great_power",
    "Content-Type": "application/json",
    "accept-encoding": "gzip",
}


class HevyError(Exception):
    pass


class HevyClient:
    def __init__(self, state: State):
        self.state = state

    def is_authorized(self) -> bool:
        return bool(self.state.get("hevy_refresh_token"))

    def set_tokens(
        self, access_token: str, refresh_token: str, expires_at: str | None = None
    ):
        """Bootstrap tokens (one-time paste from desktop session.json)."""
        self.state.set_many(
            {
                "hevy_access_token": access_token,
                "hevy_refresh_token": refresh_token,
                "hevy_token_expires_at": expires_at or "",
                "hevy_user_id": "",
                "hevy_username": "",
            }
        )
        self.state.log("INFO", "Hevy tokens stored")

    def clear_tokens(self):
        self.state.set_many(
            {
                "hevy_access_token": None,
                "hevy_refresh_token": None,
                "hevy_token_expires_at": None,
                "hevy_user_id": None,
                "hevy_username": None,
            }
        )

    def _refresh_if_needed(self) -> str:
        """Return a valid access token, refreshing first if needed."""
        access_token = self.state.get("hevy_access_token")
        refresh_token = self.state.get("hevy_refresh_token")
        expires_at = self.state.get("hevy_token_expires_at")
        if not refresh_token:
            raise HevyError("Hevy not authorized — paste tokens first")

        if access_token and expires_at and not _is_expired(expires_at):
            return access_token
        return self._refresh(refresh_token)

    def _refresh(self, refresh_token: str) -> str:
        s = requests.Session()
        s.headers.update(BASIC_HEADERS)
        r = s.post(
            "https://api.hevyapp.com/auth/refresh_token",
            json={"refresh_token": refresh_token},
            timeout=15,
        )
        if r.status_code != 200:
            raise HevyError(f"Token refresh failed: {r.status_code} {r.text[:200]}")
        data = r.json()
        # Hevy rotates refresh tokens — persist the new pair atomically.
        self.state.set_many(
            {
                "hevy_access_token": data["access_token"],
                "hevy_refresh_token": data["refresh_token"],
                "hevy_token_expires_at": data["expires_at"],
            }
        )
        return data["access_token"]

    def _auth_session(self) -> requests.Session:
        token = self._refresh_if_needed()
        s = requests.Session()
        s.headers.update(BASIC_HEADERS)
        s.headers["Authorization"] = f"Bearer {token}"
        return s

    def account(self) -> dict:
        s = self._auth_session()
        r = s.get("https://api.hevyapp.com/account", timeout=15)
        if r.status_code != 200:
            raise HevyError(f"account fetch failed: {r.status_code}")
        data = r.json()
        # Cache identity for VirtualRide alias logic.
        self.state.set_many(
            {
                "hevy_user_id": data.get("id", ""),
                "hevy_username": data.get("username", ""),
            }
        )
        return data

    def user_id(self) -> str | None:
        cached = self.state.get("hevy_user_id")
        if cached:
            return cached
        try:
            return self.account().get("id")
        except HevyError:
            return None

    def submit_workout(self, payload: dict, workout_id: str) -> int:
        """POST then PUT-on-409 mirror of strava_api.import_activity:335-346."""
        s = self._auth_session()
        body = json.dumps(payload)
        r = s.post("https://api.hevyapp.com/v2/workout", data=body, timeout=30)
        if r.status_code == 409:
            r = s.put(
                f"https://api.hevyapp.com/v2/workout/{workout_id}",
                data=body,
                timeout=30,
            )
        return r.status_code


def _is_expired(expires_at: str) -> bool:
    """Hevy stores `expires_at` as ISO 8601 UTC string (e.g. 2026-05-12T13:00:00.123Z)."""
    if not expires_at:
        return True
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    # 30s skew margin.
    return expires_at <= now
