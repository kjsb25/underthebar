# Workout matching: design notes

> Reference document for the Strava ↔ Hevy workout matcher
> (`server/matcher.py`, `server/state.py:merged_workouts`). Read this before
> changing weights, thresholds, the per-exercise summary shape, or the
> merge-tracking schema. It records *why* the code looks the way it does so
> the next iteration doesn't relearn the same lessons.

---

## 1. Problem we're solving

A user logs the same training session into **both** Strava and Hevy:

- Strava (watch / phone): GPS route, heart-rate stream, pace, power.
- Hevy (app): title, description, rich set/rep structure, RPE, images,
  notes — also lifting work in the same session if any.

Today, `import_activity` (`server/strava_client.py:173`) imports a Strava
activity by *creating a new Hevy workout* with a deterministic UUID seeded
from `start_time`. If the user already logged the workout in Hevy, this
creates a near-duplicate that the user has to merge by hand.

We want to detect that the Strava activity and the existing Hevy workout
represent the **same training session**, then merge the Strava-only fields
(HR samples, GPS, calories) into the Hevy record additively — without
touching anything Hevy already owns.

This document is about the **detection half** only. The mutating writer
(GET → patch biometrics + fenced notes marker → PUT) is a separate
concern, deliberately out of scope here.

## 2. Invariants we will never violate

These are non-negotiable; every design choice below is in service of them.

1. **Hevy is the authority** for everything except biometrics. Title,
   description, sets, exercises, images, RPE, notes outside of explicit
   merge markers — never touched by the merge writer.
2. **Additive only.** The writer can add a biometrics block, append HR
   summary lines to notes (inside a fenced marker), but cannot remove or
   overwrite existing Hevy data.
3. **Auto-merge bias is *conservative***. Wrong auto-merges write to
   Hevy and surprise the user. Missed matches just leave the duplicate
   workflow in place. Cost of false-positive ≫ cost of false-negative, so
   the threshold sits where most legitimate matches still need a confident
   signal.
4. **User overrides are sticky.** Once a user manually confirms (or
   manually rejects) a merge, automation cannot silently flip it back.
5. **Idempotent.** Re-running the matcher on the same data produces the
   same buckets and the same persisted state.

## 3. Available data on each side

### Strava activity

From the stravalib `Activity` object (see `server/strava_client.py:160-167`
and `server/strava_client.py:203-247`):

| Field | Type | Notes |
|---|---|---|
| `id` | int | Unique within Strava |
| `type` | str | "Run", "Ride", "Walk", "Hike", "VirtualRide", … |
| `start_date` | tz-aware datetime, UTC | Always reliable |
| `moving_time` | int seconds | Excludes pauses |
| `distance` | float meters | 0 for indoor where GPS is off |
| HR streams | optional | `time[]`, `heartrate[]` arrays |
| Power streams | optional | If a power meter was paired |

### Hevy workout

From the Hevy v2 API payload (see `_RUN_TEMPLATE` in
`server/strava_client.py:278-310` and the local JSON files described in
`hevy_api.py:407-529`):

| Field | Type | Notes |
|---|---|---|
| `workout_id` | UUID str | Stable |
| `start_time` | int epoch seconds (internal) OR ISO-8601 str (public v1) | **Both shapes exist** — `_to_epoch` handles both. |
| `end_time` | same | Optional |
| `exercises[]` | list | Multiple exercises possible (brick session, gym + cardio combo) |
| `exercises[].exercise_template_id` | str | Stable identifier per exercise type |
| `exercises[].sets[].duration_seconds` | int | Per-set, summed for cardio |
| `exercises[].sets[].distance_meters` | int/float | Per-set |
| `biometrics.heart_rate_samples[]` | list | What we're trying to *write* into |

The fact that **Hevy can carry multiple exercises in one workout** drives
the brick-workout logic in section 7.

## 4. Signals we score against, and why

We score every (Strava activity, Hevy candidate) pair on four axes. They
were chosen so that **each addresses a distinct kind of "could this be the
same session?" evidence**, and so that a missing axis (indoor treadmill,
gym-only workout, etc.) doesn't poison the others.

### 4.1 Activity type — a *hard gate*, not a weighted axis

A Strava "Run" can never be the same session as a Hevy workout that
contains no Running exercise. There is no gradient here; either the type
maps in, or it doesn't.

```
Strava type        → Hevy exercise_template_id(s)
─────────────────────────────────────────────────
Run                → AC1BB830
Ride               → D8F7F851
Walk               → 33EDD7DB
Hike               → 1C34A172
VirtualRide        → D8F7F851  AND  89f3ed93-…  (user-custom virtual ride)
```

Source of truth: `ALL_ACTIVITY_TYPES` in `server/strava_client.py:33` plus
the `VIRTUAL_RIDE_TYPE` alias at line 41. The matcher mirrors this in
`DEFAULT_TYPE_TEMPLATE_IDS` (`matcher.py:56`).

`VirtualRide` maps to **both** the generic Cycling template and the
custom virtual-ride template because not every user will have the custom
template configured.

A parity test (`test_matcher.py:TypeMapParityTests`) guards against drift
between the two source-of-truth lists.

If type matches: the candidate proceeds, the type axis contributes
nothing further to the score (it would just be a constant 1.0 and inflate
everything equally).

If type does not match: the candidate's overall score is 0 with a reason
explaining why. It never reaches the rejected bucket either — it's not
even a near-miss.

### 4.2 Time — the dominant signal (60% of post-gate weight)

Two complementary views of "did these start at roughly the same moment?":

**Start proximity** — Linear decay from `|Δstart| = 0` (score 1.0) to
`|Δstart| = 30 min` (score 0.0). 30 min is generous; chosen because a
user who starts Strava on their watch and then walks to the gym before
hitting start on Hevy can easily produce a 10–20 min offset.

**Interval IoU** — Intersection-over-union of `[strava.start, strava.start
+ moving_time]` and `[hevy.start, hevy.end]`. Robust to one side starting
earlier but both running over the same window.

We take **`max(start_proximity, iou)`**. Either signal alone is plenty;
combining via `max` means a high-IoU pair with a slightly off start still
scores well, and a tight-start pair with a partial interval overlap also
scores well. The two signals catch different failure modes.

**Why time dominates**: of all the signals, simultaneous occurrence is
the strongest evidence of "same event." Two activities of the same type
on the same day can have *very* similar duration and distance just by
coincidence (a regular 5k loop). Two activities that start within a
minute of each other almost never coincide accidentally.

### 4.3 Duration — secondary (25% of post-gate weight)

Sum of `sets[].duration_seconds` for the **matching-template exercises
only** (see section 7) vs. Strava `moving_time`.

Symmetric ratio: `r = min(a,b) / max(a,b)`, then mapped through
`(r - 0.5) * 2` and clamped to ≥ 0. A 100% match scores 1.0, a 50% match
scores 0.0. Anything below 50% is considered unrelated rather than
"weakly related."

The 50% floor is a choice. Strava's `moving_time` excludes pauses, Hevy
may or may not depending on the user's workflow — so 70-80% ratios are
common even for genuine matches. We don't want a perfectly legitimate
auto-pause discrepancy to drag a real match below the review threshold,
so the floor sits well below the typical drift band.

Skipped (no contribution to the weighted sum) when either side reports 0,
which happens with treadmills (Strava distance=0 but duration valid),
gym-only workouts on the Hevy side, or malformed payloads.

### 4.4 Distance — tertiary (15% of post-gate weight)

Same shape as duration. Lower weight because:

- GPS noise produces routine 1–5% mismatches even for legitimate matches.
- A surprising number of users hand-correct distance after the fact.
- Distance is 0 for many indoor activities — making it a weak signal
  *and* often missing.

### 4.5 Why not include heart rate, pace, route?

We deliberately do not include these in the *matching* score because they
are exactly the data we are trying to **merge in**. Using them to choose
which Hevy workout to merge into would create a circular dependency
(Hevy doesn't have HR yet; that's the point).

## 5. Weight renormalization

A naive weighted sum that treats "missing dimension" as 1.0 or 0.0 biases
the result. We instead **drop the missing dimension and renormalize over
the dimensions that remain**:

```python
components = {"time": time_s}
if dur_s is not None:  components["duration"] = dur_s
if dist_s is not None: components["distance"] = dist_s

total_weight = sum(_WEIGHTS[k] for k in components)
final = sum(components[k] * _WEIGHTS[k] for k in components) / total_weight
```

So an indoor treadmill match scores against time + duration only,
renormalized to `0.60 + 0.25 = 0.85` total weight. A perfect match still
hits 1.0; a 50% duration match hits `(1.0·0.60 + 0.0·0.25) / 0.85 = 0.706`,
which is in the review band — exactly the right behavior for a partial
signal.

The final value is then clamped to `[0, 1]` to absorb FP drift before it
hits the `CHECK (confidence BETWEEN 0 AND 1)` constraint in SQLite.

## 6. Threshold selection

| Bucket | Range | Behavior |
|---|---|---|
| **auto_merge** | `score ≥ 0.85` | The writer can act without confirmation. |
| **review** | `0.60 ≤ score < 0.85` | Surface in the UI for explicit user confirmation. |
| **rejected** | `score < 0.60` (type-gate passed) | Log for debugging; never shown to users. |
| **filtered out** | type gate failed | Not even logged as a near-miss. |

Why these numbers:

- **0.85 auto-merge floor.** Pinned to *exact* match minus realistic
  watch drift. A 90-second start offset with 3-4% duration/distance drift
  scores ~0.944 (traced in `test_realistic_watch_drift_still_auto_merges`).
  That's well above 0.85, so the threshold has room to absorb genuine
  jitter while keeping out adversarial ambiguity. Raising to 0.90 would
  rule out the "I forgot to press start on Hevy until 8 min in" case;
  lowering to 0.80 would let two same-type activities 15 min apart
  auto-merge.
- **0.60 review floor.** The lowest score at which "this might be the
  same workout" is a coherent statement. Below this, scores are dominated
  by start times >25 min apart with otherwise-coincidental distance
  matches — i.e. *different* workouts that happen to share a distance.

These thresholds are constants in `matcher.py` (`AUTO_MERGE_THRESHOLD`,
`REVIEW_THRESHOLD`). If you change them, re-run the math traces in
section 10 and adjust the bracketing tests in `test_matcher.py` that pin
specific scores to specific buckets.

## 7. The brick-workout problem

This was the **largest single design decision** during the build.

**Setup**: a Hevy workout can contain multiple exercises with different
`exercise_template_id`s. A "brick" session is bike→run combined in one
Hevy record. A gym session might have a Running warmup before squats. A
user could even add a "Cycling" exercise to a workout that was 90% running.

**The naive approach** sums `duration_seconds` and `distance_meters`
across all sets and compares totals against the Strava activity. This
breaks in a specific, scary way:

> A user records a Hevy run (30min/5km) and tacks a 5-min/1km bike
> warmup onto the same workout. Strava sees only a separate bike
> activity (30min/15km) later in the day. The type gate passes (a Ride
> template is present), and **summed Hevy totals** are 35min/6km vs the
> bike's 30min/15km — distance is way off but duration matches well. A
> coincidental time alignment (same hour of day) tips it over 0.85 →
> the bike is auto-merged into a run-dominated Hevy. The user sees their
> running data polluted with mountain-bike HR.

**The fix**: `HevyWorkoutSummary` carries a tuple of
`HevyExerciseSummary(template_id, duration_seconds, distance_meters)`
instead of bulk totals. `score()` filters these to the exercises whose
template matches the Strava activity type, and compares the **matching
subset** against Strava.

In the brick scenario above, matching = `(Ride, 5min, 1km)` only, so
`duration_ratio = min(1800, 300) / max = 0.167` (clamped → 0) and
`distance_ratio = 1000/15000 = 0.067` (clamped → 0). Final score lands
around 0.60 — in the review bucket, where it belongs.

The legitimate brick case still works: a real bike→run brick where the
Hevy `Ride` exercise has 30min/15km gives a `(1800/1800, 15000/15000) =
(1.0, 1.0)` match against the Strava bike. Auto-merge.

Tests: `test_brick_does_not_let_one_template_steal_other_total` and
`test_brick_real_match_for_corresponding_exercise`.

## 8. Robustness layer

Most "matcher bugs" in practice aren't math errors — they're
data-shape surprises. The matcher is the only thing standing between a
malformed payload and a CHECK constraint violation / silent mismatch /
year-57580 timestamp. So we spend a lot of code defending its edges.

### 8.1 Timestamp coercion — `_to_epoch`

Hevy's **internal app API** returns `start_time` as Unix seconds. Hevy's
**public v1 REST API** returns it as ISO-8601 strings. Both shapes can
reach `summarize_hevy` depending on which import path the caller uses.

`_to_epoch` accepts:
- `int` / `float`: returned as `int(value)`, with a heuristic rescale
  when the value exceeds 10^10 (assumed to be epoch-milliseconds and
  divided by 1000). Year 2286 in seconds is the cutoff. This caught a
  reproducible silent-failure mode where a caller passed JS-style ms by
  mistake; without the rescale, the workout landed in year 57580 and
  silently never matched anything.
- ISO-8601 strings with `Z`, with explicit offsets, or naive. Naive
  datetimes are **assumed to be UTC** — anything else would tie us to
  the host's local timezone, which is exactly the kind of bug we don't
  want.
- `None` / empty string → 0.
- `bool` is explicitly rejected even though it's an int subclass.
- Anything else → 0.

### 8.2 Numeric coercion — `_safe_int` / `_safe_float`

We've seen all of these in real-world Hevy-shaped payloads:

- `"10030"` — string-encoded int.
- `"10,030"` — comma-formatted string (works after a strip).
- `"n/a"` — garbage. Becomes 0, doesn't crash.
- `None` — missing. Becomes 0.
- NaN / inf — non-finite. Filtered later by `_ratio_score` so they
  don't propagate through the weighted sum.

### 8.3 Defensive iteration

`for s in ex.get("sets") or []` would happily iterate the characters of a
string if `sets` were ever `"oops"`. We isinstance-check before iterating
both the `exercises` list and each `sets` list (`matcher.py:170-186`).

### 8.4 Required vs optional fields

`workout_id` is the only field we treat as required. `summarize_hevy`
raises `ValueError` if it's missing rather than silently returning a row
with an empty id — that row would later flow into `mark_merged("…", "")`
and create a bogus DB record. We'd rather crash visibly.

## 9. Persistence: the `merged_workouts` table

```sql
CREATE TABLE merged_workouts (
    strava_activity_id TEXT NOT NULL,
    hevy_workout_id    TEXT NOT NULL,
    merged_at          TEXT NOT NULL,
    confidence         REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    source             TEXT NOT NULL DEFAULT 'auto'
                            CHECK (source IN ('auto', 'user')),
    PRIMARY KEY (strava_activity_id, hevy_workout_id)
);
```

### 9.1 Why a composite PK

Two real cases require both ids in the key:

- **Brick workout, one Hevy ↔ many Strava**: user did bike + run as
  one Hevy session, Strava recorded them as separate activities. The
  Hevy id appears twice with different Strava ids.
- **User correction, many Hevy ↔ one Strava**: rare, but possible — a
  Strava activity erroneously auto-merged into Hevy A, then user
  manually links it to Hevy B. Both rows can coexist; the writer
  decides which one is authoritative based on `source`.

A single-column PK on either id would have blocked one of these.

### 9.2 Why `source` is sticky against auto rematches

`mark_merged` uses `INSERT … ON CONFLICT … DO UPDATE` with:

```sql
source = CASE
    WHEN merged_workouts.source = 'user' THEN 'user'
    ELSE excluded.source
END
```

So if a user manually confirms a borderline match (`source='user'`,
`confidence=0.72`), and a later automated rematch reaches the same pair
with `(source='auto', confidence=0.81)`, the row keeps `source='user'`
but updates `confidence` to 0.81. Auto can update confidence on its own
rows freely; auto cannot downgrade a user decision.

This protects user intent across reruns of the matcher, which is the
single most important property of the persistence layer.

### 9.3 What we don't track here

- The **set of candidates considered** during a match decision. Logged
  as event-log lines in dry-run mode (planned, not yet implemented),
  not persisted as structured rows. Not worth a schema.
- The **score breakdown** at merge time. The `MatchScore` returned by
  `score()` is rich, but we only persist `confidence`. If a future
  iteration wants explainability in the UI, the breakdown should be
  recomputed on demand rather than stored.

### 9.4 Relationship to `imported_activities`

`imported_activities` tracks "Strava activity X was imported as new Hevy
workout Y." `merged_workouts` tracks "Strava activity X was identified
as the same session as existing Hevy workout Y."

They are deliberately separate. A merge is *not* an import — the
poller's "skip already-imported" check (`is_imported`) should *not*
short-circuit a merge attempt against a Hevy workout that was created
independently of any prior import.

## 10. Math walkthroughs

These are the canonical traces. If the matcher ever produces different
numbers for these inputs, something in the math has drifted.

### Exact match → ≥ 0.95

```
strava = (Run, T0, 4101s, 10030m)
hevy   = (start=T0, end=T0+4101, [Run AC1BB830: 4101s, 10030m])

type           1.0   (AC1BB830 in expected for Run)
time_score     1.0   (Δstart=0 → start_score=1.0; overlap=union=4101 → iou=1.0)
duration       1.0   (ratio=1.0)
distance       1.0   (ratio=1.0)
final          (1.0·0.60 + 1.0·0.25 + 1.0·0.15) / 1.0 = 1.0
```

### Realistic watch drift → 0.944 (auto-merge)

90 s start drift, 3% duration drift, 4% distance drift:

```
strava = (Run, T0+90, 4220s, 10440m)
hevy   = (T0, T0+4101, [Run: 4101s, 10030m])

start_diff = 90s → start_score = 1 - 90/1800 = 0.950
strava_end = T0+90+4220 = T0+4310;  hevy_end = T0+4101
overlap = T0+4101 - T0+90 = 4011
union   = T0+4310 - T0    = 4310
iou     = 0.930
time_score = max(0.950, 0.930) = 0.950

dur_ratio  = 4101/4220 = 0.972 → (0.972-0.5)*2 = 0.944
dist_ratio = 10030/10440 = 0.961 → 0.921

final = (0.950·0.60 + 0.944·0.25 + 0.921·0.15) / 1.0
      = 0.570 + 0.236 + 0.138 = 0.944
```

### 15 min off → 0.784 (review)

```
strava = (Run, T0+900, 4101s, 10030m)
hevy   = (Run, T0, T0+4101, 4101s, 10030m)

start_diff = 900 → start_score = 1 - 900/1800 = 0.500
overlap    = T0+4101 - T0+900 = 3201
union      = T0+5001 - T0     = 5001
iou        = 0.640
time_score = 0.640

dur, dist  = 1.0, 1.0
final      = (0.640·0.60 + 0.25 + 0.15) = 0.784  → review
```

### 45 min off → 0.524 (rejected)

```
start_diff = 2700 → start_score = max(0, 1 - 2700/1800) = 0
overlap = 1401, union = 6801 → iou = 0.206
time_score = 0.206
final = 0.206·0.60 + 0.40 = 0.524  → rejected
```

### Brick with wrong-template Strava → 0.600 (review, not auto)

```
strava = (Ride, T0, 1800s, 15000m)
hevy   = (T0, T0+2100, [Ride: 300s/1000m, Run: 1800s/5000m])

type        1.0  (D8F7F851 present)
matching    [Ride (300s, 1000m)]   ← brick filter
matching_dur, matching_dist = 300, 1000

dur  = min(1800,300)/max = 0.167 → (0.167-0.5)*2 → 0
dist = 1000/15000 = 0.067 → 0
time = 1.0

final = (1.0·0.60 + 0·0.25 + 0·0.15) / 1.0 = 0.600  → review, NOT auto
```

If `summarize_hevy` ever reverts to bulk totals (matching = 2100s/6000m
instead of 300s/1000m), this trace lands above 0.85 and the brick steals
the merge. That's the regression to guard against.

## 11. What we explicitly do NOT solve

These are known limitations. Each has a one-line note on the
alternative we considered.

- **Activity types we don't import** (Yoga, Swim, Weight Training):
  filtered out at the type gate. *(Adding them is a `DEFAULT_TYPE_TEMPLATE_IDS` + `ALL_ACTIVITY_TYPES` change, nothing else.)*
- **Per-exercise time alignment in bricks**: the second leg of a brick
  (the run after the bike) gets a `start_score` near 0 because Hevy
  reports the brick's start, not the run's start. Such matches land in
  review, which is acceptable. *(A real fix needs per-exercise
  `started_at`, which Hevy doesn't expose.)*
- **Cross-day workouts** (started at 23:55, finished after midnight):
  the day-keyed candidate filter described in the strategy doc would
  miss the previous-day Hevy workout. *(Solution: query a ±24h window
  around `strava.start_date`, not strictly the same calendar day.)*
- **Two Strava activities → same Hevy** when the user starts/stops
  Strava mid-session: the second activity will likely match too, and we
  do nothing to prevent both merging into the same Hevy biometrics
  block. *(Mitigation in the writer: dedupe HR samples by
  `timestamp_ms` before appending.)*
- **Manual unlink UI**: schema supports it (`unmerge`), no UI yet.
- **Tuning thresholds per-user**: hardcoded constants. *(Easy to expose
  via `state.set("auto_merge_threshold", …)` if needed.)*

## 12. How to change things safely

### Changing weights

The three weights in `_WEIGHTS` should be considered together — the
ratios matter, not the absolute values (we renormalize). If you raise
`time` to 0.70 and want `duration`/`distance` to stay in proportion:

```python
_WEIGHTS = {"time": 0.70, "duration": 0.20, "distance": 0.10}
```

Re-trace the canonical scenarios in section 10. If any of them move into
a different bucket, the test fixtures need updating to match the new
intent.

### Adding a scoring dimension

Three places to touch:
1. `score()`: compute the new component, conditionally add to
   `components`, with a reason if skipped.
2. `_WEIGHTS`: register the weight.
3. Tests: at least one positive case, one missing-dimension case.

The renormalization handles the rest automatically.

### Adding an activity type

Two places:
1. `ALL_ACTIVITY_TYPES` in `server/strava_client.py:33`.
2. `DEFAULT_TYPE_TEMPLATE_IDS` in `server/matcher.py:56`.

The parity test will fail loudly if you miss one.

### Changing the brick filter

The current filter (matching by `template_id`) is the simplest correct
one. If you want to weight partial overlap (Hevy has both a Run and a
Ride; Strava is just a Run — should the Ride exercise's presence count
*against* the match?), be careful: it's easy to introduce a regression
where legitimate bricks fail to match because they "contain extra
templates." The test `test_brick_real_match_for_corresponding_exercise`
is the regression guard.

## 13. Pointers

| What | Where |
|---|---|
| Module | `server/matcher.py` |
| Persistence | `server/state.py:213-318` |
| Schema | `server/state.py:64-73` |
| Strava type map source | `server/strava_client.py:33,41` |
| Hevy workout shape | `server/strava_client.py:278` (template) |
| Matcher tests | `server/test_matcher.py` |
| Persistence tests | `server/test_state.py` |
| Strategy doc (higher level, pre-implementation) | conversation history / PR description |

To run the tests from `server/`:

```sh
python -m unittest test_matcher test_state
```

The `TypeMapParityTests` class in `test_matcher.py` requires `stravalib`
to be importable; it's skipped automatically when it isn't.
