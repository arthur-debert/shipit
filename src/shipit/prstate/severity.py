"""The engine's read of a posted finding's Severity: the glue feeding a
posted comment into the domain's precedence chain — marker, adapter
mapping, unclassified policy, ``major``, all beaten by an override."""

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
    """Resolve one posted finding's Severity; an author no adapter matches
    resolves marker-else-``major``."""
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
