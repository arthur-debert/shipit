"""Unit tests for `shipit.review.dimensions` — the closed Dimension registry
(RVW02-WS04, ADR-0045).

The registry is the single source of the dimension vocabulary: the shipped
default set is exactly the ADR-0045 decomposition, the ADR-0051 severity-tier
set is registered but EXPERIMENT-ONLY (explicit selection, never default),
config tokens resolve through ONE lookup, and an unknown token is a loud
failure (the config boundary's job to translate — never a silent skip).
"""

from __future__ import annotations

import pytest

from shipit.review import dimensions


def test_default_set_is_the_adr_0045_decomposition():
    """The ADR-0051 guardrail: the SHIPPED DEFAULT set stays exactly the
    ADR-0045 four — correctness, cross-file invariants, security/robustness,
    test quality, registry order — no matter what experiment-only entries the
    registry gains."""
    assert dimensions.DEFAULT_DIMENSION_NAMES == (
        "correctness",
        "cross-file-invariants",
        "security-robustness",
        "test-quality",
    )


def test_severity_tier_set_is_registered_but_experiment_only():
    """ADR-0051: the severity-tier tokens resolve via the ONE resolver (so an
    explicit Lab-cell or Roster `dimensions` list can select them) and appear
    in the known set the config boundary validates against — but NEVER in the
    shipped default."""
    tiers = ("sev-critical-high", "sev-medium", "sev-low")
    resolved = dimensions.resolve_dimensions(tiers)
    assert tuple(d.name for d in resolved) == tiers
    known = dimensions.known_dimension_names()
    for name in tiers:
        assert name in known
        assert name not in dimensions.DEFAULT_DIMENSION_NAMES
    assert known == dimensions.DEFAULT_DIMENSION_NAMES + tiers


def test_every_dimension_carries_a_focus_slice():
    """Each registry entry is a complete prompt slice: a canonical lowercase
    token, a human title, and a non-empty focus body the pass task embeds."""
    for dim in dimensions.DIMENSIONS:
        assert dim.name == dim.name.lower()
        assert dim.title
        assert dim.focus.strip()


def test_resolve_none_or_empty_is_the_shipped_default_set():
    """None/empty resolves to the shipped default four — NOT the whole
    registry, which also carries the experiment-only tiers (ADR-0051)."""
    default = dimensions.resolve_dimensions(dimensions.DEFAULT_DIMENSION_NAMES)
    assert dimensions.resolve_dimensions(None) == default
    assert dimensions.resolve_dimensions(()) == default
    assert tuple(d.name for d in default) == dimensions.DEFAULT_DIMENSION_NAMES


def test_resolve_subset_preserves_the_given_order():
    resolved = dimensions.resolve_dimensions(["test-quality", "correctness"])
    assert [d.name for d in resolved] == ["test-quality", "correctness"]


def test_resolve_unknown_name_raises():
    """An unknown token raises (the config boundary rejects it earlier with the
    known set; a raw KeyError here is a programming error, loud by design)."""
    with pytest.raises(KeyError):
        dimensions.resolve_dimensions(["correctness", "highs-only"])
