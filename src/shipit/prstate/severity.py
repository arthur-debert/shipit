"""The engine's read of a posted finding's Severity (ADR-0044 / RVW02).

The PR state engine routes on **Severity** directly: every finding of a review
round — a :class:`~shipit.prstate.model.ReviewComment` off a GitHub review
thread — resolves to one 4-tier :class:`~shipit.finding.Severity` through the
precedence chain, and findings from EVERY reviewer kind obey the one ladder:

  machine marker → **Reviewer adapter** native-format mapping → the adapter's
  **unclassified-severity policy** → ``major`` fail-safe — beaten only by a
  write-once **Severity override**.

The chain's ORDER lives in the pure domain (:func:`shipit.finding.
resolve_severity`); this module is the engine-side glue that feeds it from a
posted comment: the marker parsed off the body, the adapter looked up by the
comment's author (each app reviewer's adapter owns mapping its native severity
format — :meth:`~shipit.prstate.reviewers.ReviewerAdapter.native_severity` —
and, for a reviewer with no severity vocabulary at all, its explicit
unclassified policy — :attr:`~shipit.prstate.reviewers.ReviewerAdapter.
unclassified_severity`, #743: Copilot's is ``minor``), and the override read
off the snapshot (``ReadinessView.overrides``, the
:mod:`shipit.prstate.overrides` store folded on at the gather seam). The
``major`` default is the fail-safe for a reviewer WITHOUT an explicit policy:
its unparseable finding forces a review round rather than slipping past the
Breaker.

:func:`resolve_finding_severity` additionally reports WHICH rung decided
(``override | marker | adapter | policy | default``) so the classify verb's
list view can show a human where each severity came from without re-deriving
the chain.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..finding import Severity, parse_marker, resolve_severity
from .model import ReviewComment
from .reviewers import REGISTRY, ReviewerAdapter

#: The chain rungs :func:`resolve_finding_severity` reports, strongest first.
OVERRIDE = "override"
MARKER = "marker"
ADAPTER = "adapter"
POLICY = "policy"
DEFAULT = "default"


@dataclass(frozen=True)
class SeverityResolution:
    """One finding's resolved Severity + the chain rung that decided it."""

    severity: Severity
    source: str  # override | marker | adapter | policy | default


def resolve_finding_severity(
    comment: ReviewComment,
    overrides: Mapping[int, Severity],
    adapters: Sequence[ReviewerAdapter] | None = None,
) -> SeverityResolution:
    """Resolve one posted finding's Severity via the precedence chain.

    ``comment`` is the finding as the round carries it (id + body + author);
    ``overrides`` is the snapshot's write-once override store
    (``ReadinessView.overrides``). ``adapters`` is the reviewer catalog the
    author is matched against — defaulted to the full :data:`~shipit.prstate.
    reviewers.REGISTRY` so ANY known reviewer's native mapping applies; a test
    passes its own list. An author no adapter matches simply has no adapter or
    policy rung (its findings resolve marker-else-``major``).

    The precedence ORDER is delegated to the domain's
    :func:`~shipit.finding.resolve_severity` — one chain, defined once — and
    the winning rung is re-derived here for the report: the rungs are
    consulted strongest-first, so the first non-None source IS the decider.
    """
    adapters = adapters if adapters is not None else REGISTRY
    override = overrides.get(comment.comment_id)
    marker = parse_marker(comment.body)
    marker_severity = marker.severity if marker else None
    adapter = next((a for a in adapters if a.matches(comment.author)), None)
    adapter_severity = adapter.native_severity(comment.body) if adapter else None
    policy_severity = adapter.unclassified_severity if adapter else None
    severity = resolve_severity(
        marker=marker_severity,
        adapter=adapter_severity,
        override=override,
        policy=policy_severity,
    )
    if override is not None:
        source = OVERRIDE
    elif marker_severity is not None:
        source = MARKER
    elif adapter_severity is not None:
        source = ADAPTER
    elif policy_severity is not None:
        source = POLICY
    else:
        source = DEFAULT
    return SeverityResolution(severity=severity, source=source)


def finding_severity(
    comment: ReviewComment,
    overrides: Mapping[int, Severity],
    adapters: Sequence[ReviewerAdapter] | None = None,
) -> Severity:
    """The chain's Severity alone — what the Breaker and the ordering read."""
    return resolve_finding_severity(comment, overrides, adapters).severity
