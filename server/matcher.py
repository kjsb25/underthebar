"""Match a Strava activity to existing Hevy workouts.

When the user logs the same training session in both Strava and Hevy we want to
merge HR / GPS streams from Strava into the Hevy workout additively, without
creating a duplicate. This module is the matching half — given a Strava
activity and a candidate set of Hevy workouts on the same day, score each
candidate and split them into auto-merge / user-review / rejected buckets.

The merge half (mutating a Hevy workout) lives elsewhere; this module is pure.

Scoring dimensions
------------------
type      Hard filter. Strava activity type must map to an exercise template
          present in the Hevy workout (see ALL_ACTIVITY_TYPES in
          strava_client.py). Score 0 short-circuits everything.

time      Dominant post-gate signal. Combines two views:
          - start_score: linear decay from |Δstart| = 0 (1.0) to 30 min (0.0).
          - iou: intersection-over-union of the two time intervals.
          We take max(start_score, iou) — either signal alone is plenty.

duration  Σ(set.duration_seconds) vs activity.moving_time. Ratio of
          min/max mapped through (r - 0.5) * 2, so a 50% match scores 0
          and an exact match scores 1.0. Skipped if either side is 0.

distance  Σ(set.distance_meters) vs activity.distance. Same shape as
          duration. Skipped if either side is 0 (e.g. treadmill, missing
          GPS).

Skipped dimensions are dropped from the weighted average rather than counted
as 1.0, so we don't artificially inflate the score when data is missing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable


AUTO_MERGE_THRESHOLD = 0.85
REVIEW_THRESHOLD = 0.60

# Post-type-gate weights. Sum is irrelevant — we renormalize over the
# dimensions actually present.
_WEIGHTS = {
    "time": 0.60,
    "duration": 0.25,
    "distance": 0.15,
}

# Strava activity types are matched against a Hevy workout by checking that
# the workout contains at least one exercise whose template id matches one
# of the ids returned for that type. The map mirrors
# strava_client.ALL_ACTIVITY_TYPES plus the optional VirtualRide alias.
DEFAULT_TYPE_TEMPLATE_IDS: dict[str, set[str]] = {
    "Run": {"AC1BB830"},
    "Ride": {"D8F7F851"},
    "Walk": {"33EDD7DB"},
    "Hike": {"1C34A172"},
    "VirtualRide": {
        "D8F7F851",
        "89f3ed93-5418-4cc6-a114-0590f2977ae8",
    },
}

# Time scoring: linear decay over a 30-minute window centered on Δstart=0.
# Anything beyond this is considered a different workout.
_TIME_CUTOFF_SECONDS = 30 * 60


@dataclass(frozen=True)
class StravaSummary:
    """The subset of a Strava activity needed for matching."""

    activity_id: str
    activity_type: str
    start_ts: int             # UTC epoch seconds
    moving_seconds: int       # 0 if unknown
    distance_meters: float    # 0 if unknown / indoor


@dataclass(frozen=True)
class HevyExerciseSummary:
    """Per-exercise totals so brick workouts can be scored honestly.

    Without this split, a Hevy workout containing both a Ride warmup and a
    Run main set would have its totals bleed across activity types — a
    Strava bike activity could match against duration/distance that
    actually belong to the run. We keep each exercise separate and let
    `score()` filter to the ones that match the Strava activity type.
    """

    template_id: str
    duration_seconds: int
    distance_meters: float


@dataclass(frozen=True)
class HevyWorkoutSummary:
    """The subset of a Hevy workout needed for matching."""

    workout_id: str
    start_ts: int             # UTC epoch seconds
    end_ts: int               # UTC epoch seconds
    exercises: tuple[HevyExerciseSummary, ...]

    @property
    def exercise_template_ids(self) -> tuple[str, ...]:
        # De-duplicated so a template appearing twice in a brick session
        # doesn't bias anything downstream.
        return tuple(dict.fromkeys(e.template_id for e in self.exercises))

    @property
    def total_duration_seconds(self) -> int:
        return sum(e.duration_seconds for e in self.exercises)

    @property
    def total_distance_meters(self) -> float:
        return sum(e.distance_meters for e in self.exercises)


@dataclass(frozen=True)
class MatchScore:
    workout_id: str
    score: float
    type_score: float
    time_score: float
    duration_score: float | None
    distance_score: float | None
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class MatchResult:
    """Bucketed candidates for a single Strava activity.

    auto_merge   highest-scoring candidate at or above AUTO_MERGE_THRESHOLD,
                 or None if no candidate qualifies.
    review       candidates with score in [REVIEW_THRESHOLD, AUTO_MERGE_THRESHOLD),
                 sorted by score desc. Show in a confirmation UI.
    rejected     candidates with non-zero type compatibility but score
                 below REVIEW_THRESHOLD. Useful for debugging and the dry-run
                 logging mode; do not surface to users.
    """

    auto_merge: MatchScore | None
    review: tuple[MatchScore, ...]
    rejected: tuple[MatchScore, ...]


def _to_epoch(value: Any) -> int:
    """Coerce int / float / ISO-8601 string to UTC epoch seconds.

    Hevy's internal app API returns Unix seconds for `start_time`/`end_time`
    but the public v1 REST API returns ISO-8601 strings. We accept both so
    summarize_hevy doesn't crash depending on which payload it's fed.
    """
    if value is None or value == "":
        return 0
    if isinstance(value, bool):  # bool is an int subclass — guard explicitly
        return 0
    if isinstance(value, (int, float)):
        v = int(value)
        # Heuristic: anything past ~year 2286 in seconds (10^10) is almost
        # certainly an epoch-millisecond value being passed by mistake.
        # Year 2286 in seconds = 9_999_999_999.
        if v > 10_000_000_000:
            v //= 1000
        return v
    if isinstance(value, str):
        # `fromisoformat` doesn't accept the trailing-Z shorthand; substitute
        # the explicit offset. A naive datetime (no zone info at all) is
        # assumed to be UTC — anything else would tie us to the host's local
        # timezone, which is exactly the kind of bug we don't want.
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return 0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    return 0


def summarize_hevy(workout_json: dict) -> HevyWorkoutSummary:
    """Extract matching-relevant fields from a raw Hevy workout payload.

    Tolerates both the API-shaped envelope ({"workout": {...}, ...}) and a
    bare workout dict so callers don't need to know which they have. Also
    tolerates the envelope having a None `workout` value (some malformed
    Hevy responses) — treats it as an empty payload.

    Raises ValueError if the payload has no usable workout_id, so callers
    don't silently merge into an empty-string row.
    """
    w = workout_json.get("workout") if "workout" in workout_json else workout_json
    if not w:
        w = {}

    exercises: list[HevyExerciseSummary] = []
    for ex in w.get("exercises") or []:
        if not isinstance(ex, dict):
            continue
        tid = ex.get("exercise_template_id")
        if not tid:
            continue
        ex_dur = 0
        ex_dist = 0.0
        sets_raw = ex.get("sets")
        if isinstance(sets_raw, list):
            for s in sets_raw:
                if not isinstance(s, dict):
                    continue
                ex_dur += _safe_int(s.get("duration_seconds"))
                ex_dist += _safe_float(s.get("distance_meters"))
        exercises.append(
            HevyExerciseSummary(
                template_id=str(tid),
                duration_seconds=ex_dur,
                distance_meters=ex_dist,
            )
        )

    total_dur = sum(e.duration_seconds for e in exercises)
    start_ts = _to_epoch(w.get("start_time"))
    end_ts = _to_epoch(w.get("end_time")) or (start_ts + total_dur)

    workout_id = str(w.get("workout_id") or "")
    if not workout_id:
        raise ValueError("Hevy workout payload is missing workout_id")

    return HevyWorkoutSummary(
        workout_id=workout_id,
        start_ts=start_ts,
        end_ts=end_ts,
        exercises=tuple(exercises),
    )


def _safe_int(value: Any) -> int:
    """Best-effort int coercion. Returns 0 on anything weird."""
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0


def _safe_float(value: Any) -> float:
    """Best-effort float coercion. Returns 0.0 on anything weird, including
    comma-formatted strings like '10,030' that float() rejects outright."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        if isinstance(value, str):
            try:
                return float(value.replace(",", ""))
            except ValueError:
                return 0.0
        return 0.0


def _type_score(
    strava: StravaSummary,
    hevy: HevyWorkoutSummary,
    type_map: dict[str, set[str]],
) -> float:
    expected = type_map.get(strava.activity_type)
    if not expected:
        return 0.0
    return 1.0 if any(t in expected for t in hevy.exercise_template_ids) else 0.0


def _time_score(strava: StravaSummary, hevy: HevyWorkoutSummary) -> float:
    start_diff = abs(strava.start_ts - hevy.start_ts)
    start_score = max(0.0, 1.0 - start_diff / _TIME_CUTOFF_SECONDS)

    strava_end = strava.start_ts + max(strava.moving_seconds, 0)
    interval_start = max(strava.start_ts, hevy.start_ts)
    interval_end = min(strava_end, hevy.end_ts)
    overlap = max(0, interval_end - interval_start)
    union_end = max(strava_end, hevy.end_ts)
    union_start = min(strava.start_ts, hevy.start_ts)
    union = max(1, union_end - union_start)
    iou = overlap / union

    return max(start_score, iou)


def _ratio_score(a: float, b: float) -> float | None:
    """Symmetric similarity in [0, 1]. None if either input is non-positive
    or non-finite (NaN/inf), which we treat the same way: missing data,
    drop the dimension rather than poison the weighted sum."""
    if not (math.isfinite(a) and math.isfinite(b)):
        return None
    if a <= 0 or b <= 0:
        return None
    ratio = min(a, b) / max(a, b)
    # Below a 50% ratio we treat the two as unrelated.
    return max(0.0, (ratio - 0.5) * 2.0)


def score(
    strava: StravaSummary,
    hevy: HevyWorkoutSummary,
    type_map: dict[str, set[str]] | None = None,
) -> MatchScore:
    """Score a single (Strava, Hevy) pair."""
    type_map = type_map or DEFAULT_TYPE_TEMPLATE_IDS
    reasons: list[str] = []

    ts = _type_score(strava, hevy, type_map)
    if ts == 0.0:
        reasons.append(
            f"activity type {strava.activity_type!r} not in Hevy exercises"
        )
        return MatchScore(
            workout_id=hevy.workout_id,
            score=0.0,
            type_score=0.0,
            time_score=0.0,
            duration_score=None,
            distance_score=None,
            reasons=tuple(reasons),
        )

    time_s = _time_score(strava, hevy)

    # Brick-session guard: when a Hevy workout has multiple exercises with
    # different templates (e.g. Ride warmup + Run main), score duration /
    # distance only against the exercise(s) whose template matches the
    # Strava activity type. Otherwise the totals bleed across types and a
    # bike Strava can erroneously match a workout dominated by running.
    expected_ids = type_map.get(strava.activity_type, set())
    matching = [e for e in hevy.exercises if e.template_id in expected_ids]
    matching_dur = sum(e.duration_seconds for e in matching)
    matching_dist = sum(e.distance_meters for e in matching)

    dur_s = _ratio_score(strava.moving_seconds, matching_dur)
    dist_s = _ratio_score(strava.distance_meters, matching_dist)

    components: dict[str, float] = {"time": time_s}
    if dur_s is not None:
        components["duration"] = dur_s
    else:
        reasons.append("duration missing on one side")
    if dist_s is not None:
        components["distance"] = dist_s
    else:
        reasons.append("distance missing on one side (indoor / no GPS?)")

    total_weight = sum(_WEIGHTS[k] for k in components)
    weighted = sum(components[k] * _WEIGHTS[k] for k in components)
    final = weighted / total_weight if total_weight > 0 else 0.0
    # Clamp away FP-accumulation drift so the value fits SQLite's CHECK
    # constraint on `merged_workouts.confidence`.
    final = min(1.0, max(0.0, final))

    start_diff_min = abs(strava.start_ts - hevy.start_ts) / 60
    reasons.append(f"start offset {start_diff_min:.1f} min")

    return MatchScore(
        workout_id=hevy.workout_id,
        score=final,
        type_score=ts,
        time_score=time_s,
        duration_score=dur_s,
        distance_score=dist_s,
        reasons=tuple(reasons),
    )


def best_match(
    strava: StravaSummary,
    candidates: Iterable[HevyWorkoutSummary],
    type_map: dict[str, set[str]] | None = None,
) -> MatchResult:
    """Score every candidate, bucket by threshold, return a MatchResult.

    The auto_merge bucket holds at most one candidate (the top scorer above
    AUTO_MERGE_THRESHOLD). The review bucket holds the rest above
    REVIEW_THRESHOLD, sorted desc. Rejected holds anything else that at
    least passed the type gate.
    """
    scored: list[MatchScore] = []
    rejected: list[MatchScore] = []

    for hevy in candidates:
        s = score(strava, hevy, type_map=type_map)
        if s.type_score == 0.0:
            # Type-gated out; not interesting even for debugging callers.
            continue
        if s.score < REVIEW_THRESHOLD:
            rejected.append(s)
        else:
            scored.append(s)

    scored.sort(key=lambda m: m.score, reverse=True)

    auto: MatchScore | None = None
    review: list[MatchScore] = []
    if scored and scored[0].score >= AUTO_MERGE_THRESHOLD:
        auto = scored[0]
        review = scored[1:]
    else:
        review = scored

    return MatchResult(
        auto_merge=auto,
        review=tuple(review),
        rejected=tuple(sorted(rejected, key=lambda m: m.score, reverse=True)),
    )
