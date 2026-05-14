# Provider architecture: design notes

> Reference document for the multi-provider activity sync core
> (`server/canonical.py`, `server/provider.py`,
> `server/state.py:canonical_activities`/`provider_links`,
> `server/backfill.py`). Read this before adding a provider, changing the
> canonical model, touching `FieldValue` merge policy, or modifying the
> `provider_links` schema. It records *why* the code looks the way it does
> so future iterations don't relearn the same lessons.
>
> Companion to `MATCHING_DESIGN.md`. That document explains how a single
> external activity is matched against candidates; this one explains the
> data model the matcher operates against and the lifecycle around it.

---

## 1. Problem we're solving

A user logs the same training session into **many** services:

- A watch / phone records to Strava (GPS, HR, power).
- The user logs the same session in Hevy (title, sets, RPE, images).
- They might also push it to Garmin Connect, Wahoo, TrainingPeaks, Apple
  Health, …

Today, the import service is hardcoded for one direction:
`Strava → Hevy`. Adding a third system would require duplicating the
matching wiring, the persistence wiring, and the merge writer wiring per
new edge. With N services this is `O(N²)` edges of pairwise integration.

We want **`O(N)` integration cost**: one local canonical record per
training session, one link row per external system that has a copy of it,
one provider implementation per system. New providers plug in without
changes to the core.

This document is about the **abstraction layer** that makes that possible.
The mutating writer (push merged data back to each provider) and the
orchestrator loop (poll all providers, dispatch matching, fan out
updates) are downstream of this; both are deliberately out of scope here.

## 2. Invariants we will never violate

Every design choice below is in service of these. They are the things
that, if any one of them is broken, the system has actively made the
user's data worse than not running at all.

1. **The canonical record is the source of truth.** Every external system
   is a cached view of the canonical. The canonical never reads "what
   does Hevy think the title is" at runtime — Hevy's title is a value
   the canonical absorbed via a patch, carrying provenance.
2. **Field-level provenance is mandatory** for any scalar an external
   system can opine on. We must always be able to answer "who set this,
   when, and is it locked by user override" before deciding whether an
   incoming patch can overwrite.
3. **Additive only for streams.** HR / power samples are unioned by
   `(source_provider, source_external_id, timestamp_ms)`. We never
   discard samples that were already there — only merge in new ones.
4. **User overrides are sticky.** Once `FieldValue.locked == True`, no
   automated patch from any provider can change that field. Only an
   explicit unlock action can.
5. **Idempotent.** Pulling the same external activity twice produces no
   state change after the first. Pushing the same canonical to the same
   provider twice issues no second write.
6. **Cross-system duplicate detection is structural, not heuristic.**
   When Hevy auto-shares to Strava (or any "originated from X" path),
   the resulting Strava activity carries a marker pointing back to its
   origin. We resolve by marker, not by re-scoring through the matcher
   — because (a) the marker is exact, and (b) the activity in the
   destination system was constructed from the origin and may not score
   well against itself (different timestamps, rounded fields, etc).
7. **Provider implementations are isolated.** A provider must not import
   another provider, query another provider, or know about another
   provider's data shapes. Cross-provider knowledge lives in the
   orchestrator and the canonical model only.

## 3. The hub-and-spoke model

```
                 ┌──────────────┐
   Strava ──┐    │              │    ┌── Hevy
   Garmin ──┼───►│  Canonical   │◄───┼── Wahoo
   Apple  ──┘    │   Activity   │    └── TrainingPeaks
                 │  (the truth) │
                 └──────────────┘
```

Each spoke is a **`Provider`** — a single file implementing a single
Protocol. The hub is the **`CanonicalActivity`** plus its
**`ProviderLink`** rows. The orchestrator (out of scope here) is the only
code that knows the list of providers exists.

The asymmetry that matters: providers translate **bidirectionally**.

- *Inbound*: `provider.to_canonical(external_activity)` returns a
  `CanonicalPatch` — a partial update to the canonical record, carrying
  provenance for each field.
- *Outbound*: the orchestrator hands the provider a `CanonicalPatch` of
  fields the provider declared it can write, and the provider translates
  that into its own API shape.

Providers do not own canonical state. Providers do not see other
providers' state. The canonical is the only place where multi-provider
state lives.

## 4. The canonical model

### 4.1 `FieldValue` — the provenance wrapper

```python
@dataclass(frozen=True)
class FieldValue[T]:
    value: T
    source: str          # "strava" | "hevy" | … | "user"
    set_at: int          # epoch seconds; for tie-breaking on merge
    locked: bool = False # user override — automation must never change
```

Every scalar field on the canonical that **more than one provider could
opine on** is wrapped. Title, description, is_private, activity_type,
device_name, calories — all `FieldValue`s.

Pure descriptive fields that only one source can supply (`start_ts`,
`duration_seconds` of the canonical view) are *not* wrapped — they're
populated once on canonical creation and treated as immutable
characteristics of the underlying real-world event.

**Why provenance, not just last-writer-wins**: without `source`, you
end up writing one-off rules per field pair ("if Hevy and Strava both
have a title, prefer Hevy"). With `source`, those rules collapse into a
single `MergePolicy` table keyed by field name — *declarative*, not
spread across N branches in N call sites.

**Why `set_at`**: tie-breaks when two providers' policies say "I should
win" (e.g. both declared `most_recent_wins`). Also enables the loop
guard in §7.

**Why `locked`**: the only way a user override stays an override across
poll cycles. Without it, the next pull from any provider would happily
overwrite the user's manual correction.

### 4.2 `Sample` — the stream record with lineage

```python
@dataclass(frozen=True)
class HRSample:
    timestamp_ms: int
    bpm: int
    source_provider: str        # which provider contributed this sample
    source_external_id: str     # which external activity contributed it
```

Time-series points (HR, power, GPS in a future iteration) carry
**per-sample lineage**. This is the answer to the brick-workout question
from the planning doc:

> If a Hevy workout has two linked Strava activities (a Ride leg and a
> Run leg in a brick session), and HR samples are added by both —
> which sample belongs to which leg?

Answer: each sample carries its origin. Re-pushing to Strava only
sends the samples whose `source_external_id` matches the destination
link (or none if the destination is the origin — see §7 for that
guard). Re-pushing to Hevy sends all of them, because Hevy *is* the
brick aggregator.

This also makes dedupe trivial: union by
`(source_provider, source_external_id, timestamp_ms)`.

### 4.3 `CanonicalActivity` — the record

```python
@dataclass
class CanonicalActivity:
    id: str                               # local UUID
    activity_type: str                    # "Run", "Ride", …
    start_ts: int                         # immutable: the event itself
    end_ts: int | None

    title: FieldValue[str]
    description: FieldValue[str]
    is_private: FieldValue[bool]
    device_name: FieldValue[str | None]
    calories: FieldValue[int | None]

    # Aggregated metrics — derived from samples or from a provider that
    # supplies them directly. The wrapper records the source so a later
    # provider with more precise data can supersede a less precise one
    # via the field's MergePolicy.
    distance_meters: FieldValue[float | None]
    moving_seconds: FieldValue[int | None]

    hr_samples: list[HRSample]            # additive, dedupe by lineage+ts
    power_samples: list[PowerSample]      # same shape

    created_at: int
    updated_at: int
```

**Why dataclass, not a SQLAlchemy model**: SQLite is already wrapped by
hand-rolled state.py. Bringing in an ORM for one table is the kind of
half-finished abstraction the project explicitly avoids.

**Why no `exercises` field**: Hevy's structured set/rep blocks are
provider-specific and don't have an analogue on Strava / Garmin / etc.
We don't pretend they do. The structured-data block lives only on the
Hevy link's cached external state. If a second provider one day also
records sets/reps, this decision gets revisited then — not earlier.

### 4.4 `CanonicalPatch` — what crosses provider boundaries

```python
@dataclass(frozen=True)
class CanonicalPatch:
    activity_type: str | None = None
    start_ts: int | None = None
    end_ts: int | None = None

    # Each field carries its own provenance, so the orchestrator can
    # apply MergePolicy without having to know what the source was.
    title: FieldValue[str] | None = None
    description: FieldValue[str] | None = None
    is_private: FieldValue[bool] | None = None
    device_name: FieldValue[str | None] | None = None
    calories: FieldValue[int | None] | None = None
    distance_meters: FieldValue[float | None] | None = None
    moving_seconds: FieldValue[int | None] | None = None

    hr_samples: tuple[HRSample, ...] = ()
    power_samples: tuple[PowerSample, ...] = ()
```

A patch is a *partial* update. `None` fields are "no opinion" — they
are not "this field should be cleared." Clearing a field requires an
explicit user action; no automated pull can null out a value.

## 5. Merge policies

Field updates go through one function:

```python
def should_overwrite(existing: FieldValue, incoming: FieldValue) -> bool
```

Behavior is driven by a **`MergePolicy`** declared per field:

| Policy | Behavior | Used for |
|---|---|---|
| `additive` | Only valid for samples; union by lineage. | hr_samples, power_samples |
| `prefer_provider:X` | If incoming source is X, overwrite. Otherwise only overwrite if existing source is X (so X can update itself), else keep existing. | title (prefer Hevy), description (prefer Hevy) |
| `most_recent_wins` | Overwrite if `incoming.set_at > existing.set_at`. | device_name |
| `first_writer_wins` | Never overwrite once set. | activity_type (set at canonical creation; we don't reclassify) |
| `prefer_specific_over_null` | If existing.value is None and incoming.value isn't, overwrite. Otherwise treat as `first_writer_wins`. | calories, distance_meters, moving_seconds |

`locked` short-circuits all of these. A locked field is never
overwritten by anything except an explicit unlock.

`prefer_provider:X` is the policy that makes Hevy authoritative for
`title`/`description` while still letting Hevy itself update those
fields on rerun. The full logic:

```python
if existing.locked:                  return False
if policy is first_writer_wins:      return existing.value is None
if policy is prefer_provider(X):
    if incoming.source == X:         return True
    if existing.source == X:         return False   # X owns this; only X may write
    return existing.value is None
if policy is most_recent_wins:       return incoming.set_at > existing.set_at
if policy is prefer_specific:
    if existing.value is None:       return True
    return False
```

The policy table is the *only* place per-project taste lives. Everything
else is mechanical.

## 6. The provider Protocol

```python
class Provider(Protocol):
    name: str

    def capabilities(self) -> ProviderCaps: ...

    def list_recent(self, since: datetime) -> Iterable[ExternalActivity]: ...
    def fetch(self, external_id: str) -> ExternalActivity: ...

    def to_canonical(self, ext: ExternalActivity) -> CanonicalPatch: ...
    def origin_link(self, ext: ExternalActivity) -> tuple[str, str] | None: ...

    def create(self, canonical: CanonicalActivity) -> str: ...
    def update(self, external_id: str, patch: CanonicalPatch) -> None: ...
```

### 6.1 `ProviderCaps`

```python
@dataclass(frozen=True)
class ProviderCaps:
    readable_fields: frozenset[str]    # canonical field names we can extract
    writable_fields: frozenset[str]    # canonical field names we can push
    can_list_by_window: bool           # if False, orchestrator must mirror
    can_create: bool                   # can this provider accept brand-new
                                       # activities? (Strava: no; Hevy: yes)
    has_webhook: bool = False          # future use; unused by today's code
```

Capabilities are **declarative and trusted**. The orchestrator must not
call `provider.update(…)` with a field the provider didn't declare
writable; the provider must not silently drop fields it didn't declare.
A future per-provider self-test endpoint will exercise each declared
capability against the real API; for now we trust the declaration and
fail loudly if a write 4xx's.

### 6.2 `origin_link` — duplicate detection across systems

When Hevy posts a workout to Strava on the user's behalf
(`share_to_strava: true`), the resulting Strava activity is a derived
view of the same training session. The matcher could *probably* score
them as the same session, but:

1. The two records are constructed from each other, so coincidences in
   start time and distance approach 100% — but rounding, timezone
   handling, and the fact that Hevy's title became Strava's name make
   matcher scores noisier than they should be for what is structurally
   an exact duplicate.
2. We *know* the relationship, exactly. Throwing that knowledge away to
   run a heuristic is wasteful.

`origin_link(ext) -> (origin_provider, origin_external_id) | None` is
how a provider declares "this external activity was created from
another system, and I know which one." The orchestrator checks this
*before* running the matcher; if a result is returned and that
`(origin_provider, origin_external_id)` already has a `ProviderLink` to
a canonical, we add a second link to that canonical and skip matching
entirely.

Known origin markers, with confidence and discovery notes:

| Provider | Origin signal | Confidence | Where to look |
|---|---|---|---|
| Strava | `activity.external_id` populated by uploader on upload. Hevy sets it to its own workout_id (or a `hevy-<id>` prefix). | Medium — needs verification against a real Hevy share | `stravalib.Activity.external_id` |
| Strava | `activity.device_name` may indicate the source app for shares. | Low — secondary signal | Same |
| Hevy | No equivalent on the Hevy side; Hevy doesn't accept share-from-Strava. | n/a | n/a |

When the marker isn't verified yet, the implementation returns `None`
and we fall back to the matcher. The marker check is a fast-path, not
a correctness requirement.

### 6.3 `create` vs `update`

`create` is called only when matching produced no candidate AND no
origin link. It returns the new external_id. Providers without
`can_create` (Strava is read-only for our purposes — we don't upload
Strava activities from Hevy) raise `NotSupported`.

`update` is called with a patch of fields the provider declared
writable. Implementations must be **additive only**: never clear fields,
never overwrite a field they didn't write themselves unless the merge
policy allows it on the canonical side.

## 7. Loop prevention and idempotency

The classic hub-and-spoke failure is the write-then-read echo: push to
Strava → next pull reads the updated activity → looks like a "change"
→ apply patch → push again → ...

Three guards, each cheap and additive:

1. **`provider_links.last_push_hash`** — a hash of the writable subset
   of the canonical at the moment of last push. On next pull, if the
   incoming patch's writable subset hashes to the same value, drop the
   patch entirely. (No write attempt.)
2. **`provider_links.skip_pulls_until`** — set to `now + 60s` after a
   successful push. Pulls during this window for the same external_id
   are ignored. Catches the common case where a write is reflected in
   the next list_recent() within the same poll cycle.
3. **Provenance shortcut** — within `should_overwrite`, if `incoming`'s
   source matches the existing `FieldValue.source` AND the value is
   identical, no overwrite. (`set_at` doesn't change.) This makes
   replays of the same patch genuinely no-op.

Pushes themselves use `last_push_hash` to skip entirely when the
canonical's writable subset hasn't changed since the last successful
push.

## 8. Persistence schema

```sql
-- One row per real-world training session. The hub.
CREATE TABLE canonical_activities (
    id              TEXT PRIMARY KEY,            -- local UUID
    activity_type   TEXT NOT NULL,               -- "Run", "Ride", …
    start_ts        INTEGER NOT NULL,            -- UTC epoch seconds
    end_ts          INTEGER,
    fields_json     TEXT NOT NULL,               -- serialized FieldValues
    samples_json    TEXT NOT NULL,               -- {hr: [...], power: [...]}
    created_at      TEXT NOT NULL,               -- ISO-8601
    updated_at      TEXT NOT NULL
);
CREATE INDEX idx_canonical_start ON canonical_activities(start_ts);

-- One row per (provider, external activity) — possibly multiple rows
-- with the same canonical_id if the same canonical session is recorded
-- as separate activities in one provider (brick workouts on Strava).
CREATE TABLE provider_links (
    canonical_id        TEXT NOT NULL,
    provider            TEXT NOT NULL,
    external_id         TEXT NOT NULL,
    role                TEXT,                    -- 'primary' | 'segment' | NULL
    segment_label       TEXT,                    -- 'Ride leg', 'Run leg', etc.
    confidence          REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    link_source         TEXT NOT NULL
                            CHECK (link_source IN ('auto','user','origin','backfill')),
    last_pulled_at      TEXT,
    last_pushed_at      TEXT,
    last_push_hash      TEXT,
    external_etag       TEXT,
    skip_pulls_until    INTEGER,                 -- epoch seconds
    PRIMARY KEY (provider, external_id),
    FOREIGN KEY (canonical_id) REFERENCES canonical_activities(id) ON DELETE CASCADE
);
CREATE INDEX idx_provider_links_canonical ON provider_links(canonical_id);
```

### 8.1 Why no `UNIQUE (canonical_id, provider)`

Brick workouts. One Hevy session (`bike + run`) corresponds to two
separate Strava activities (the Ride and the Run). Both Strava
activities should link to the same canonical. With a UNIQUE constraint
on `(canonical_id, provider)`, the second insert would fail.

The trade-off: queries like "find the Strava activity for this
canonical" can return multiple rows. The `role` and `segment_label`
columns are how the orchestrator disambiguates when it has to choose
one (e.g. when pushing a single update target). Practically: pushes to
Strava in a brick scenario push the corresponding leg by inspecting
`segment_label` against the patch's content; pushes to Hevy push the
whole aggregated canonical.

### 8.2 Why JSON columns for fields and samples

Two options were on the table:

1. Normalized — `canonical_fields(canonical_id, field_name, value,
   source, set_at, locked)` and `canonical_samples(canonical_id, kind,
   timestamp_ms, value, source_provider, source_external_id)`.
2. JSON-in-row — current schema.

We picked (2) because:

- The canonical record is always loaded as a whole; we never query
  "all canonicals where title.source = 'hevy'". The normalized form
  pays for flexibility we don't use.
- SQLite handles small JSON blobs well at the scales we operate at
  (hundreds to low-thousands of activities per user). The cross-over
  to (1) is somewhere around 100k samples per activity, which we
  won't hit.
- (1) makes per-field updates marginally faster but per-canonical
  reads `N` queries; we're read-heavy in this direction.

If sample counts ever explode (e.g. second-by-second power data over
multi-hour rides for many users), revisit and move samples to their own
table; the JSON shape is forward-compatible with that move.

### 8.3 Why `link_source = 'origin'` exists

Distinguishes a link that was established via the duplicate-detection
fast path (§6.2) from one established by the matcher (`'auto'`) or by
explicit user action (`'user'`). `'backfill'` covers links created by
the one-shot migration from the old `imported_activities` /
`merged_workouts` tables.

The distinction matters for future debugging and for the "how confident
are we in this link" question. Origin links are exact by construction;
auto links are heuristic; user links are intent. We don't want to lose
that information when looking at a row a year from now.

## 9. Coexistence with the legacy tables

For the duration of the spike (this PR), the new tables coexist with
the legacy `imported_activities` and `merged_workouts` tables. The
existing poller continues to write to the legacy tables; the new
abstraction is exercised through tests and (later) a shadow-run mode
in the orchestrator.

The legacy tables are **not** read or written by any new code in this
PR. The backfill (`server/backfill.py`) populates the new tables from
the legacy ones once on initialization; that's the only point of
contact.

Removal of the legacy tables comes in a later migration, gated on the
orchestrator running cleanly end-to-end against real data for a week.
Until then, double-writes are unsafe and undesirable: they introduce a
divergent source of truth.

## 10. How to add a new provider

The test that the abstraction holds: a new provider should require
*one* new file and *no* changes to anything in `canonical.py`,
`provider.py`, or `state.py`.

Steps to add Garmin Connect (worked example):

1. Create `server/garmin_provider.py`.
2. Implement the `Provider` Protocol. Declare `ProviderCaps`:
   readable_fields likely include `title, description, distance_meters,
   moving_seconds, hr_samples, power_samples, calories, device_name`;
   writable_fields are whatever Garmin's API actually accepts on update
   (probably `title, description` only).
3. Implement `to_canonical(ext)`: produce a `CanonicalPatch` with
   `FieldValue(value, source='garmin', set_at=ext.updated_at_epoch)`
   for each field Garmin supplied.
4. Implement `origin_link(ext)`: if Garmin exposes a "shared from"
   marker (it may not — Garmin is usually the origin, not the
   destination), return the origin pair; otherwise return None.
5. Implement `create` / `update` against Garmin's API.
6. Register the provider with the orchestrator (one line).
7. If new fields need a merge policy decision (e.g. Garmin supplies an
   `elevation_gain` that no other provider does), add the field to
   `CanonicalActivity` and pick a policy in `canonical.py`. This is
   the *only* core change a new provider can necessitate, and it's
   localized.

The matcher does not need to know about Garmin. The state schema does
not need to change. The orchestrator gets one new provider in its
registry.

## 11. What we explicitly do NOT solve in this PR

These are deliberate omissions, each with the alternative we considered:

- **The orchestrator loop itself.** This PR provides the data model and
  the provider interface; wiring them into a poll loop is the next step.
  *(Could ship them together, but separating lets us validate the
  abstraction shape via tests before committing the runtime to it.)*
- **The merge writer for any provider.** `update()` is part of the
  Protocol but implementations are minimal stubs in this PR. The first
  real implementation (Hevy biometrics merge) is a follow-up.
- **A UI for `FieldValue.locked`.** Schema supports it, no UI yet.
  Locking can be done manually via state.py accessors in the meantime.
- **Webhook ingest for any provider.** `has_webhook` caps flag is
  declared, no provider sets it true today, no orchestrator path
  consumes webhooks. *(Strava supports webhooks; we'd add this when we
  outgrow polling.)*
- **Removal of `imported_activities` and `merged_workouts`.** They are
  still written by the poller. Their removal is gated on the
  orchestrator shipping (§9).
- **Migration of `hr_samples` storage out of JSON.** Acceptable now;
  becomes pressing only if a user has thousands of long activities. The
  JSON shape is intentionally schema-compatible with a future
  `canonical_samples` side table.
- **The cross-day candidate window** in the matcher. Independent issue,
  belongs to MATCHING_DESIGN.md §11.

## 12. Pointers

| What | Where |
|---|---|
| Canonical model | `server/canonical.py` |
| Provider Protocol | `server/provider.py` |
| Schema | `server/state.py` — `canonical_activities`, `provider_links` |
| Accessors | `server/state.py` — `upsert_canonical`, `get_canonical`, `link_*`, `links_for_canonical`, etc. |
| Strava provider wrapper | `server/strava_provider.py` |
| Hevy provider wrapper | `server/hevy_provider.py` |
| Backfill (legacy → new) | `server/backfill.py` |
| Tests — canonical/policies | `server/test_canonical.py` |
| Tests — state accessors | `server/test_provider_state.py` |
| Tests — backfill | `server/test_backfill.py` |
| Companion: matching design | `server/MATCHING_DESIGN.md` |

To run the new tests from `server/`:

```sh
python -m unittest test_canonical test_provider_state test_backfill
```
