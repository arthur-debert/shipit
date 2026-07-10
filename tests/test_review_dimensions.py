"""Unit tests for `shipit.review.dimensions` — the closed Dimension registry
(RVW02-WS04, ADR-0045).

The registry is the single source of the dimension vocabulary: the shipped
default set is exactly the ADR-0045 decomposition, config tokens resolve
through ONE lookup, and an unknown token is a loud failure (the config
boundary's job to translate — never a silent skip).
"""

from __future__ import annotations

import pytest

from shipit.review import dimensions


def test_default_set_is_the_adr_0045_decomposition():
    """The shipped default set: correctness, cross-file invariants,
    security/robustness, test quality — registry order."""
    assert dimensions.DEFAULT_DIMENSION_NAMES == (
        "correctness",
        "cross-file-invariants",
        "security-robustness",
        "test-quality",
    )
    assert dimensions.known_dimension_names() == dimensions.DEFAULT_DIMENSION_NAMES


def test_every_dimension_carries_a_focus_slice():
    """Each registry entry is a complete prompt slice: a canonical lowercase
    token, a human title, and a non-empty focus body the pass task embeds."""
    for dim in dimensions.DIMENSIONS:
        assert dim.name == dim.name.lower()
        assert dim.title
        assert dim.focus.strip()


def test_resolve_none_or_empty_is_the_full_default_set():
    assert dimensions.resolve_dimensions(None) == dimensions.DIMENSIONS
    assert dimensions.resolve_dimensions(()) == dimensions.DIMENSIONS


def test_resolve_subset_preserves_the_given_order():
    resolved = dimensions.resolve_dimensions(["test-quality", "correctness"])
    assert [d.name for d in resolved] == ["test-quality", "correctness"]


def test_resolve_unknown_name_raises():
    """An unknown token raises (the config boundary rejects it earlier with the
    known set; a raw KeyError here is a programming error, loud by design)."""
    with pytest.raises(KeyError):
        dimensions.resolve_dimensions(["correctness", "highs-only"])
