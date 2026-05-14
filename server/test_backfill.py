"""Unit tests for the legacy → canonical backfill.

Each test seeds `imported_activities` and `merged_workouts` directly,
runs `backfill_once`, then inspects `canonical_activities` and
`provider_links` to verify the migration. Run from server/:
    python -m unittest test_backfill
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from backfill import BACKFILL_FLAG, backfill_once
from state import State


def _seed_import(
    state: State,
    strava_id: str,
    name: str = "Morning run",
    activity_type: str = "Run",
    imported_at: str = "2025-01-01T10:00:00+00:00",
    hevy_id: str | None = None,
) -> None:
    with sqlite3.connect(state.db_path) as raw:
        raw.execute(
            "INSERT INTO imported_activities "
            "(strava_activity_id, activity_name, activity_type, "
            "imported_at, hevy_workout_id) VALUES (?, ?, ?, ?, ?)",
            (strava_id, name, activity_type, imported_at, hevy_id),
        )


def _seed_merge(
    state: State,
    strava_id: str,
    hevy_id: str,
    merged_at: str = "2025-01-01T11:00:00+00:00",
    confidence: float = 0.9,
    source: str = "auto",
) -> None:
    with sqlite3.connect(state.db_path) as raw:
        raw.execute(
            "INSERT INTO merged_workouts "
            "(strava_activity_id, hevy_workout_id, merged_at, "
            "confidence, source) VALUES (?, ?, ?, ?, ?)",
            (strava_id, hevy_id, merged_at, confidence, source),
        )


class BackfillBaseTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.state = State(self.db_path)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            p = self.db_path + suffix
            if os.path.exists(p):
                os.unlink(p)


class IdempotencyTests(BackfillBaseTests):
    def test_first_run_does_work_second_run_skips(self):
        _seed_import(self.state, "s1", hevy_id="h1")
        first = backfill_once(self.state)
        self.assertFalse(first["skipped"])
        self.assertEqual(first["canonicals_created"], 1)
        self.assertEqual(first["strava_links_created"], 1)
        self.assertEqual(first["hevy_links_created"], 1)

        # Flag is set, so a re-run is a no-op.
        self.assertEqual(self.state.get(BACKFILL_FLAG), "1")
        second = backfill_once(self.state)
        self.assertTrue(second["skipped"])

    def test_empty_legacy_tables_still_marks_complete(self):
        # No imports, no merges — backfill still flips the flag so we
        # don't keep paying the cost on every boot.
        result = backfill_once(self.state)
        self.assertFalse(result["skipped"])
        self.assertEqual(result["canonicals_created"], 0)
        self.assertEqual(self.state.get(BACKFILL_FLAG), "1")


class ImportRowTests(BackfillBaseTests):
    def test_import_without_hevy_creates_strava_only_link(self):
        _seed_import(self.state, "s1", name="Just a run", hevy_id=None)
        backfill_once(self.state)

        link = self.state.lookup_link("strava", "s1")
        self.assertIsNotNone(link)
        self.assertEqual(link["link_source"], "backfill")
        canonical = self.state.get_canonical(link["canonical_id"])
        self.assertIsNotNone(canonical)
        self.assertEqual(canonical["activity_type"], "Run")
        # No Hevy link for this canonical.
        links = self.state.links_for_canonical(link["canonical_id"])
        self.assertEqual(len(links), 1)

    def test_import_with_hevy_creates_both_links_to_same_canonical(self):
        _seed_import(self.state, "s1", hevy_id="h1")
        backfill_once(self.state)

        s_link = self.state.lookup_link("strava", "s1")
        h_link = self.state.lookup_link("hevy", "h1")
        self.assertIsNotNone(s_link)
        self.assertIsNotNone(h_link)
        self.assertEqual(s_link["canonical_id"], h_link["canonical_id"])

    def test_import_with_missing_type_defaults_to_unknown(self):
        # imported_activities can have NULL activity_type. Backfill must
        # not violate the canonical NOT NULL constraint.
        with sqlite3.connect(self.state.db_path) as raw:
            raw.execute(
                "INSERT INTO imported_activities "
                "(strava_activity_id, activity_name, activity_type, "
                "imported_at, hevy_workout_id) VALUES (?, ?, ?, ?, ?)",
                ("s1", "Untitled", None, "2025-01-01T10:00:00+00:00", None),
            )
        backfill_once(self.state)
        link = self.state.lookup_link("strava", "s1")
        canonical = self.state.get_canonical(link["canonical_id"])
        self.assertEqual(canonical["activity_type"], "Unknown")


class MergeCaseTests(BackfillBaseTests):
    def test_case_a_collapses_two_canonicals_onto_one(self):
        # Both strava and hevy already have canonicals from imports, but
        # they were different canonicals. A merge row says they're the
        # same session — backfill should collapse them.
        _seed_import(self.state, "s1", imported_at="2025-01-01T10:00:00+00:00")
        _seed_import(
            self.state,
            "s2",  # placeholder strava id used as a synthetic anchor
            imported_at="2025-01-02T10:00:00+00:00",
            hevy_id="h1",
        )
        # Merge says s1 ↔ h1 are actually the same session.
        _seed_merge(self.state, "s1", "h1", source="user", confidence=0.95)

        backfill_once(self.state)

        s1_link = self.state.lookup_link("strava", "s1")
        h1_link = self.state.lookup_link("hevy", "h1")
        s2_link = self.state.lookup_link("strava", "s2")
        # s1 and h1 now share a canonical (the older one, from s1).
        self.assertEqual(s1_link["canonical_id"], h1_link["canonical_id"])
        # The original s2 canonical was the home of h1; after collapse
        # s2 stays linked, but its canonical was the one that got the
        # merge — depending on which was older. The s1 import is
        # earlier, so the s2 canonical is the *younger* one that got
        # dropped. s2 must still be reachable: its link should now
        # point at the kept (s1) canonical.
        self.assertEqual(s2_link["canonical_id"], s1_link["canonical_id"])

        # Sanity: only one canonical survives where there were two.
        with sqlite3.connect(self.state.db_path) as raw:
            count = raw.execute(
                "SELECT COUNT(*) FROM canonical_activities"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_case_b_strava_canonical_gains_hevy_link(self):
        # An import (Strava-only) + a merge row that introduces a new
        # Hevy workout. The existing canonical should pick up the Hevy
        # link rather than spawn a duplicate.
        _seed_import(self.state, "s1", hevy_id=None)
        _seed_merge(self.state, "s1", "h-new", confidence=0.8, source="auto")
        backfill_once(self.state)

        s_link = self.state.lookup_link("strava", "s1")
        h_link = self.state.lookup_link("hevy", "h-new")
        self.assertIsNotNone(h_link)
        self.assertEqual(s_link["canonical_id"], h_link["canonical_id"])
        self.assertAlmostEqual(h_link["confidence"], 0.8, places=4)

    def test_case_c_hevy_canonical_gains_strava_link(self):
        # The Hevy side already has a canonical (via an import that
        # carried hevy_id), and a merge row adds a *different* strava
        # activity id to that same canonical.
        _seed_import(self.state, "s-old", hevy_id="h1")
        _seed_merge(self.state, "s-new", "h1", confidence=0.7, source="auto")
        backfill_once(self.state)

        h_link = self.state.lookup_link("hevy", "h1")
        s_new_link = self.state.lookup_link("strava", "s-new")
        self.assertIsNotNone(s_new_link)
        self.assertEqual(h_link["canonical_id"], s_new_link["canonical_id"])

    def test_case_d_neither_side_has_canonical_creates_one(self):
        # No imports at all — just a stray merge row. (Unlikely in
        # practice, but the table allows it, so the backfill must too.)
        _seed_merge(self.state, "s1", "h1", confidence=0.6, source="auto")
        backfill_once(self.state)

        s_link = self.state.lookup_link("strava", "s1")
        h_link = self.state.lookup_link("hevy", "h1")
        self.assertIsNotNone(s_link)
        self.assertIsNotNone(h_link)
        self.assertEqual(s_link["canonical_id"], h_link["canonical_id"])

    def test_merge_redundant_with_import_is_noop(self):
        # Import already linked s1 ↔ h1; the merged_workouts row says
        # the same thing. Backfill should NOT create a duplicate link
        # or a second canonical.
        _seed_import(self.state, "s1", hevy_id="h1")
        _seed_merge(self.state, "s1", "h1", confidence=0.99, source="user")
        backfill_once(self.state)

        with sqlite3.connect(self.state.db_path) as raw:
            canon_count = raw.execute(
                "SELECT COUNT(*) FROM canonical_activities"
            ).fetchone()[0]
            link_count = raw.execute(
                "SELECT COUNT(*) FROM provider_links"
            ).fetchone()[0]
        self.assertEqual(canon_count, 1)
        self.assertEqual(link_count, 2)


class LinkSourceMappingTests(BackfillBaseTests):
    def test_user_source_preserved_through_backfill(self):
        # Case-d row with source='user' becomes a 'user' link, which
        # preserves stickiness against future auto rematches.
        _seed_merge(self.state, "s1", "h1", confidence=0.9, source="user")
        backfill_once(self.state)
        s_link = self.state.lookup_link("strava", "s1")
        h_link = self.state.lookup_link("hevy", "h1")
        self.assertEqual(s_link["link_source"], "user")
        self.assertEqual(h_link["link_source"], "user")

    def test_auto_source_maps_to_backfill(self):
        # Non-user legacy sources are flattened to 'backfill' so audit
        # queries can tell them apart from links created post-migration.
        _seed_merge(self.state, "s1", "h1", confidence=0.9, source="auto")
        backfill_once(self.state)
        self.assertEqual(
            self.state.lookup_link("strava", "s1")["link_source"], "backfill"
        )

    def test_import_links_always_backfill_sourced(self):
        _seed_import(self.state, "s1", hevy_id="h1")
        backfill_once(self.state)
        self.assertEqual(
            self.state.lookup_link("strava", "s1")["link_source"], "backfill"
        )
        self.assertEqual(
            self.state.lookup_link("hevy", "h1")["link_source"], "backfill"
        )


if __name__ == "__main__":
    unittest.main()
