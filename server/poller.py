"""Background polling loop for Strava → Hevy auto-import."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from duplicate_detector import (
    DEFAULT_GAP_SECONDS,
    find_duplicate_groups,
    loser_title,
    pick_survivor,
)
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
            "merged_duplicates": [],
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

        # Drop activities we've already absorbed into a survivor in an
        # earlier poll — they shouldn't be imported and shouldn't be
        # re-merged. Done before the duplicate sweep so a loser doesn't
        # pull a fresh activity into its old group.
        activities = [
            a for a in activities if not self.state.is_strava_loser(a["id"])
        ]

        merged_loser_ids = self._merge_strava_duplicates(activities, result)

        is_private = self.state.get_bool("import_private")
        for a in activities:
            if a["id"] in merged_loser_ids:
                continue
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

        if (
            not result["imported"]
            and not result["errors"]
            and not result["merged_duplicates"]
        ):
            self.state.log(
                "INFO", f"Poll ({triggered_by}) — no new activities"
            )
        return result

    def _merge_strava_duplicates(
        self, activities: list[dict], result: dict
    ) -> set[str]:
        """Collapse same-type, overlapping/adjacent Strava activities.

        Picks a survivor per group (best GPS → most distance → longest →
        earliest), then PUTs `hide_from_home=True` and a `[merged]` title
        prefix on each loser via Strava's update endpoint. The survivor
        is intentionally left alone — the user keeps the recording they
        actually care about, unmodified.

        Records each successful loser in `merged_strava_activities` so
        future polls skip it. Returns the set of loser ids to filter out
        of the import loop for *this* poll.

        Failures are logged and the loser is left unrecorded; the next
        poll will see the same group and retry. The `loser_title` helper
        is idempotent so retry is safe even if the title already got
        prefixed before a state write failed."""
        loser_ids: set[str] = set()
        if not self.state.get_bool("strava_merge_duplicates", default=True):
            return loser_ids

        gap = self.state.get_int(
            "strava_merge_gap_seconds", DEFAULT_GAP_SECONDS
        )
        groups = find_duplicate_groups(activities, gap_seconds=gap)
        if not groups:
            return loser_ids

        for group in groups:
            survivor = pick_survivor(group)
            for loser in group:
                if loser["id"] == survivor["id"]:
                    continue
                try:
                    new_title = loser_title(loser.get("name"))
                    self.strava.update_activity(
                        loser["id"],
                        name=new_title,
                        hide_from_home=True,
                    )
                except StravaError as e:
                    msg = (
                        f"Strava duplicate-merge failed for {loser['id']} "
                        f"(would merge into {survivor['id']}): {e}"
                    )
                    self.state.log("WARN", msg)
                    result["errors"].append(msg)
                    continue

                self.state.mark_strava_loser(
                    loser["id"], survivor["id"], loser.get("name") or ""
                )
                loser_ids.add(loser["id"])
                summary = {
                    "loser_id": loser["id"],
                    "loser_name": loser.get("name") or "",
                    "survivor_id": survivor["id"],
                    "survivor_name": survivor.get("name") or "",
                }
                result["merged_duplicates"].append(summary)
                self.state.log(
                    "INFO",
                    f"Merged duplicate {loser['type']} {loser['id']} "
                    f"'{loser.get('name')}' → {survivor['id']} "
                    f"'{survivor.get('name')}'",
                )
        return loser_ids
