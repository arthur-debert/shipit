"""The matching primitive (RVW03-WS06): ONE deterministic definition of "the same
claim" — file + line-in-range + normalized claim-token overlap, aliases honored
(ADR-0048) — shared by the Ground-truth scorer and #673's same-round dedup.
Wording-variant cases are the point: the primitive must match rephrasings of one
defect without an LLM anywhere near the ruler."""

from __future__ import annotations

import pytest

from shipit.review.match import (
    CLAIM_THRESHOLD,
    Claim,
    MatchVerdict,
    best_overlap,
    claim_overlap,
    match_claim,
    normalize_claim,
    same_claim,
)

# The RVW02-WS05 app#391 ground truth in its two historical phrasings — the
# recall-swinging finding the fixture exists to measure (issue #665).
GT_CLAIM = (
    "the 0x0-frame short-circuit skips applyLayout, so a fully-offscreen pan "
    "leaves stale pixels on screen"
)
EMITTED_VARIANT = (
    "empty-frame short-circuit skips applyLayout; an offscreen pan leaves a "
    "stale texture on the canvas"
)


class TestNormalizeClaim:
    def test_lowercases_and_splits_on_non_alphanumerics(self):
        assert normalize_claim("Foo-BAR baz_qux") == {"foo", "bar", "baz", "qux"}

    def test_drops_stopwords_and_single_chars(self):
        tokens = normalize_claim("the a of X panic")
        assert tokens == {"panic"}

    def test_identifier_survives_as_one_token(self):
        # camelCase does not decompose — both sides normalize identically, so
        # the identifier is a high-signal shared token.
        assert "applylayout" in normalize_claim("skips applyLayout on pan")

    def test_empty_text_normalizes_empty(self):
        assert normalize_claim("") == frozenset()


class TestClaimOverlap:
    def test_identical_claims_are_1(self):
        assert claim_overlap(GT_CLAIM, GT_CLAIM) == 1.0

    def test_disjoint_claims_are_0(self):
        assert (
            claim_overlap("readback zero-fill fallback", "docstring contradicts code")
            == 0.0
        )

    def test_empty_side_is_0_not_an_error(self):
        assert claim_overlap("", GT_CLAIM) == 0.0

    def test_wording_variants_of_one_defect_clear_the_threshold(self):
        # The load-bearing case: two historical phrasings of app-G1 must agree.
        assert claim_overlap(GT_CLAIM, EMITTED_VARIANT) >= CLAIM_THRESHOLD

    def test_dense_claim_inside_paragraph_scores_high(self):
        # Overlap coefficient, not Jaccard: a paragraph-length finding text must
        # not dilute its agreement with a one-sentence fixture claim.
        paragraph = (
            "In Editor.svelte the render path short-circuits when the frame is "
            "0x0 and returns before applyLayout runs. Because the offscreen pan "
            "handler relies on that layout, panning while fully offscreen leaves "
            "stale pixels on screen until an unrelated invalidation. This was "
            "verified by reverting the guard."
        )
        assert claim_overlap(GT_CLAIM, paragraph) >= CLAIM_THRESHOLD

    def test_symmetric(self):
        assert claim_overlap(GT_CLAIM, EMITTED_VARIANT) == claim_overlap(
            EMITTED_VARIANT, GT_CLAIM
        )


class TestBestOverlap:
    def test_alias_lifts_the_score(self):
        texts = ["completely unrelated words entirely", EMITTED_VARIANT]
        assert best_overlap(GT_CLAIM, texts) >= CLAIM_THRESHOLD

    def test_empty_texts_scores_0(self):
        assert best_overlap(GT_CLAIM, []) == 0.0


LABEL = {
    "file": "src/lib/editor/Editor.svelte",
    "lines": (2360, 2385),
    "texts": [GT_CLAIM],
}


class TestMatchClaim:
    def test_same_file_line_in_range_same_words_matches(self):
        claim = Claim("src/lib/editor/Editor.svelte", 2370, EMITTED_VARIANT)
        assert match_claim(claim, **LABEL) is MatchVerdict.MATCH

    def test_different_file_never_matches_even_with_identical_text(self):
        claim = Claim("src/lib/editor/Other.svelte", 2370, GT_CLAIM)
        assert match_claim(claim, **LABEL) is MatchVerdict.NO_MATCH

    def test_right_location_unknown_wording_is_a_near_miss(self):
        # ADR-0048: right file, overlapping lines, claim below the lexical
        # threshold → surfaced for adjudication (bank an alias), never dropped.
        claim = Claim(
            "src/lib/editor/Editor.svelte",
            2370,
            "pan gesture leaves the viewport blank when the window scrolls away",
        )
        assert match_claim(claim, **LABEL) is MatchVerdict.NEAR_MISS

    def test_right_claim_line_just_outside_range_is_a_near_miss(self):
        claim = Claim("src/lib/editor/Editor.svelte", 2390, GT_CLAIM)
        assert match_claim(claim, **LABEL) is MatchVerdict.NEAR_MISS

    def test_right_claim_line_far_outside_range_is_no_match(self):
        claim = Claim("src/lib/editor/Editor.svelte", 3000, GT_CLAIM)
        assert match_claim(claim, **LABEL) is MatchVerdict.NO_MATCH

    def test_same_file_unrelated_claim_elsewhere_is_no_match(self):
        claim = Claim("src/lib/editor/Editor.svelte", 100, "tooltip label typo")
        assert match_claim(claim, **LABEL) is MatchVerdict.NO_MATCH

    def test_alias_makes_the_match(self):
        label = dict(LABEL, texts=["totally different words here now", EMITTED_VARIANT])
        claim = Claim("src/lib/editor/Editor.svelte", 2370, GT_CLAIM)
        assert match_claim(claim, **label) is MatchVerdict.MATCH

    def test_file_scoped_label_matches_any_line(self):
        label = dict(LABEL, lines=None)
        for line in (1, 5000, None):
            claim = Claim("src/lib/editor/Editor.svelte", line, GT_CLAIM)
            assert match_claim(claim, **label) is MatchVerdict.MATCH

    def test_line_none_against_ranged_label_cannot_hard_match(self):
        claim = Claim("src/lib/editor/Editor.svelte", None, GT_CLAIM)
        assert match_claim(claim, **LABEL) is MatchVerdict.NEAR_MISS

    @pytest.mark.parametrize("line", [2360, 2385])
    def test_range_is_inclusive_at_both_ends(self, line):
        claim = Claim("src/lib/editor/Editor.svelte", line, GT_CLAIM)
        assert match_claim(claim, **LABEL) is MatchVerdict.MATCH

    def test_deterministic_across_calls(self):
        claim = Claim("src/lib/editor/Editor.svelte", 2370, EMITTED_VARIANT)
        results = {match_claim(claim, **LABEL) for _ in range(50)}
        assert results == {MatchVerdict.MATCH}


class TestSameClaim:
    """The #673 dedup seam: symmetric finding-vs-finding comparison."""

    def test_two_phrasings_at_nearby_lines_are_the_same(self):
        a = Claim("src/x.py", 100, GT_CLAIM)
        b = Claim("src/x.py", 105, EMITTED_VARIANT)
        assert same_claim(a, b) and same_claim(b, a)

    def test_far_apart_lines_are_not_the_same(self):
        a = Claim("src/x.py", 100, GT_CLAIM)
        b = Claim("src/x.py", 500, GT_CLAIM)
        assert not same_claim(a, b)

    def test_different_files_are_not_the_same(self):
        a = Claim("src/x.py", 100, GT_CLAIM)
        b = Claim("src/y.py", 100, GT_CLAIM)
        assert not same_claim(a, b)

    def test_missing_line_falls_back_to_file_scope(self):
        a = Claim("src/x.py", None, GT_CLAIM)
        b = Claim("src/x.py", 100, EMITTED_VARIANT)
        assert same_claim(a, b)

    def test_same_location_different_defect_is_not_the_same(self):
        a = Claim("src/x.py", 100, "cache identity omits the execution backend")
        b = Claim("src/x.py", 100, "docstring contradicts the return contract")
        assert not same_claim(a, b)
