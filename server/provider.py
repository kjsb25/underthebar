"""Provider Protocol: the uniform interface for every external system.

Each external system (Strava, Hevy, future: Garmin, Wahoo, …) is
represented by a single Python file implementing the `Provider`
Protocol declared here. The orchestrator (not in this PR) is the only
code that holds the list of providers; everything below the
orchestrator is provider-agnostic.

The Protocol is structural — providers don't `Provider` inherit from
anything; they just need to expose the right methods and a `name`
attribute. This keeps wrapping pre-existing clients (`StravaClient`,
`HevyClient`) lightweight: a thin file per provider, no rewrites of
the underlying API code.

See PROVIDER_ARCHITECTURE.md §6 for the design rationale and §10 for
the "how to add a new provider" walkthrough.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Protocol, runtime_checkable

from canonical import CanonicalActivity, CanonicalPatch


# ──────────────────────────────────────────────────────────────────────────
# Capability declaration
# ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProviderCaps:
    """What this provider can and cannot do.

    Declarative and trusted. The orchestrator inspects these to decide
    what to ask of the provider; the provider promises to honor what it
    declared. A future self-test will exercise each declared capability
    against the real API to catch drift between declaration and reality,
    but for now we fail loudly when a write 4xx's.

    `readable_fields` and `writable_fields` are sets of canonical field
    names (the strings from `canonical._FIELDVALUE_FIELDS` plus
    `"hr_samples"` and `"power_samples"`). They are NOT the provider's
    own field names — translation between API shape and canonical shape
    happens inside `to_canonical()` / `update()`.

    `can_list_by_window` is False for providers (like Hevy) that don't
    expose a date-range query. The orchestrator must mirror those
    providers locally and answer "give me candidates near time T" from
    the mirror. Providers that do support windowed listing (Strava's
    `after=` parameter) save the orchestrator from maintaining a mirror.

    `can_create` is False for read-only providers and for providers
    where we deliberately don't want to author new records (we don't
    create Strava activities from Hevy data; the asymmetry is by design,
    not an API limitation).
    """

    readable_fields: frozenset[str]
    writable_fields: frozenset[str]
    can_list_by_window: bool
    can_create: bool
    has_webhook: bool = False


# ──────────────────────────────────────────────────────────────────────────
# Inbound payload from a provider
# ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExternalActivity:
    """A raw activity as returned by a provider's `list_recent` / `fetch`.

    Providers may return any extra fields they need for their own
    `to_canonical` and `origin_link` translation — `raw` is opaque to
    everyone else. The standard fields (`external_id`, `provider`,
    `start_ts`, `updated_at_ts`) are required because the orchestrator
    consults them before involving the provider further (deduplication,
    skip-pulls-until window, etc).

    `updated_at_ts` is the provider's own "this record was last modified"
    timestamp, used as `FieldValue.set_at` when the provider produces a
    patch. Falls back to `start_ts` if the provider doesn't track an
    update timestamp.
    """

    provider: str
    external_id: str
    start_ts: int
    updated_at_ts: int
    raw: dict[str, Any] = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────────────────


class ProviderError(Exception):
    """Base for any provider-level failure. Subclasses are per-provider so
    callers can distinguish 'Strava down' from 'Hevy down'."""


class NotSupported(ProviderError):
    """Raised when a provider is asked to do something it didn't declare
    as a capability. The orchestrator should not provoke this; if it
    does, the orchestrator has a bug.

    Providers raise this when their `create` or `update` is called but
    the corresponding capability flag is False / the field set is empty.
    """


# ──────────────────────────────────────────────────────────────────────────
# The Protocol
# ──────────────────────────────────────────────────────────────────────────


@runtime_checkable
class Provider(Protocol):
    """Uniform interface for every external system.

    Implementations are stateless except for credentials (which live in
    the State object, shared across the process). Methods are sync —
    the orchestrator handles concurrency with `asyncio.to_thread`.
    """

    name: str
    """Stable identifier for this provider. Appears in
    `FieldValue.source`, `ProviderLink.provider`, `HRSample.source_provider`.
    Must match the strings used in `canonical.DEFAULT_POLICIES`
    `PreferProvider` parameters. Lowercase, no spaces.
    """

    def capabilities(self) -> ProviderCaps:
        """Return the static capability declaration. Pure; no I/O."""

    def list_recent(self, since: datetime) -> Iterable[ExternalActivity]:
        """Yield activities updated at or after `since`.

        Providers without `can_list_by_window` may ignore `since` and
        return everything they have; the orchestrator will use a local
        mirror to do the windowing instead.
        """

    def fetch(self, external_id: str) -> ExternalActivity:
        """Return one activity by its external id. Used for full-detail
        fetches when `list_recent` only returns summaries."""

    def to_canonical(self, ext: ExternalActivity) -> CanonicalPatch:
        """Translate this provider's raw shape into a CanonicalPatch.

        Each FieldValue in the returned patch must use this provider's
        `name` as `source` and the provider's own observation timestamp
        as `set_at`. Patches with the wrong `source` will be misrouted
        by the merge policy.

        Fields the provider has no opinion on are left as `None` — never
        as an empty FieldValue. (FieldValue(value=None, source='strava')
        is a different statement than not setting the field at all.)
        """

    def origin_link(self, ext: ExternalActivity) -> tuple[str, str] | None:
        """If `ext` is a cross-system duplicate, return its origin.

        Returns `(origin_provider_name, origin_external_id)` when the
        provider recognizes that this activity was created by another
        system pushing data into this one (e.g. Hevy's share-to-Strava
        flow producing a Strava activity that points back at a Hevy
        workout). Returns `None` if no origin marker is present.

        The orchestrator uses this *before* running the matcher. If the
        origin pair already has a link to some canonical, the new
        external_id is linked to that same canonical directly, without
        going through scoring. See PROVIDER_ARCHITECTURE.md §6.2.

        Providers that can't act as a destination for cross-system
        pushes (Hevy today, since Hevy doesn't accept inbound shares)
        should always return None.
        """

    def create(self, canonical: CanonicalActivity) -> str:
        """Create a new external record matching `canonical`. Return the
        new external id. Raise `NotSupported` if `caps.can_create` is False.
        """

    def update(self, external_id: str, patch: CanonicalPatch) -> None:
        """Push the writable subset of `patch` to the external record.

        The orchestrator pre-filters the patch to fields the provider
        declared writable, but implementations should still defensively
        ignore any field they don't recognize rather than crash. Writes
        must be additive: never clear fields the provider didn't author.
        """


__all__ = [
    "Provider",
    "ProviderCaps",
    "ExternalActivity",
    "ProviderError",
    "NotSupported",
]
