"""Canonical activity model and field-level merge policy.

The hub of the hub-and-spoke sync architecture (see
`PROVIDER_ARCHITECTURE.md`). One `CanonicalActivity` per real-world
training session, one `ProviderLink` row per external system that has a
copy of it, one `Provider` implementation per external system.

This module contains:

* `FieldValue` — a provenance wrapper around a scalar. Records *who* set
  the value, *when*, and whether it's user-locked. The mechanism that
  lets us answer "should an incoming Hevy title overwrite a Strava
  title?" with a declarative rule instead of N pairwise branches.
* `HRSample` / `PowerSample` — sample records with per-sample lineage
  (`source_provider`, `source_external_id`). The mechanism that makes
  brick-workout HR attribution well-defined: each sample knows which
  external activity contributed it.
* `MergePolicy` — declarative rules per field, evaluated in one place
  (`should_overwrite`). The only place per-project taste lives.
* `CanonicalActivity` — the record itself. Pure dataclass; no I/O.
* `CanonicalPatch` — a partial update that crosses provider boundaries.
* `apply_patch` — the one function that mutates a canonical, evaluating
  merge policy and dedupe lineage.

Nothing in this module talks to SQLite, the network, or any provider's
API. The point of this layer is that adding a new provider does not
require changes here — only the policy table grows, and only if the
provider supplies a field no existing provider does.

See PROVIDER_ARCHITECTURE.md §4 and §5 for the design rationale.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Generic, TypeVar


T = TypeVar("T")


# ──────────────────────────────────────────────────────────────────────────
# Provenance wrapper
# ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FieldValue(Generic[T]):
    """A scalar value paired with the metadata needed to merge it safely.

    `source` is the provider name (`"strava"`, `"hevy"`, …) or `"user"`
    for a manual override. `set_at` is the epoch second at which the
    provider observed this value (NOT when we received the patch — the
    provider's own timestamp, so retro-pulls of older data don't appear
    "newer" than current data). `locked` is the user-override flag: when
    True, no automated patch can change this field, regardless of policy.

    The wrapper is intentionally minimal. Anything more (e.g. confidence
    intervals, multi-source aggregation) belongs in a future iteration
    and would need a corresponding `MergePolicy` to consume it.
    """

    value: T
    source: str
    set_at: int
    locked: bool = False

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "source": self.source,
            "set_at": self.set_at,
            "locked": self.locked,
        }

    @classmethod
    def from_jsonable(cls, data: dict[str, Any] | None) -> "FieldValue | None":
        if data is None:
            return None
        return cls(
            value=data.get("value"),
            source=str(data.get("source", "")),
            set_at=int(data.get("set_at", 0)),
            locked=bool(data.get("locked", False)),
        )


# ──────────────────────────────────────────────────────────────────────────
# Samples with lineage
# ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HRSample:
    """One heart-rate reading with the lineage needed for cross-provider sync.

    `source_provider` + `source_external_id` together identify which
    external activity contributed this sample. That's the mechanism that
    answers "which Strava activity in a brick session did this sample
    come from?" — and the mechanism that prevents echo loops when we
    push samples back to the same provider that supplied them.

    Dedupe key is `(source_provider, source_external_id, timestamp_ms)`.
    See PROVIDER_ARCHITECTURE.md §4.2.
    """

    timestamp_ms: int
    bpm: int
    source_provider: str
    source_external_id: str

    def lineage_key(self) -> tuple[str, str, int]:
        return (self.source_provider, self.source_external_id, self.timestamp_ms)


@dataclass(frozen=True)
class PowerSample:
    """One power reading, same shape and rationale as HRSample."""

    timestamp_ms: int
    watts: int
    source_provider: str
    source_external_id: str

    def lineage_key(self) -> tuple[str, str, int]:
        return (self.source_provider, self.source_external_id, self.timestamp_ms)


# ──────────────────────────────────────────────────────────────────────────
# Merge policies
# ──────────────────────────────────────────────────────────────────────────


class MergePolicy(Enum):
    """How `should_overwrite` decides between an existing and incoming value.

    The five policies are deliberately small. New behaviors should be
    new enum members rather than ad-hoc conditionals at call sites; the
    whole point of this layer is that the decision is declarative.

    * `FIRST_WRITER_WINS` — once a value is set, it is not overwritten by
      any other source. Used for `activity_type`: we don't reclassify a
      Run as a Ride because a different provider disagrees.
    * `MOST_RECENT_WINS` — the value with the larger `set_at` wins. Used
      where the field is genuinely changeable and we trust whoever
      observed it most recently (e.g. `device_name` if a user re-records).
    * `PREFER_SPECIFIC_OVER_NULL` — incoming value wins only if the
      existing value is None. Otherwise existing wins. Used for fields
      where any non-null observation is better than nothing but we don't
      want to flap between two non-null sources that disagree slightly.
    * `PREFER_PROVIDER:X` — if the incoming source is X, it wins.
      Otherwise X owns this field: only X may update it (rerun case),
      and a non-X incoming patch can only fill in a None. Used for
      `title`/`description` to make Hevy authoritative without preventing
      Hevy from updating its own copy.
    * `ADDITIVE` — sentinel for sample lists. `should_overwrite` is
      never called for these; `apply_patch` merges by lineage instead.
    """

    FIRST_WRITER_WINS = "first_writer_wins"
    MOST_RECENT_WINS = "most_recent_wins"
    PREFER_SPECIFIC_OVER_NULL = "prefer_specific_over_null"
    ADDITIVE = "additive"
    # PREFER_PROVIDER is parameterized — see PreferProvider below.


@dataclass(frozen=True)
class PreferProvider:
    """Parameterized merge policy: a specific provider owns this field.

    Compared against by identity (`isinstance`) rather than by enum
    membership so `MergePolicy` doesn't have to carry the parameter.
    """

    provider: str


# The default policy table. Per PROVIDER_ARCHITECTURE.md §5, this is the
# *only* place where per-project taste about field ownership lives.
#
# When a new provider adds a field no existing provider supplied, the
# new field gets a policy entry here. Modifying an existing entry
# changes how the entire system resolves conflicts on that field; treat
# changes here as you would changes to the matcher thresholds.
DEFAULT_POLICIES: dict[str, MergePolicy | PreferProvider] = {
    "activity_type": MergePolicy.FIRST_WRITER_WINS,
    "title": PreferProvider("hevy"),
    "description": PreferProvider("hevy"),
    "is_private": PreferProvider("hevy"),
    "device_name": MergePolicy.MOST_RECENT_WINS,
    "calories": MergePolicy.PREFER_SPECIFIC_OVER_NULL,
    "distance_meters": MergePolicy.PREFER_SPECIFIC_OVER_NULL,
    "moving_seconds": MergePolicy.PREFER_SPECIFIC_OVER_NULL,
}


def should_overwrite(
    existing: FieldValue | None,
    incoming: FieldValue,
    policy: MergePolicy | PreferProvider,
) -> bool:
    """Apply `policy` to decide whether `incoming` should replace `existing`.

    A None `existing` means "field has never been set"; the incoming
    value always wins in that case (subject to lock, which can't be
    True if existing is None — locks are only meaningful on set values).

    `locked` always wins. The only way past a lock is an explicit unlock
    (a future state.py accessor; not part of the patch flow).

    See PROVIDER_ARCHITECTURE.md §5 for the full policy semantics.
    """
    if existing is None:
        return True
    if existing.locked:
        return False

    if isinstance(policy, PreferProvider):
        if incoming.source == policy.provider:
            return True
        # Non-owner can only fill nulls — even nulls written by the owner.
        # An owner who has no value for a field shouldn't permanently
        # block other providers from supplying one.
        return existing.value is None

    if policy is MergePolicy.FIRST_WRITER_WINS:
        return existing.value is None

    if policy is MergePolicy.MOST_RECENT_WINS:
        return incoming.set_at > existing.set_at

    if policy is MergePolicy.PREFER_SPECIFIC_OVER_NULL:
        return existing.value is None and incoming.value is not None

    if policy is MergePolicy.ADDITIVE:
        # Sample lists are merged by apply_patch directly, not via
        # should_overwrite. If we get here, something is wired wrong.
        raise ValueError(
            "ADDITIVE policy is for sample lists; should_overwrite "
            "should not be called for them"
        )

    raise ValueError(f"unknown merge policy: {policy!r}")


# ──────────────────────────────────────────────────────────────────────────
# Canonical record + patch
# ──────────────────────────────────────────────────────────────────────────


# Field names that are FieldValue-wrapped scalars on CanonicalActivity.
# Used by serialization and by apply_patch's iteration. Kept in sync
# with the dataclass below by `_validate_canonical_fields()` at import
# time.
_FIELDVALUE_FIELDS: tuple[str, ...] = (
    "title",
    "description",
    "is_private",
    "device_name",
    "calories",
    "distance_meters",
    "moving_seconds",
)


@dataclass
class CanonicalActivity:
    """The merged truth for one training session.

    Identity fields (`id`, `start_ts`, `end_ts`, `activity_type`) are
    set at canonical creation and not normally changed.
    `activity_type` is governed by FIRST_WRITER_WINS so it could in
    principle be set by a later patch if creation left it unset, but
    once non-None it's immutable.

    All other scalars are FieldValue-wrapped — see PROVIDER_ARCHITECTURE.md
    §4 for the full list and rationale.

    Sample lists are additive across patches; dedupe is by lineage_key.
    """

    id: str
    activity_type: str
    start_ts: int
    end_ts: int | None

    title: FieldValue[str] | None = None
    description: FieldValue[str] | None = None
    is_private: FieldValue[bool] | None = None
    device_name: FieldValue[str | None] | None = None
    calories: FieldValue[int | None] | None = None
    distance_meters: FieldValue[float | None] | None = None
    moving_seconds: FieldValue[int | None] | None = None

    hr_samples: list[HRSample] = field(default_factory=list)
    power_samples: list[PowerSample] = field(default_factory=list)

    created_at: int = 0
    updated_at: int = 0

    @staticmethod
    def new(activity_type: str, start_ts: int, end_ts: int | None = None) -> "CanonicalActivity":
        """Create a fresh canonical with timestamps populated."""
        now = int(datetime.now(timezone.utc).timestamp())
        return CanonicalActivity(
            id=str(uuid.uuid4()),
            activity_type=activity_type,
            start_ts=start_ts,
            end_ts=end_ts,
            created_at=now,
            updated_at=now,
        )

    def to_jsonable(self) -> dict[str, Any]:
        """Serialize to a dict that round-trips through json.dumps."""
        out: dict[str, Any] = {
            "id": self.id,
            "activity_type": self.activity_type,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "fields": {},
            "samples": {
                "hr": [asdict(s) for s in self.hr_samples],
                "power": [asdict(s) for s in self.power_samples],
            },
        }
        for name in _FIELDVALUE_FIELDS:
            fv = getattr(self, name)
            out["fields"][name] = fv.to_jsonable() if fv is not None else None
        return out

    @classmethod
    def from_jsonable(cls, data: dict[str, Any]) -> "CanonicalActivity":
        fields_blob = data.get("fields") or {}
        samples_blob = data.get("samples") or {}
        kwargs: dict[str, Any] = {
            "id": str(data["id"]),
            "activity_type": str(data["activity_type"]),
            "start_ts": int(data["start_ts"]),
            "end_ts": (
                int(data["end_ts"]) if data.get("end_ts") is not None else None
            ),
            "created_at": int(data.get("created_at", 0)),
            "updated_at": int(data.get("updated_at", 0)),
            "hr_samples": [HRSample(**s) for s in samples_blob.get("hr", [])],
            "power_samples": [
                PowerSample(**s) for s in samples_blob.get("power", [])
            ],
        }
        for name in _FIELDVALUE_FIELDS:
            kwargs[name] = FieldValue.from_jsonable(fields_blob.get(name))
        return cls(**kwargs)


@dataclass(frozen=True)
class CanonicalPatch:
    """A partial update to a canonical, produced by `provider.to_canonical()`.

    Each scalar field is optional and `None` means "no opinion" — never
    "clear this field." Clearing requires an explicit user action, not
    a missing value on an inbound patch.

    Each FieldValue carries its own provenance, so `apply_patch` can
    consult policy without knowing what produced the patch.
    """

    activity_type: str | None = None
    start_ts: int | None = None
    end_ts: int | None = None

    title: FieldValue[str] | None = None
    description: FieldValue[str] | None = None
    is_private: FieldValue[bool] | None = None
    device_name: FieldValue[str | None] | None = None
    calories: FieldValue[int | None] | None = None
    distance_meters: FieldValue[float | None] | None = None
    moving_seconds: FieldValue[int | None] | None = None

    hr_samples: tuple[HRSample, ...] = ()
    power_samples: tuple[PowerSample, ...] = ()


def apply_patch(
    canonical: CanonicalActivity,
    patch: CanonicalPatch,
    policies: dict[str, MergePolicy | PreferProvider] | None = None,
) -> bool:
    """Apply `patch` to `canonical` in place; return True if anything changed.

    For each FieldValue field on the patch, consult the policy table and
    overwrite if `should_overwrite` says so. For samples, union by
    lineage_key — never discard existing samples.

    `policies` defaults to `DEFAULT_POLICIES`. Callers can pass a custom
    table for testing or per-user overrides.

    Identity fields (`activity_type`, `start_ts`, `end_ts`) are handled
    specially: they are populated on creation and don't change. The one
    exception is `activity_type` under FIRST_WRITER_WINS — if it was
    somehow left empty, a later patch may fill it in.
    """
    policies = policies or DEFAULT_POLICIES
    changed = False

    if patch.activity_type and not canonical.activity_type:
        canonical.activity_type = patch.activity_type
        changed = True

    if patch.end_ts is not None and canonical.end_ts is None:
        canonical.end_ts = patch.end_ts
        changed = True

    for name in _FIELDVALUE_FIELDS:
        incoming: FieldValue | None = getattr(patch, name)
        if incoming is None:
            continue
        existing: FieldValue | None = getattr(canonical, name)
        policy = policies.get(name)
        if policy is None:
            raise ValueError(
                f"no merge policy registered for field {name!r} — add an "
                f"entry to canonical.DEFAULT_POLICIES"
            )
        if isinstance(policy, MergePolicy) and policy is MergePolicy.ADDITIVE:
            raise ValueError(
                f"field {name!r} cannot use ADDITIVE policy; that's only "
                f"for sample lists"
            )
        # Provenance shortcut (PROVIDER_ARCHITECTURE.md §7): identical
        # source+value is a no-op even if policy would say "overwrite."
        # Prevents echo loops where pulling our own pushed data
        # ratchets `set_at` forward on every cycle.
        if (
            existing is not None
            and existing.source == incoming.source
            and existing.value == incoming.value
        ):
            continue
        if should_overwrite(existing, incoming, policy):
            setattr(canonical, name, incoming)
            changed = True

    if patch.hr_samples:
        added = _merge_samples(canonical.hr_samples, patch.hr_samples)
        if added:
            changed = True
    if patch.power_samples:
        added = _merge_samples(canonical.power_samples, patch.power_samples)
        if added:
            changed = True

    if changed:
        canonical.updated_at = int(datetime.now(timezone.utc).timestamp())
    return changed


def _merge_samples(existing: list, incoming) -> int:
    """Union samples by lineage_key, preserving insertion order. Returns
    the number of new samples added."""
    seen = {s.lineage_key() for s in existing}
    added = 0
    for s in incoming:
        if s.lineage_key() in seen:
            continue
        existing.append(s)
        seen.add(s.lineage_key())
        added += 1
    return added


# ──────────────────────────────────────────────────────────────────────────
# Self-check: every FieldValue-typed field on CanonicalActivity must
# have a corresponding entry in _FIELDVALUE_FIELDS and in DEFAULT_POLICIES.
# Run at import time so drift between the dataclass, the tuple, and the
# policy table fails loudly instead of silently producing wrong merges.
# ──────────────────────────────────────────────────────────────────────────


def _validate_canonical_fields() -> None:
    declared = set(_FIELDVALUE_FIELDS)
    in_policies = set(DEFAULT_POLICIES) - {"activity_type"}
    in_class = set()
    for f in fields(CanonicalActivity):
        if "FieldValue" in str(f.type):
            in_class.add(f.name)
    if declared != in_class:
        missing_from_tuple = in_class - declared
        extra_in_tuple = declared - in_class
        raise RuntimeError(
            "canonical._FIELDVALUE_FIELDS drift: "
            f"missing {sorted(missing_from_tuple)}, "
            f"extra {sorted(extra_in_tuple)}"
        )
    if declared != in_policies:
        missing_policy = declared - in_policies
        extra_policy = in_policies - declared
        raise RuntimeError(
            "canonical.DEFAULT_POLICIES drift: "
            f"missing policy for {sorted(missing_policy)}, "
            f"extra policy for {sorted(extra_policy)}"
        )


_validate_canonical_fields()


__all__ = [
    "FieldValue",
    "HRSample",
    "PowerSample",
    "MergePolicy",
    "PreferProvider",
    "DEFAULT_POLICIES",
    "should_overwrite",
    "CanonicalActivity",
    "CanonicalPatch",
    "apply_patch",
]
