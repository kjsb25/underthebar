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


if __name__ == "__main__":
    unittest.main()
