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
    # Registration means resolvable to a REAL registry entry, not a name
    # round-trip: by_name maps token -> that entry, so `d.name == token` is
    # structural, and order is already covered by
    # test_resolve_subset_preserves_the_given_order. The content check is that
    # each tier resolves (no KeyError; see test_resolve_unknown_name_raises) to a
    # registry Dimension carrying a focus slice.
    resolved = dimensions.resolve_dimensions(tiers)
    assert all(d in dimensions.DIMENSIONS and d.focus.strip() for d in resolved)
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
    # Assert on the None path itself, not on `default` (which was built FROM
    # DEFAULT_DIMENSION_NAMES and so can never disagree): this pins that the
    # None/empty branch returns exactly the shipped-default names, in order.
    assert (
        tuple(d.name for d in dimensions.resolve_dimensions(None))
        == dimensions.DEFAULT_DIMENSION_NAMES
    )


def test_resolve_subset_preserves_the_given_order():
    resolved = dimensions.resolve_dimensions(["test-quality", "correctness"])
    assert [d.name for d in resolved] == ["test-quality", "correctness"]


def test_resolve_unknown_name_raises():
    """An unknown token raises (the config boundary rejects it earlier with the
    known set; a raw KeyError here is a programming error, loud by design)."""
    with pytest.raises(KeyError):
        dimensions.resolve_dimensions(["correctness", "highs-only"])


def test_fanout_variant_text_folds_names_titles_and_focus_texts():
    """#713: a fan-out round's variant material is the instructions PLUS the
    resolved dimension set — each dimension's config token, title, and focus
    slice (all prompt material the pass task embeds) — so two arms that differ
    only by dimension set can never hash to one variant."""
    base = "review instructions"
    concern = dimensions.fanout_variant_text(base, None)
    tiers = dimensions.fanout_variant_text(
        base, ["sev-critical-high", "sev-medium", "sev-low"]
    )
    assert concern != tiers
    assert concern.startswith(base) and tiers.startswith(base)
    for dim in dimensions.resolve_dimensions(None):
        assert dim.name in concern
        assert dim.title in concern
        assert dim.focus in concern


def test_fanout_variant_text_default_and_reordered_sets_pool():
    """Canonicalization: None/empty means the shipped default (pools with the
    explicit spelling of it), and passes run in parallel, so a REORDERED
    dimensions list is the same experiment — same text, same hash."""
    base = "x"
    explicit = dimensions.fanout_variant_text(
        base, list(dimensions.DEFAULT_DIMENSION_NAMES)
    )
    assert dimensions.fanout_variant_text(base, None) == explicit
    assert dimensions.fanout_variant_text(base, ()) == explicit
    reordered = dimensions.fanout_variant_text(
        base, list(reversed(dimensions.DEFAULT_DIMENSION_NAMES))
    )
    assert reordered == explicit


def test_fanout_variant_text_folds_per_dimension_overrides():
    """A per-dimension Invocation override is experiment material too (#713):
    it changes the text; override FIELD order is canonicalized; an override
    naming a dimension outside the set never reaches the material (the config
    boundaries reject it loudly — here it must simply not corrupt the hash)."""
    base = "x"
    plain = dimensions.fanout_variant_text(base, ["correctness"])
    overridden = dimensions.fanout_variant_text(
        base, ["correctness"], {"correctness": {"model": "o3", "timeout": "120s"}}
    )
    assert overridden != plain
    assert overridden == dimensions.fanout_variant_text(
        base, ["correctness"], {"correctness": {"timeout": "120s", "model": "o3"}}
    )
    stray = dimensions.fanout_variant_text(
        base, ["correctness"], {"test-quality": {"model": "o3"}}
    )
    assert stray == plain


def test_fanout_variant_text_canonicalization_is_injective_no_line_injection():
    """#713 hash integrity: the canonicalization is INJECTIVE — an override
    value carrying its own block's line framing (a newline plus a forged
    `override.` line) must NOT canonicalize identically to a genuinely
    different override set. Values are JSON-encoded, so no value can forge a
    line boundary and pool two distinct dimension sets under one variant."""
    base = "x"
    injected = dimensions.fanout_variant_text(
        base, ["correctness"], {"correctness": {"model": "b\noverride.timeout: d"}}
    )
    genuine = dimensions.fanout_variant_text(
        base, ["correctness"], {"correctness": {"model": "b", "timeout": "d"}}
    )
    assert injected != genuine
