"""Unit tests for the canonical model and merge policy.

Pure-logic tests; no SQLite or network. Run from server/:
    python -m unittest test_canonical
"""

from __future__ import annotations

import unittest

from canonical import (
    CanonicalActivity,
    CanonicalPatch,
    FieldValue,
    HRSample,
    MergePolicy,
    PowerSample,
    PreferProvider,
    apply_patch,
    should_overwrite,
)


class FieldValueTests(unittest.TestCase):
    def test_to_jsonable_round_trips(self):
        fv = FieldValue(value="hello", source="strava", set_at=1700, locked=True)
        out = fv.to_jsonable()
        self.assertEqual(out, {"value": "hello", "source": "strava", "set_at": 1700, "locked": True})
        back = FieldValue.from_jsonable(out)
        self.assertEqual(back, fv)

    def test_from_jsonable_none(self):
        self.assertIsNone(FieldValue.from_jsonable(None))


class ShouldOverwriteTests(unittest.TestCase):
    def _fv(self, value, source="strava", set_at=1000, locked=False):
        return FieldValue(value=value, source=source, set_at=set_at, locked=locked)

    def test_none_existing_always_overwrites(self):
        self.assertTrue(
            should_overwrite(None, self._fv("x"), MergePolicy.FIRST_WRITER_WINS)
        )

    def test_locked_never_overwrites(self):
        existing = self._fv("locked-val", locked=True)
        incoming = self._fv("new", source="hevy")
        self.assertFalse(
            should_overwrite(existing, incoming, MergePolicy.MOST_RECENT_WINS)
        )
        self.assertFalse(
            should_overwrite(existing, incoming, PreferProvider("hevy"))
        )

    def test_first_writer_wins_keeps_existing_when_set(self):
        existing = self._fv("Run")
        incoming = self._fv("Ride", source="hevy", set_at=2000)
        self.assertFalse(
            should_overwrite(existing, incoming, MergePolicy.FIRST_WRITER_WINS)
        )

    def test_most_recent_wins(self):
        existing = self._fv("Garmin Forerunner", source="garmin", set_at=1000)
        newer = self._fv("Apple Watch", source="strava", set_at=2000)
        older = self._fv("Polar", source="strava", set_at=500)
        self.assertTrue(
            should_overwrite(existing, newer, MergePolicy.MOST_RECENT_WINS)
        )
        self.assertFalse(
            should_overwrite(existing, older, MergePolicy.MOST_RECENT_WINS)
        )

    def test_prefer_specific_only_fills_nulls(self):
        nulled = self._fv(None)
        incoming = self._fv(280)
        self.assertTrue(
            should_overwrite(nulled, incoming, MergePolicy.PREFER_SPECIFIC_OVER_NULL)
        )
        set_already = self._fv(280)
        bigger = self._fv(310)
        self.assertFalse(
            should_overwrite(set_already, bigger, MergePolicy.PREFER_SPECIFIC_OVER_NULL)
        )

    def test_prefer_provider_lets_owner_update_itself(self):
        existing = self._fv("Hevy title", source="hevy")
        new_hevy = self._fv("Hevy title v2", source="hevy", set_at=2000)
        self.assertTrue(
            should_overwrite(existing, new_hevy, PreferProvider("hevy"))
        )

    def test_prefer_provider_blocks_non_owner_overwrite(self):
        existing = self._fv("Hevy title", source="hevy")
        from_strava = self._fv("Strava name", source="strava", set_at=9999)
        self.assertFalse(
            should_overwrite(existing, from_strava, PreferProvider("hevy"))
        )

    def test_prefer_provider_lets_non_owner_fill_null(self):
        existing = self._fv(None, source="hevy")
        from_strava = self._fv("Strava name", source="strava")
        self.assertTrue(
            should_overwrite(existing, from_strava, PreferProvider("hevy"))
        )

    def test_additive_policy_raises_if_misused(self):
        with self.assertRaises(ValueError):
            should_overwrite(
                self._fv(1), self._fv(2), MergePolicy.ADDITIVE
            )

    def test_unknown_policy_raises(self):
        with self.assertRaises(ValueError):
            should_overwrite(self._fv(1), self._fv(2), "nonsense")


class ApplyPatchTests(unittest.TestCase):
    def _canonical(self):
        return CanonicalActivity.new("Run", start_ts=1000, end_ts=2000)

    def test_first_patch_populates_fields(self):
        c = self._canonical()
        patch = CanonicalPatch(
            title=FieldValue("Morning run", "strava", 1500),
            distance_meters=FieldValue(10000.0, "strava", 1500),
        )
        changed = apply_patch(c, patch)
        self.assertTrue(changed)
        self.assertEqual(c.title.value, "Morning run")
        self.assertEqual(c.distance_meters.value, 10000.0)

    def test_hevy_overrides_strava_on_title(self):
        c = self._canonical()
        apply_patch(c, CanonicalPatch(title=FieldValue("Strava name", "strava", 1500)))
        apply_patch(c, CanonicalPatch(title=FieldValue("Hevy name", "hevy", 1600)))
        self.assertEqual(c.title.value, "Hevy name")
        self.assertEqual(c.title.source, "hevy")

    def test_strava_cannot_overwrite_hevy_title(self):
        c = self._canonical()
        apply_patch(c, CanonicalPatch(title=FieldValue("Hevy name", "hevy", 1500)))
        apply_patch(c, CanonicalPatch(title=FieldValue("Strava name", "strava", 2000)))
        self.assertEqual(c.title.value, "Hevy name")

    def test_locked_field_blocks_owner(self):
        c = self._canonical()
        c.title = FieldValue("User title", "user", 1000, locked=True)
        apply_patch(c, CanonicalPatch(title=FieldValue("Hevy name", "hevy", 2000)))
        self.assertEqual(c.title.value, "User title")
        self.assertTrue(c.title.locked)

    def test_hr_samples_union_by_lineage(self):
        c = self._canonical()
        s1 = HRSample(timestamp_ms=1000, bpm=120, source_provider="strava", source_external_id="A")
        s2 = HRSample(timestamp_ms=2000, bpm=130, source_provider="strava", source_external_id="A")
        apply_patch(c, CanonicalPatch(hr_samples=(s1, s2)))
        self.assertEqual(len(c.hr_samples), 2)

        s2_dup = HRSample(timestamp_ms=2000, bpm=130, source_provider="strava", source_external_id="A")
        s3 = HRSample(timestamp_ms=3000, bpm=140, source_provider="strava", source_external_id="A")
        apply_patch(c, CanonicalPatch(hr_samples=(s2_dup, s3)))
        self.assertEqual(len(c.hr_samples), 3)

    def test_hr_samples_keep_different_lineages_separate(self):
        # A brick workout: two Strava activities both contributing
        # samples at the same timestamp. They are *not* duplicates.
        c = self._canonical()
        s_ride = HRSample(timestamp_ms=1000, bpm=120, source_provider="strava", source_external_id="ride")
        s_run = HRSample(timestamp_ms=1000, bpm=160, source_provider="strava", source_external_id="run")
        apply_patch(c, CanonicalPatch(hr_samples=(s_ride, s_run)))
        self.assertEqual(len(c.hr_samples), 2)

    def test_provenance_shortcut_is_noop(self):
        # Re-applying the same patch from the same source shouldn't
        # ratchet updated_at forward. This is the echo-loop guard.
        c = self._canonical()
        original_updated = c.updated_at
        apply_patch(c, CanonicalPatch(title=FieldValue("Same", "strava", 1500)))
        after_first = c.updated_at
        same_again = CanonicalPatch(title=FieldValue("Same", "strava", 1500))
        changed = apply_patch(c, same_again)
        self.assertFalse(changed)
        self.assertEqual(c.updated_at, after_first)
        _ = original_updated  # silence unused

    def test_power_samples_round_trip(self):
        c = self._canonical()
        p = PowerSample(timestamp_ms=1000, watts=250, source_provider="strava", source_external_id="A")
        apply_patch(c, CanonicalPatch(power_samples=(p,)))
        self.assertEqual(len(c.power_samples), 1)


class SerializationTests(unittest.TestCase):
    def test_canonical_round_trip_through_jsonable(self):
        c = CanonicalActivity.new("Ride", start_ts=1000, end_ts=4000)
        c.title = FieldValue("Test ride", "strava", 1500)
        c.distance_meters = FieldValue(20000.0, "strava", 1500)
        c.hr_samples.append(
            HRSample(timestamp_ms=1500, bpm=140, source_provider="strava", source_external_id="A")
        )
        blob = c.to_jsonable()
        rebuilt = CanonicalActivity.from_jsonable(blob)
        self.assertEqual(rebuilt.id, c.id)
        self.assertEqual(rebuilt.activity_type, "Ride")
        self.assertEqual(rebuilt.title.value, "Test ride")
        self.assertEqual(rebuilt.distance_meters.value, 20000.0)
        self.assertEqual(len(rebuilt.hr_samples), 1)
        self.assertEqual(rebuilt.hr_samples[0].bpm, 140)

    def test_canonical_with_no_fields_round_trips(self):
        c = CanonicalActivity.new("Walk", start_ts=1000, end_ts=2000)
        rebuilt = CanonicalActivity.from_jsonable(c.to_jsonable())
        self.assertIsNone(rebuilt.title)
        self.assertEqual(rebuilt.hr_samples, [])


if __name__ == "__main__":
    unittest.main()
