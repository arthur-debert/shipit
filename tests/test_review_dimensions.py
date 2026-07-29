from __future__ import annotations

import pytest

from shipit.review import dimensions


def test_default_set_is_the_adr_0045_decomposition():
    assert dimensions.DEFAULT_DIMENSION_NAMES == (
        "correctness",
        "cross-file-invariants",
        "security-robustness",
        "test-quality",
    )


def test_severity_tier_set_is_registered_but_experiment_only():
    tiers = ("sev-critical-high", "sev-medium", "sev-low")
    resolved = dimensions.resolve_dimensions(tiers)
    assert all(d in dimensions.DIMENSIONS and d.focus.strip() for d in resolved)
    known = dimensions.known_dimension_names()
    for name in tiers:
        assert name in known
        assert name not in dimensions.DEFAULT_DIMENSION_NAMES
    assert known == dimensions.DEFAULT_DIMENSION_NAMES + tiers


def test_every_dimension_carries_a_focus_slice():
    for dim in dimensions.DIMENSIONS:
        assert dim.name == dim.name.lower()
        assert dim.title
        assert dim.focus.strip()


def test_resolve_none_or_empty_is_the_fanout_default_set():
    default = dimensions.resolve_dimensions(dimensions.DEFAULT_DIMENSION_NAMES)
    assert dimensions.resolve_dimensions(None) == default
    assert dimensions.resolve_dimensions(()) == default
    assert (
        tuple(d.name for d in dimensions.resolve_dimensions(None))
        == dimensions.DEFAULT_DIMENSION_NAMES
    )


def test_resolve_subset_preserves_the_given_order():
    resolved = dimensions.resolve_dimensions(["test-quality", "correctness"])
    assert [d.name for d in resolved] == ["test-quality", "correctness"]


def test_resolve_unknown_name_raises():
    with pytest.raises(KeyError):
        dimensions.resolve_dimensions(["correctness", "highs-only"])


def test_fanout_variant_text_folds_names_titles_and_focus_texts():
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
    base = "x"
    injected = dimensions.fanout_variant_text(
        base, ["correctness"], {"correctness": {"model": "b\noverride.timeout: d"}}
    )
    genuine = dimensions.fanout_variant_text(
        base, ["correctness"], {"correctness": {"model": "b", "timeout": "d"}}
    )
    assert injected != genuine
