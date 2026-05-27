"""Unit tests for the Strava duplicate detector.

Run from server/:
    python -m unittest test_duplicate_detector
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from duplicate_detector import (
    DEFAULT_GAP_SECONDS,
    LOSER_TITLE_PREFIX,
    find_duplicate_groups,
    loser_title,
    pick_survivor,
)


T0 = datetime(2025, 8, 23, 10, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _act(
    id_: str,
    *,
    type_: str = "Run",
    start: datetime = T0,
    moving: int = 1800,
    distance: float = 5000.0,
    has_gps: bool = True,
    name: str = "Morning run",
) -> dict:
    return {
        "id": id_,
        "type": type_,
        "name": name,
        "start_date": _iso(start),
        "moving_time": moving,
        "distance": distance,
        "has_gps": has_gps,
    }


class FindDuplicateGroupsTests(unittest.TestCase):
    def test_two_overlapping_same_type_group(self):
        a = _act("a", start=T0, moving=1800)
        # Starts 5 min in, runs another 30 min — clearly overlapping.
        b = _act("b", start=T0 + timedelta(minutes=5), moving=1800)
        groups = find_duplicate_groups([a, b])
        self.assertEqual(len(groups), 1)
        self.assertEqual({x["id"] for x in groups[0]}, {"a", "b"})

    def test_sequential_within_15min_gap_groups(self):
        a = _act("a", start=T0, moving=600)  # ends at T0+10min
        # 10 min later — well within the 15 min gap.
        b = _act("b", start=T0 + timedelta(minutes=20), moving=600)
        groups = find_duplicate_groups([a, b])
        self.assertEqual(len(groups), 1)

    def test_sequential_exactly_at_gap_boundary_groups(self):
        a = _act("a", start=T0, moving=600)  # ends T0+10min
        b = _act("b", start=T0 + timedelta(minutes=25), moving=600)  # 15 min gap exactly
        groups = find_duplicate_groups([a, b])
        self.assertEqual(len(groups), 1, "boundary should be inclusive")

    def test_gap_over_15min_does_not_group(self):
        a = _act("a", start=T0, moving=600)  # ends T0+10min
        # 16 min gap — outside the window.
        b = _act("b", start=T0 + timedelta(minutes=26), moving=600)
        groups = find_duplicate_groups([a, b])
        self.assertEqual(groups, [])

    def test_different_types_never_group(self):
        a = _act("a", type_="Run", start=T0)
        b = _act("b", type_="Ride", start=T0)
        groups = find_duplicate_groups([a, b])
        self.assertEqual(groups, [])

    def test_three_activity_chain_collapses_to_one_group(self):
        a = _act("a", start=T0, moving=600)                       # 10:00–10:10
        b = _act("b", start=T0 + timedelta(minutes=20), moving=600)  # 10:20–10:30
        c = _act("c", start=T0 + timedelta(minutes=40), moving=600)  # 10:40–10:50
        groups = find_duplicate_groups([a, b, c])
        self.assertEqual(len(groups), 1)
        self.assertEqual({x["id"] for x in groups[0]}, {"a", "b", "c"})

    def test_unrelated_singletons_are_filtered_out(self):
        a = _act("a", start=T0, moving=1800)
        b = _act("b", start=T0 + timedelta(hours=6), moving=1800)
        c = _act("c", type_="Ride", start=T0, moving=1800)
        self.assertEqual(find_duplicate_groups([a, b, c]), [])

    def test_input_order_does_not_change_grouping(self):
        a = _act("a", start=T0, moving=1800)
        b = _act("b", start=T0 + timedelta(minutes=5), moving=1800)
        forward = find_duplicate_groups([a, b])
        reverse = find_duplicate_groups([b, a])
        self.assertEqual(
            [{x["id"] for x in g} for g in forward],
            [{x["id"] for x in g} for g in reverse],
        )

    def test_custom_gap_seconds_respected(self):
        a = _act("a", start=T0, moving=600)
        b = _act("b", start=T0 + timedelta(minutes=11), moving=600)  # 1 min gap
        # With a 30s gap window, they should not group.
        self.assertEqual(find_duplicate_groups([a, b], gap_seconds=30), [])
        # With default (15min) they do.
        self.assertEqual(
            len(find_duplicate_groups([a, b], gap_seconds=DEFAULT_GAP_SECONDS)),
            1,
        )


class PickSurvivorTests(unittest.TestCase):
    def test_gps_beats_no_gps_even_when_no_gps_has_more_distance(self):
        # The watch died (10 min/2km) but had GPS; phone backup recorded
        # the rest without GPS (30 min/manual entry, 0 distance because no
        # GPS — pretend the phone entry has larger distance for the worst
        # case). Survivor should still be the GPS one.
        gps_short = _act(
            "watch", moving=600, distance=2000.0, has_gps=True, name="Watch run"
        )
        no_gps_long = _act(
            "phone", moving=1800, distance=8000.0, has_gps=False, name="Phone run"
        )
        self.assertEqual(pick_survivor([gps_short, no_gps_long])["id"], "watch")

    def test_more_distance_wins_among_gps(self):
        short = _act("a", distance=3000.0, moving=900, has_gps=True)
        long_ = _act("b", distance=6000.0, moving=900, has_gps=True)
        self.assertEqual(pick_survivor([short, long_])["id"], "b")

    def test_longer_moving_time_breaks_distance_tie(self):
        a = _act("a", distance=5000.0, moving=1500, has_gps=True)
        b = _act("b", distance=5000.0, moving=2400, has_gps=True)
        self.assertEqual(pick_survivor([a, b])["id"], "b")

    def test_earlier_start_breaks_remaining_ties(self):
        a = _act("a", start=T0 + timedelta(minutes=10), has_gps=True)
        b = _act("b", start=T0, has_gps=True)
        self.assertEqual(pick_survivor([a, b])["id"], "b")

    def test_id_is_final_deterministic_tiebreak(self):
        a = _act("a", has_gps=True)
        b = _act("b", has_gps=True)
        self.assertEqual(pick_survivor([a, b])["id"], "a")
        self.assertEqual(pick_survivor([b, a])["id"], "a")

    def test_empty_group_raises(self):
        with self.assertRaises(ValueError):
            pick_survivor([])


class LoserTitleTests(unittest.TestCase):
    def test_prefixes_original(self):
        self.assertEqual(loser_title("Morning run"), "[merged] Morning run")

    def test_idempotent_does_not_double_prefix(self):
        once = loser_title("Morning run")
        twice = loser_title(once)
        self.assertEqual(once, twice)

    def test_empty_or_none_falls_back_to_prefix_only(self):
        self.assertEqual(loser_title(None), LOSER_TITLE_PREFIX.strip())
        self.assertEqual(loser_title(""), LOSER_TITLE_PREFIX.strip())

    def test_whitespace_only_treated_as_empty(self):
        self.assertEqual(loser_title("   "), LOSER_TITLE_PREFIX.strip())


class GroupOrderingTests(unittest.TestCase):
    def test_groups_sorted_by_size_descending(self):
        # One group of 3, one group of 2.
        big = [
            _act("a", start=T0, moving=600, type_="Run"),
            _act("b", start=T0 + timedelta(minutes=20), moving=600, type_="Run"),
            _act("c", start=T0 + timedelta(minutes=40), moving=600, type_="Run"),
        ]
        small = [
            _act("d", start=T0 + timedelta(hours=4), moving=600, type_="Ride"),
            _act("e", start=T0 + timedelta(hours=4, minutes=20), moving=600, type_="Ride"),
        ]
        groups = find_duplicate_groups(big + small)
        self.assertEqual(len(groups), 2)
        self.assertEqual(len(groups[0]), 3)
        self.assertEqual(len(groups[1]), 2)


if __name__ == "__main__":
    unittest.main()
