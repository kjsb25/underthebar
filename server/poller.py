"""Background polling loop for Strava → Hevy auto-import."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from hevy_client import HevyClient, HevyError
from state import State
from strava_client import StravaClient, StravaError


log = logging.getLogger("poller")


class Poller:
    """Polls Strava on a schedule and imports new matching activities.

    The loop tick is hardcoded short (10 s) so changes to the configured
    interval take effect quickly; actual work only runs when the interval
    has elapsed since the last poll.
    """

    TICK_SECONDS = 10

    def __init__(self, state: State, strava: StravaClient, hevy: HevyClient):
        self.state = state
        self.strava = strava
        self.hevy = hevy
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self._stop = False

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="strava-poller")

    async def stop(self):
        self._stop = True
        self._wake.set()
        if self._task is not None:
            await asyncio.wait([self._task], timeout=5)

    def kick(self):
        """Wake the loop early — used by the 'Sync now' button."""
        self._wake.set()

    async def _run(self):
        log.info("poller started")
        self.state.log("INFO", "Poller started")
        while not self._stop:
            try:
                await self._maybe_poll()
            except Exception as e:
                log.exception("poll iteration failed")
                self.state.log("ERROR", f"Poll iteration failed: {e}")
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.TICK_SECONDS)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
        log.info("poller stopped")
        self.state.log("INFO", "Poller stopped")

    async def _maybe_poll(self):
        if not self.state.get_bool("polling_enabled"):
            return
        interval = max(60, self.state.get_int("poll_interval_seconds", 600))
        last = self.state.get("last_poll_at_ts")
        now_ts = int(time.time())
        if last and (now_ts - int(last)) < interval:
            return
        await self.poll_once(triggered_by="schedule")
        self.state.set("last_poll_at_ts", str(now_ts))

    async def poll_once(self, triggered_by: str = "manual") -> dict:
        """Run a single fetch+import cycle. Safe to call from a route handler."""
        async with self._lock:
            return await asyncio.to_thread(self._poll_sync, triggered_by)

    def _poll_sync(self, triggered_by: str) -> dict:
        result = {
            "triggered_by": triggered_by,
            "ran_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "imported": [],
            "skipped": [],
            "errors": [],
        }
        self.state.set("last_poll_at", result["ran_at"])

        if not self.strava.is_authorized():
            msg = "Strava not authorized — skipping poll"
            self.state.log("WARN", msg)
            result["errors"].append(msg)
            return result
        if not self.hevy.is_authorized():
            msg = "Hevy not authorized — skipping poll"
            self.state.log("WARN", msg)
            result["errors"].append(msg)
            return result

        try:
            hevy_user_id = self.hevy.user_id()
            lookback = self.state.get_int("import_lookback_hours", 24)
            activities = self.strava.recent_activities(
                hevy_user_id=hevy_user_id, limit=10, lookback_hours=lookback
            )
        except (StravaError, HevyError) as e:
            self.state.log("ERROR", f"Fetch failed: {e}")
            result["errors"].append(str(e))
            return result

        is_private = self.state.get_bool("import_private")
        for a in activities:
            if self.state.is_imported(a["id"]):
                result["skipped"].append(a["id"])
                continue
            try:
                payload, hevy_workout_id = self.strava.build_hevy_workout(
                    a["id"], hevy_user_id=hevy_user_id, is_private=is_private
                )
                status = self.hevy.submit_workout(payload, hevy_workout_id)
                if status in (200, 201):
                    self.state.mark_imported(
                        a["id"], a["name"], a["type"], hevy_workout_id
                    )
                    self.state.log(
                        "INFO",
                        f"Imported {a['type']} '{a['name']}' ({a['id']}) → Hevy {hevy_workout_id}",
                    )
                    result["imported"].append(a)
                else:
                    msg = f"Import {a['id']} failed: HTTP {status}"
                    self.state.log("ERROR", msg)
                    result["errors"].append(msg)
            except (StravaError, HevyError) as e:
                msg = f"Import {a['id']} failed: {e}"
                self.state.log("ERROR", msg)
                result["errors"].append(msg)

        if not result["imported"] and not result["errors"]:
            self.state.log(
                "INFO", f"Poll ({triggered_by}) — no new activities"
            )
        return result
