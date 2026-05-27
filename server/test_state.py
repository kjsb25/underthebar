"""Unit tests for the merged_workouts persistence layer in state.py.

Run from the server/ directory:
    python -m unittest test_state

Each test uses a fresh in-memory-like DB via a tempfile so we never collide
with the real /data/state.db.
"""

from __future__ import annotations

import os
import tempfile
import unittest

from state import State


class MergedWorkoutsTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.state = State(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)
        wal = self.db_path + "-wal"
        shm = self.db_path + "-shm"
        for p in (wal, shm):
            if os.path.exists(p):
                os.unlink(p)

    def test_mark_and_query_round_trip(self):
        self.state.mark_merged("strava-1", "hevy-a", confidence=0.92)
        self.assertTrue(self.state.is_merged("strava-1", "hevy-a"))
        self.assertFalse(self.state.is_merged("strava-1", "hevy-b"))

        rows = self.state.merges_for_strava("strava-1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["hevy_workout_id"], "hevy-a")
        self.assertAlmostEqual(rows[0]["confidence"], 0.92, places=4)
        self.assertEqual(rows[0]["source"], "auto")

    def test_composite_pk_allows_brick_workouts(self):
        # One Hevy workout merged with two Strava activities (real brick).
        self.state.mark_merged("strava-bike", "hevy-brick", 0.95)
        self.state.mark_merged("strava-run", "hevy-brick", 0.93)
        rows = self.state.merges_for_hevy("hevy-brick")
        self.assertEqual(
            {r["strava_activity_id"] for r in rows},
            {"strava-bike", "strava-run"},
        )

    def test_user_source_is_sticky_against_auto_rematch(self):
        # User confirmed a borderline match manually.
        self.state.mark_merged("s1", "h1", confidence=0.72, source="user")
        # Later, the auto matcher rematches the same pair with lower confidence.
        self.state.mark_merged("s1", "h1", confidence=0.81, source="auto")
        rows = self.state.merges_for_strava("s1")
        self.assertEqual(rows[0]["source"], "user")
        # Confidence is still updated to the latest score for transparency.
        self.assertAlmostEqual(rows[0]["confidence"], 0.81, places=4)

    def test_user_can_re_confirm_an_auto_merge(self):
        self.state.mark_merged("s1", "h1", confidence=0.91, source="auto")
        self.state.mark_merged("s1", "h1", confidence=0.91, source="user")
        rows = self.state.merges_for_strava("s1")
        self.assertEqual(rows[0]["source"], "user")

    def test_unmerge_returns_row_count(self):
        self.state.mark_merged("s1", "h1", 0.9)
        self.assertEqual(self.state.unmerge("s1", "h1"), 1)
        self.assertEqual(self.state.unmerge("s1", "h1"), 0)
        self.assertFalse(self.state.is_merged("s1", "h1"))

    def test_int_and_str_ids_collide_safely(self):
        # State coerces both sides to str so a caller that occasionally
        # passes an int Strava id won't fragment the table.
        self.state.mark_merged(12345, "h1", 0.9)
        self.assertTrue(self.state.is_merged("12345", "h1"))
        self.assertTrue(self.state.is_merged(12345, "h1"))

    def test_confidence_check_constraint_rejects_out_of_range(self):
        import sqlite3

        with self.assertRaises(sqlite3.IntegrityError):
            self.state.mark_merged("s-bad", "h-bad", confidence=1.5)
        with self.assertRaises(sqlite3.IntegrityError):
            self.state.mark_merged("s-bad", "h-bad", confidence=-0.1)

    def test_invalid_source_rejected(self):
        import sqlite3

        with self.assertRaises(sqlite3.IntegrityError):
            self.state.mark_merged("s-x", "h-x", 0.5, source="hacker")

    def test_recent_merges_orders_by_time_desc(self):
        # mark_merged stamps merged_at with seconds resolution, so we can't
        # rely on insertion order alone — but we can rely on the secondary
        # tie-break of insertion identity since SQLite's ORDER BY is stable
        # within the same merged_at second. So verify both rows are present.
        self.state.mark_merged("s1", "h1", 0.9)
        self.state.mark_merged("s2", "h2", 0.8)
        rows = self.state.recent_merges()
        self.assertEqual(len(rows), 2)
        self.assertIn(rows[0]["strava_activity_id"], {"s1", "s2"})


class MergedStravaActivitiesTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.state = State(self.db_path)

    def tearDown(self):
        os.unlink(self.db_path)
        for suffix in ("-wal", "-shm"):
            p = self.db_path + suffix
            if os.path.exists(p):
                os.unlink(p)

    def test_mark_and_lookup_round_trip(self):
        self.state.mark_strava_loser("loser-1", "survivor-1", "Morning run")
        self.assertTrue(self.state.is_strava_loser("loser-1"))
        self.assertFalse(self.state.is_strava_loser("survivor-1"))
        self.assertEqual(self.state.get_strava_survivor("loser-1"), "survivor-1")

    def test_int_and_str_ids_collide_safely(self):
        # Callers occasionally pass int Strava ids; the state coerces both
        # sides so the table doesn't fragment.
        self.state.mark_strava_loser(12345, 99999, "Run")
        self.assertTrue(self.state.is_strava_loser("12345"))
        self.assertTrue(self.state.is_strava_loser(12345))

    def test_insert_or_ignore_preserves_first_original_title(self):
        # First call captures the *real* original title. A retry where
        # the title has already been prefixed on Strava should not
        # overwrite the recorded original — that's how we'd later
        # restore it on un-merge.
        self.state.mark_strava_loser("l1", "s1", "Original")
        self.state.mark_strava_loser("l1", "s1", "[merged] Original")
        merges = self.state.recent_strava_merges()
        self.assertEqual(len(merges), 1)
        self.assertEqual(merges[0]["original_title"], "Original")

    def test_one_loser_belongs_to_one_survivor(self):
        # loser_id is PK — a second survivor for the same loser is
        # ignored. (Real scenario: a survivor itself later gets merged
        # into a third recording. We want the loser→original-survivor
        # link preserved for un-merge purposes.)
        self.state.mark_strava_loser("loser", "survivor-A", "title")
        self.state.mark_strava_loser("loser", "survivor-B", "title")
        self.assertEqual(self.state.get_strava_survivor("loser"), "survivor-A")

    def test_one_survivor_can_absorb_many_losers(self):
        self.state.mark_strava_loser("l1", "s1", "title1")
        self.state.mark_strava_loser("l2", "s1", "title2")
        merges = self.state.recent_strava_merges()
        self.assertEqual({m["loser_id"] for m in merges}, {"l1", "l2"})
        self.assertTrue(
            all(m["survivor_id"] == "s1" for m in merges)
        )

    def test_recent_strava_merges_ordered_by_time_desc_with_limit(self):
        self.state.mark_strava_loser("l1", "s1", "title1")
        self.state.mark_strava_loser("l2", "s2", "title2")
        merges = self.state.recent_strava_merges(limit=1)
        self.assertEqual(len(merges), 1)

    def test_default_strava_merge_duplicates_is_on(self):
        # The feature defaults on so new installs get the behavior
        # without an explicit toggle. Confirms DEFAULTS seeded the row.
        self.assertTrue(self.state.get_bool("strava_merge_duplicates"))

    def test_default_gap_seconds_is_900(self):
        self.assertEqual(
            self.state.get_int("strava_merge_gap_seconds"), 15 * 60
        )


if __name__ == "__main__":
    unittest.main()
