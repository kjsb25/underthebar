"""Unit tests for the canonical / provider_links accessors on State.

Mirrors test_state.py's tempfile pattern. Run from server/:
    python -m unittest test_provider_state
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from canonical import CanonicalActivity, FieldValue, HRSample
from state import State


class CanonicalAccessorTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.state = State(self.db_path)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            p = self.db_path + suffix
            if os.path.exists(p):
                os.unlink(p)

    def _make_canonical(self) -> CanonicalActivity:
        c = CanonicalActivity.new("Run", start_ts=1700000000, end_ts=1700003600)
        c.title = FieldValue("Morning run", "strava", 1700000000)
        c.distance_meters = FieldValue(10000.0, "strava", 1700000000)
        c.hr_samples.append(
            HRSample(timestamp_ms=1700000500_000, bpm=140, source_provider="strava", source_external_id="ext-1")
        )
        return c

    def test_upsert_and_get_round_trip(self):
        c = self._make_canonical()
        self.state.upsert_canonical(c.to_jsonable())
        rebuilt = self.state.get_canonical(c.id)
        self.assertIsNotNone(rebuilt)
        restored = CanonicalActivity.from_jsonable(rebuilt)
        self.assertEqual(restored.title.value, "Morning run")
        self.assertEqual(len(restored.hr_samples), 1)

    def test_get_unknown_returns_none(self):
        self.assertIsNone(self.state.get_canonical("does-not-exist"))

    def test_canonicals_in_window(self):
        for ts in (1000, 2000, 3000):
            c = CanonicalActivity.new("Run", start_ts=ts, end_ts=ts + 1000)
            self.state.upsert_canonical(c.to_jsonable())
        rows = self.state.canonicals_in_window(1500, 2500)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["start_ts"], 2000)

    def test_delete_canonical_cascades_links(self):
        c = self._make_canonical()
        self.state.upsert_canonical(c.to_jsonable())
        self.state.link_external(
            canonical_id=c.id,
            provider="strava",
            external_id="ext-1",
            confidence=0.95,
            link_source="auto",
        )
        deleted = self.state.delete_canonical(c.id)
        self.assertEqual(deleted, 1)
        # Link should be gone via FK cascade.
        self.assertIsNone(self.state.lookup_link("strava", "ext-1"))


class LinkAccessorTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.state = State(self.db_path)
        self.canonical = CanonicalActivity.new("Run", 1700000000, 1700003600)
        self.state.upsert_canonical(self.canonical.to_jsonable())

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            p = self.db_path + suffix
            if os.path.exists(p):
                os.unlink(p)

    def test_link_round_trip(self):
        self.state.link_external(
            canonical_id=self.canonical.id,
            provider="strava",
            external_id="abc",
            confidence=0.9,
            link_source="auto",
        )
        link = self.state.lookup_link("strava", "abc")
        self.assertIsNotNone(link)
        self.assertEqual(link["canonical_id"], self.canonical.id)
        self.assertEqual(link["link_source"], "auto")

    def test_user_link_source_is_sticky(self):
        # Once a user has confirmed a link, auto rematching can't
        # downgrade it back to 'auto'.
        self.state.link_external(
            canonical_id=self.canonical.id,
            provider="strava",
            external_id="abc",
            confidence=0.72,
            link_source="user",
        )
        self.state.link_external(
            canonical_id=self.canonical.id,
            provider="strava",
            external_id="abc",
            confidence=0.88,
            link_source="auto",
        )
        link = self.state.lookup_link("strava", "abc")
        self.assertEqual(link["link_source"], "user")
        # Confidence still updates so audit views show the latest score.
        self.assertAlmostEqual(link["confidence"], 0.88, places=4)

    def test_invalid_link_source_rejected(self):
        with self.assertRaises(ValueError):
            self.state.link_external(
                canonical_id=self.canonical.id,
                provider="strava",
                external_id="abc",
                confidence=0.9,
                link_source="hackery",
            )

    def test_confidence_check_constraint(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.state.link_external(
                canonical_id=self.canonical.id,
                provider="strava",
                external_id="abc",
                confidence=1.5,
                link_source="auto",
            )

    def test_brick_allows_multiple_links_per_provider(self):
        # No UNIQUE(canonical_id, provider) — same canonical can have
        # two Strava links (the ride leg and the run leg of a brick).
        self.state.link_external(
            canonical_id=self.canonical.id,
            provider="strava",
            external_id="ride-leg",
            confidence=0.9,
            link_source="auto",
            role="segment",
            segment_label="Ride leg",
        )
        self.state.link_external(
            canonical_id=self.canonical.id,
            provider="strava",
            external_id="run-leg",
            confidence=0.9,
            link_source="auto",
            role="segment",
            segment_label="Run leg",
        )
        links = self.state.links_for_canonical(self.canonical.id)
        self.assertEqual(len(links), 2)
        labels = {l["segment_label"] for l in links}
        self.assertEqual(labels, {"Ride leg", "Run leg"})

    def test_unlink_returns_rowcount(self):
        self.state.link_external(
            canonical_id=self.canonical.id,
            provider="strava",
            external_id="abc",
            confidence=0.9,
            link_source="auto",
        )
        self.assertEqual(self.state.unlink_external("strava", "abc"), 1)
        self.assertEqual(self.state.unlink_external("strava", "abc"), 0)

    def test_mark_pulled_updates_timestamp(self):
        self.state.link_external(
            canonical_id=self.canonical.id,
            provider="strava",
            external_id="abc",
            confidence=0.9,
            link_source="auto",
        )
        self.state.mark_link_pulled("strava", "abc", etag="W/\"abc\"")
        link = self.state.lookup_link("strava", "abc")
        self.assertIsNotNone(link["last_pulled_at"])
        self.assertEqual(link["external_etag"], "W/\"abc\"")

    def test_mark_pushed_sets_skip_window(self):
        self.state.link_external(
            canonical_id=self.canonical.id,
            provider="strava",
            external_id="abc",
            confidence=0.9,
            link_source="auto",
        )
        self.state.mark_link_pushed("strava", "abc", push_hash="deadbeef", skip_pulls_for_seconds=60)
        link = self.state.lookup_link("strava", "abc")
        self.assertEqual(link["last_push_hash"], "deadbeef")
        self.assertIsNotNone(link["skip_pulls_until"])


if __name__ == "__main__":
    unittest.main()
