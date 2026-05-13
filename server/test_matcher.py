"""Unit tests for the Strava → Hevy workout matcher.

Run with: python -m unittest server.test_matcher  (from repo root)
        or: python -m unittest test_matcher        (from server/)

No external dependencies; uses stdlib unittest only.
"""

from __future__ import annotations

import unittest

from matcher import (
    AUTO_MERGE_THRESHOLD,
    REVIEW_THRESHOLD,
    HevyExerciseSummary,
    HevyWorkoutSummary,
    StravaSummary,
    best_match,
    score,
    summarize_hevy,
)


# Anchor every scenario at a single, plausible epoch second so debugging
# timestamp arithmetic stays sane. 2025-08-23T00:25:39Z.
T0 = 1755906339


def _strava(
    activity_type: str = "Run",
    start: int = T0,
    moving: int = 4101,
    distance: float = 10030.0,
    activity_id: str = "12345",
) -> StravaSummary:
    return StravaSummary(
        activity_id=activity_id,
        activity_type=activity_type,
        start_ts=start,
        moving_seconds=moving,
        distance_meters=distance,
    )


def _hevy(
    workout_id: str = "h1",
    start: int = T0,
    end: int | None = None,
    template_ids: tuple[str, ...] = ("AC1BB830",),
    duration: int = 4101,
    distance: float = 10030.0,
) -> HevyWorkoutSummary:
    """Build a single-exercise Hevy summary. For brick-session tests use
    `_brick_hevy` instead.

    If multiple template ids are given, total `duration`/`distance` are
    split evenly across them — this keeps simple tests legible while
    matching the new per-exercise data model.
    """
    n = max(len(template_ids), 1)
    per_dur = duration // n
    per_dist = distance / n
    exercises = tuple(
        HevyExerciseSummary(
            template_id=tid,
            duration_seconds=per_dur,
            distance_meters=per_dist,
        )
        for tid in template_ids
    )
    return HevyWorkoutSummary(
        workout_id=workout_id,
        start_ts=start,
        end_ts=end if end is not None else start + duration,
        exercises=exercises,
    )


def _brick_hevy(
    workout_id: str = "brick",
    start: int = T0,
    end: int | None = None,
    exercises: tuple[tuple[str, int, float], ...] = (),
) -> HevyWorkoutSummary:
    """Build a Hevy summary with explicit (template_id, dur, dist) per exercise."""
    total_dur = sum(d for _, d, _ in exercises)
    return HevyWorkoutSummary(
        workout_id=workout_id,
        start_ts=start,
        end_ts=end if end is not None else start + total_dur,
        exercises=tuple(
            HevyExerciseSummary(template_id=t, duration_seconds=d, distance_meters=x)
            for t, d, x in exercises
        ),
    )


class ScoreTests(unittest.TestCase):
    def test_exact_match_scores_near_one(self):
        s = score(_strava(), _hevy())
        self.assertGreaterEqual(s.score, AUTO_MERGE_THRESHOLD)
        self.assertEqual(s.type_score, 1.0)
        self.assertAlmostEqual(s.time_score, 1.0, places=3)
        self.assertAlmostEqual(s.duration_score, 1.0, places=3)
        self.assertAlmostEqual(s.distance_score, 1.0, places=3)

    def test_realistic_watch_drift_still_auto_merges(self):
        # 90 s start drift, 3% duration drift, 4% distance drift.
        s = score(
            _strava(start=T0 + 90, moving=4220, distance=10440),
            _hevy(start=T0, duration=4101, distance=10030),
        )
        self.assertGreaterEqual(
            s.score,
            AUTO_MERGE_THRESHOLD,
            f"watch-drift case should auto-merge, scored {s.score:.3f}",
        )

    def test_start_15_min_off_drops_to_review(self):
        s = score(
            _strava(start=T0 + 15 * 60),
            _hevy(start=T0),
        )
        self.assertGreaterEqual(s.score, REVIEW_THRESHOLD)
        self.assertLess(s.score, AUTO_MERGE_THRESHOLD)

    def test_start_45_min_off_falls_below_review(self):
        s = score(
            _strava(start=T0 + 45 * 60),
            _hevy(start=T0),
        )
        self.assertLess(s.score, REVIEW_THRESHOLD)

    def test_type_mismatch_zeroes_score(self):
        # Run Strava vs Hevy that only has a lifting template.
        s = score(_strava("Run"), _hevy(template_ids=("DEADLIFT_TEMPLATE",)))
        self.assertEqual(s.score, 0.0)
        self.assertEqual(s.type_score, 0.0)
        self.assertIn(
            "activity type 'Run' not in Hevy exercises",
            " ".join(s.reasons),
        )

    def test_unknown_strava_type_zeroes_score(self):
        s = score(_strava(activity_type="Yoga"), _hevy())
        self.assertEqual(s.score, 0.0)

    def test_indoor_strava_no_distance_still_matches(self):
        # Treadmill: Strava sees 0 distance, time still tracks.
        s = score(
            _strava(distance=0.0),
            _hevy(distance=0.0),
        )
        self.assertGreaterEqual(s.score, AUTO_MERGE_THRESHOLD)
        self.assertIsNone(s.distance_score)

    def test_hevy_missing_duration_drops_dimension(self):
        s = score(
            _strava(moving=4101),
            _hevy(duration=0, distance=10030),  # only distance present on Hevy
        )
        # No duration to compare; time + type + distance carry it.
        self.assertGreaterEqual(s.score, REVIEW_THRESHOLD)
        self.assertIsNone(s.duration_score)

    def test_distance_50_percent_off_scores_zero_on_that_axis(self):
        s = score(
            _strava(distance=10000),
            _hevy(distance=5000),
        )
        self.assertEqual(s.distance_score, 0.0)


class VirtualRideTests(unittest.TestCase):
    def test_virtualride_matches_generic_cycling_template(self):
        s = score(
            _strava(activity_type="VirtualRide"),
            _hevy(template_ids=("D8F7F851",)),  # generic Cycling
        )
        self.assertEqual(s.type_score, 1.0)
        self.assertGreaterEqual(s.score, AUTO_MERGE_THRESHOLD)

    def test_virtualride_matches_custom_template(self):
        s = score(
            _strava(activity_type="VirtualRide"),
            _hevy(template_ids=("89f3ed93-5418-4cc6-a114-0590f2977ae8",)),
        )
        self.assertEqual(s.type_score, 1.0)


class BrickWorkoutTests(unittest.TestCase):
    """Hevy workout containing more than one exercise template (e.g. brick)."""

    def test_run_strava_matches_brick_via_run_template(self):
        brick = _hevy(template_ids=("D8F7F851", "AC1BB830"))
        s = score(_strava("Run"), brick)
        self.assertEqual(s.type_score, 1.0)

    def test_ride_strava_matches_same_brick(self):
        brick = _hevy(template_ids=("D8F7F851", "AC1BB830"))
        s = score(_strava("Ride"), brick)
        self.assertEqual(s.type_score, 1.0)

    def test_brick_does_not_let_one_template_steal_other_total(self):
        # Scenario from phase-2 review: user marked a tiny Ride warmup
        # inside a Hevy run workout. A Strava bike-only activity (30min,
        # 15km) must NOT auto-merge into the Run-dominant brick.
        brick = _brick_hevy(
            workout_id="run-with-warmup-ride",
            start=T0,
            exercises=(
                ("D8F7F851", 300, 1000.0),    # 5-min, 1km Ride warmup
                ("AC1BB830", 1800, 5000.0),   # 30-min, 5km Run main
            ),
        )
        strava_bike = _strava(
            activity_type="Ride", moving=1800, distance=15000.0, start=T0,
        )
        s = score(strava_bike, brick)
        # Type gate passes (Ride template present), but dur / dist must
        # be compared only to the Ride exercise — not 5+30 min / 1+5 km.
        self.assertEqual(s.type_score, 1.0)
        # Ride exercise is 5min/1km vs strava 30min/15km — both ratios
        # should be terrible.
        self.assertIsNotNone(s.duration_score)
        self.assertIsNotNone(s.distance_score)
        self.assertLess(s.duration_score, 0.5)
        self.assertLess(s.distance_score, 0.5)
        self.assertLess(
            s.score,
            AUTO_MERGE_THRESHOLD,
            f"brick brought {s.score:.3f}, must not auto-merge",
        )

    def test_brick_real_match_for_corresponding_exercise(self):
        # The happy version: user did a real bike-then-run brick in one
        # Hevy workout, Strava logged just the bike. The Strava bike
        # should auto-merge against the brick's Ride exercise.
        brick = _brick_hevy(
            start=T0,
            exercises=(
                ("D8F7F851", 1800, 15000.0),  # 30-min, 15km Ride
                ("AC1BB830", 1800, 5000.0),   # 30-min, 5km Run after
            ),
        )
        strava_bike = _strava(
            activity_type="Ride", moving=1800, distance=15000.0, start=T0,
        )
        s = score(strava_bike, brick)
        self.assertGreaterEqual(s.score, AUTO_MERGE_THRESHOLD)


class BestMatchTests(unittest.TestCase):
    def test_picks_top_scorer_for_auto_merge(self):
        s = _strava()
        candidates = [
            _hevy(workout_id="far", start=T0 + 30 * 60),       # 30 min off
            _hevy(workout_id="close", start=T0 + 60),           # 1 min off
            _hevy(workout_id="medium", start=T0 + 10 * 60),    # 10 min off
        ]
        result = best_match(s, candidates)
        self.assertIsNotNone(result.auto_merge)
        self.assertEqual(result.auto_merge.workout_id, "close")

    def test_no_auto_merge_when_top_below_threshold(self):
        s = _strava(start=T0)
        candidates = [_hevy(start=T0 + 20 * 60)]  # 20 min off
        result = best_match(s, candidates)
        self.assertIsNone(result.auto_merge)
        # 20 min off with perfect duration/distance should still be reviewable.
        self.assertEqual(len(result.review), 1)

    def test_no_candidates_returns_empty_result(self):
        result = best_match(_strava(), [])
        self.assertIsNone(result.auto_merge)
        self.assertEqual(result.review, ())
        self.assertEqual(result.rejected, ())

    def test_type_incompatible_candidates_dropped_entirely(self):
        # Lifting workouts in the candidate list shouldn't even reach
        # the rejected bucket — they're not "near misses", they're wrong.
        candidates = [
            _hevy(workout_id="lifts", template_ids=("SQUAT", "BENCH")),
        ]
        result = best_match(_strava("Run"), candidates)
        self.assertEqual(result.auto_merge, None)
        self.assertEqual(result.review, ())
        self.assertEqual(result.rejected, ())

    def test_adversarial_morning_vs_evening_same_type(self):
        # User ran in the morning AND in the evening. Strava sees the
        # evening one; only that should match.
        morning = _hevy(workout_id="morning", start=T0)
        evening = _hevy(workout_id="evening", start=T0 + 8 * 3600)
        strava_evening = _strava(start=T0 + 8 * 3600 + 30)
        result = best_match(strava_evening, [morning, evening])
        self.assertIsNotNone(result.auto_merge)
        self.assertEqual(result.auto_merge.workout_id, "evening")

    def test_results_sorted_desc(self):
        s = _strava(start=T0)
        # Bracket the auto-merge threshold so the test is robust to small
        # weight tweaks: auto + review combined should be ordered by score.
        candidates = [
            _hevy(workout_id="c10", start=T0 + 10 * 60),
            _hevy(workout_id="c20", start=T0 + 20 * 60),
            _hevy(workout_id="c15", start=T0 + 15 * 60),
        ]
        result = best_match(s, candidates)
        ordered = (
            [result.auto_merge.workout_id] if result.auto_merge else []
        ) + [m.workout_id for m in result.review]
        self.assertEqual(ordered, ["c10", "c15", "c20"])


class SummarizeHevyTests(unittest.TestCase):
    """Verifies summarize_hevy handles the real payload shape."""

    WRAPPED = {
        "workout": {
            "workout_id": "abc-123",
            "title": "Morning Run",
            "start_time": T0,
            "end_time": T0 + 4101,
            "exercises": [
                {
                    "title": "Running",
                    "exercise_template_id": "AC1BB830",
                    "sets": [
                        {
                            "index": 0,
                            "type": "normal",
                            "distance_meters": 10030,
                            "duration_seconds": 4101,
                        }
                    ],
                }
            ],
        }
    }

    def test_extracts_from_wrapped_envelope(self):
        h = summarize_hevy(self.WRAPPED)
        self.assertEqual(h.workout_id, "abc-123")
        self.assertEqual(h.start_ts, T0)
        self.assertEqual(h.end_ts, T0 + 4101)
        self.assertEqual(h.exercise_template_ids, ("AC1BB830",))
        self.assertEqual(h.total_duration_seconds, 4101)
        self.assertEqual(h.total_distance_meters, 10030.0)

    def test_extracts_from_bare_workout(self):
        h = summarize_hevy(self.WRAPPED["workout"])
        self.assertEqual(h.workout_id, "abc-123")
        self.assertEqual(h.total_duration_seconds, 4101)

    def test_sums_across_multiple_sets_and_exercises(self):
        payload = {
            "workout": {
                "workout_id": "brick",
                "start_time": T0,
                "exercises": [
                    {
                        "exercise_template_id": "D8F7F851",  # Cycling
                        "sets": [
                            {"distance_meters": 20000, "duration_seconds": 1800},
                            {"distance_meters": 5000, "duration_seconds": 600},
                        ],
                    },
                    {
                        "exercise_template_id": "AC1BB830",  # Running
                        "sets": [
                            {"distance_meters": 5000, "duration_seconds": 1500},
                        ],
                    },
                ],
            }
        }
        h = summarize_hevy(payload)
        self.assertEqual(h.total_distance_meters, 30000.0)
        self.assertEqual(h.total_duration_seconds, 3900)
        self.assertEqual(set(h.exercise_template_ids), {"D8F7F851", "AC1BB830"})

    def test_end_time_defaults_to_start_plus_duration(self):
        payload = {
            "workout": {
                "workout_id": "no-end",
                "start_time": T0,
                # No end_time
                "exercises": [
                    {
                        "exercise_template_id": "AC1BB830",
                        "sets": [{"duration_seconds": 3000}],
                    }
                ],
            }
        }
        h = summarize_hevy(payload)
        self.assertEqual(h.end_ts, T0 + 3000)

    def test_handles_missing_or_null_fields(self):
        payload = {"workout": {"workout_id": "empty"}}
        h = summarize_hevy(payload)
        self.assertEqual(h.start_ts, 0)
        self.assertEqual(h.total_duration_seconds, 0)
        self.assertEqual(h.total_distance_meters, 0.0)
        self.assertEqual(h.exercise_template_ids, ())

    def test_envelope_with_none_workout_does_not_crash(self):
        with self.assertRaises(ValueError):
            # No workout_id present at all → must raise, not silently merge.
            summarize_hevy({"workout": None})

    def test_missing_workout_id_raises(self):
        with self.assertRaises(ValueError):
            summarize_hevy({"workout": {"start_time": T0}})

    def test_accepts_iso_string_timestamps(self):
        # Hevy's public v1 REST API returns ISO-8601, not epoch ints.
        payload = {
            "workout": {
                "workout_id": "iso-1",
                "start_time": "2025-08-23T00:25:39Z",
                "end_time": "2025-08-23T01:34:00Z",
                "exercises": [
                    {
                        "exercise_template_id": "AC1BB830",
                        "sets": [
                            {"distance_meters": 10030, "duration_seconds": 4101},
                        ],
                    }
                ],
            }
        }
        h = summarize_hevy(payload)
        self.assertEqual(h.start_ts, T0)  # 2025-08-23T00:25:39Z is T0 by construction
        self.assertGreater(h.end_ts, h.start_ts)

    def test_degenerate_end_before_start_is_clamped(self):
        # Real-world corruption: end_ts < start_ts. _time_score must not crash.
        h = HevyWorkoutSummary(
            workout_id="weird",
            start_ts=T0,
            end_ts=T0 - 5,
            exercises=(
                HevyExerciseSummary(
                    template_id="AC1BB830",
                    duration_seconds=0,
                    distance_meters=0.0,
                ),
            ),
        )
        s = score(_strava(distance=0.0), h)
        # Result should be a finite, non-negative float; we don't care
        # about the exact value, only that the math survived.
        self.assertTrue(0.0 <= s.score <= 1.0)

    def test_distance_with_comma_string_parses(self):
        # Some Hevy exports serialize numeric strings with commas.
        payload = {
            "workout": {
                "workout_id": "comma",
                "start_time": T0,
                "exercises": [
                    {
                        "exercise_template_id": "AC1BB830",
                        "sets": [
                            {"distance_meters": "10,030", "duration_seconds": "4101"},
                        ],
                    }
                ],
            }
        }
        h = summarize_hevy(payload)
        self.assertEqual(h.total_distance_meters, 10030.0)
        self.assertEqual(h.total_duration_seconds, 4101)

    def test_distance_garbage_string_becomes_zero(self):
        payload = {
            "workout": {
                "workout_id": "garbage",
                "start_time": T0,
                "exercises": [
                    {
                        "exercise_template_id": "AC1BB830",
                        "sets": [
                            {"distance_meters": "n/a", "duration_seconds": "?"},
                        ],
                    }
                ],
            }
        }
        h = summarize_hevy(payload)
        self.assertEqual(h.total_distance_meters, 0.0)
        self.assertEqual(h.total_duration_seconds, 0)

    def test_sets_not_a_list_is_ignored(self):
        # Defensive: if Hevy ever returns sets as a string, we should not
        # iterate its characters.
        payload = {
            "workout": {
                "workout_id": "weird-sets",
                "start_time": T0,
                "exercises": [
                    {"exercise_template_id": "AC1BB830", "sets": "oops"},
                ],
            }
        }
        h = summarize_hevy(payload)
        self.assertEqual(h.total_duration_seconds, 0)
        self.assertEqual(h.exercise_template_ids, ("AC1BB830",))

    def test_millisecond_epoch_normalized_to_seconds(self):
        # A common mistake: callers pass JS-style epoch-ms instead of secs.
        # We silently rescale rather than placing the workout in year 57580.
        payload = {
            "workout": {
                "workout_id": "ms",
                "start_time": T0 * 1000,
                "end_time": (T0 + 4101) * 1000,
                "exercises": [
                    {
                        "exercise_template_id": "AC1BB830",
                        "sets": [{"duration_seconds": 4101, "distance_meters": 10030}],
                    }
                ],
            }
        }
        h = summarize_hevy(payload)
        self.assertEqual(h.start_ts, T0)
        self.assertEqual(h.end_ts, T0 + 4101)

    def test_duplicate_template_ids_deduped(self):
        payload = {
            "workout": {
                "workout_id": "dup",
                "start_time": T0,
                "exercises": [
                    {
                        "exercise_template_id": "AC1BB830",
                        "sets": [{"duration_seconds": 1000, "distance_meters": 2000}],
                    },
                    {
                        "exercise_template_id": "AC1BB830",
                        "sets": [{"duration_seconds": 2000, "distance_meters": 3000}],
                    },
                ],
            }
        }
        h = summarize_hevy(payload)
        self.assertEqual(h.exercise_template_ids, ("AC1BB830",))
        # Both exercise blocks still contribute to totals.
        self.assertEqual(h.total_duration_seconds, 3000)
        self.assertEqual(h.total_distance_meters, 5000.0)


try:
    from strava_client import ALL_ACTIVITY_TYPES, VIRTUAL_RIDE_TYPE
    _STRAVA_CLIENT_AVAILABLE = True
except ImportError:
    _STRAVA_CLIENT_AVAILABLE = False


@unittest.skipUnless(
    _STRAVA_CLIENT_AVAILABLE, "stravalib / strava_client not importable"
)
class TypeMapParityTests(unittest.TestCase):
    """Guard against drift between matcher's type map and strava_client."""

    def test_every_activity_type_has_its_template_id(self):
        from matcher import DEFAULT_TYPE_TEMPLATE_IDS

        for at in ALL_ACTIVITY_TYPES:
            self.assertIn(
                at.type,
                DEFAULT_TYPE_TEMPLATE_IDS,
                f"{at.type} missing from matcher's type map",
            )
            self.assertIn(
                at.id,
                DEFAULT_TYPE_TEMPLATE_IDS[at.type],
                f"template id {at.id} not registered for {at.type}",
            )

    def test_virtual_ride_maps_to_custom_and_generic(self):
        from matcher import DEFAULT_TYPE_TEMPLATE_IDS

        vr_set = DEFAULT_TYPE_TEMPLATE_IDS.get(VIRTUAL_RIDE_TYPE.type)
        self.assertIsNotNone(vr_set)
        self.assertIn(VIRTUAL_RIDE_TYPE.id, vr_set)
        # Should also match the generic Cycling template so users who
        # don't have the custom template still get matched.
        ride_id = next(at.id for at in ALL_ACTIVITY_TYPES if at.type == "Ride")
        self.assertIn(ride_id, vr_set)


class IntegrationWithStravaTemplateTests(unittest.TestCase):
    """Regression: the canonical Strava→Hevy template should match itself."""

    def test_run_template_matches_its_own_source_activity(self):
        # Values mirror _RUN_TEMPLATE in strava_client.py with realistic numbers.
        strava = StravaSummary(
            activity_id="9999",
            activity_type="Run",
            start_ts=T0,
            moving_seconds=4101,
            distance_meters=10030.0,
        )
        hevy_payload = {
            "workout": {
                "workout_id": "template-id",
                "start_time": T0,
                "end_time": T0 + 4101,
                "exercises": [
                    {
                        "exercise_template_id": "AC1BB830",
                        "sets": [
                            {
                                "distance_meters": 10030,
                                "duration_seconds": 4101,
                            }
                        ],
                    }
                ],
            }
        }
        result = best_match(strava, [summarize_hevy(hevy_payload)])
        self.assertIsNotNone(result.auto_merge)
        self.assertGreaterEqual(result.auto_merge.score, 0.95)


if __name__ == "__main__":
    unittest.main()
