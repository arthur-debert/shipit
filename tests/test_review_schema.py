"""Unit tests for `shipit.review.schema.extract_json` — the tolerant JSON parse.

RVW03-WS03 replaced the greedy ``{.*}`` regex fallback with a BALANCED SCAN
(:func:`shipit.review.schema._scan_embedded_objects`): agents wrap their JSON in
prose, fences, and log noise, and the greedy first-brace-to-last-brace capture
broke on any brace-bearing wrapper — a stray braced log line silently cost the
round a whole dimension pass. These tests pin the wrapper shapes the scan must
recover (prose braces before/after, fenced output, nested braces, braces inside
string values) and the invariant that a recovered object is a COMPLETE parsed
object, never a splice.
"""

from __future__ import annotations

import json

import pytest

from shipit.review.schema import extract_json, is_review_shaped

#: A review-shaped object big enough to dwarf any wrapper fragment (the scan
#: returns the LARGEST embedded object).
_REVIEW = {
    "summary": {
        "status": "COMMENT",
        "overall_feedback": "looks fine overall",
        "coverage": {"reviewed": ["a.py", "b.py"], "skipped": []},
    },
    "comments": [
        {
            "file": "a.py",
            "line": 3,
            "text": "off-by-one in the loop bound",
            "severity": "major",
            "category": "correctness",
            "confidence": 0.9,
            "evidence": "for i in range(n + 1)",
            "fix": "range(n)",
        }
    ],
}
_REVIEW_JSON = json.dumps(_REVIEW)


def test_direct_parse_of_bare_json():
    assert extract_json(f"  {_REVIEW_JSON}\n") == _REVIEW


def test_fenced_output_is_unwrapped():
    assert extract_json(f"```json\n{_REVIEW_JSON}\n```") == _REVIEW


def test_prose_around_a_fenced_object_is_tolerated():
    text = f"Here is the review:\n```json\n{_REVIEW_JSON}\n```\nDone."
    assert extract_json(text) == _REVIEW


def test_braced_prose_before_and_after_the_object_is_recovered():
    """THE RVW03-WS03 regression: a stray braced log line around the object
    broke the greedy ``{.*}`` capture (first brace to last brace spans the
    noise), failing the whole extraction — which silently dropped a dimension
    pass from the round. The balanced scan steps past non-JSON braces."""
    text = (
        "starting review {level: info, phase: scan}\n"
        f"{_REVIEW_JSON}\n"
        "done {elapsed: 3s}"
    )
    assert extract_json(text) == _REVIEW


def test_nested_braces_inside_the_object_are_balanced():
    text = f"prose before {{unbalanced\n{_REVIEW_JSON}"
    assert extract_json(text) == _REVIEW


def test_braces_inside_string_values_do_not_end_the_object():
    payload = dict(_REVIEW, note="use f(x) {and braces} } inside strings")
    text = f"wrapper says {{hi\n{json.dumps(payload)}\ntrailing }}"
    assert extract_json(text) == payload


def test_largest_object_wins_over_a_smaller_valid_fragment():
    """Wrapper prose can carry a SMALL valid JSON fragment (a structured log
    line) before the review — first-match would return the fragment; the scan
    returns the largest embedded object, the findings object."""
    text = f'{{"level": "info"}}\n{_REVIEW_JSON}\nbye'
    assert extract_json(text) == _REVIEW


def test_a_recovered_object_is_never_a_splice():
    """Two adjacent objects must come back as ONE of them, complete — never a
    span glued across both (the greedy-regex failure mode). The balanced scan
    parses each candidate with the real JSON decoder, so a splice cannot
    exist."""
    small = {"a": 1}
    text = f"{json.dumps(small)} {_REVIEW_JSON}"
    assert extract_json(text) == _REVIEW


def test_truncated_object_before_a_complete_one_is_stepped_past():
    truncated = _REVIEW_JSON[: len(_REVIEW_JSON) // 2]
    text = f"first attempt (cut off):\n{truncated}\nretry:\n{_REVIEW_JSON}"
    assert extract_json(text) == _REVIEW


def test_no_json_at_all_raises_value_error():
    with pytest.raises(ValueError, match="Could not parse valid JSON"):
        extract_json("no json here, just {braces} and prose")


def test_only_a_truncated_object_raises_value_error():
    with pytest.raises(ValueError, match="Could not parse valid JSON"):
        extract_json(_REVIEW_JSON[: len(_REVIEW_JSON) // 2])


#: A valid JSON object LARGER than the review but NOT review-shaped (no
#: ``summary`` object, no ``comments`` list) — a stray log/tool dump in noisy
#: reviewer stdout. Larger than ``_REVIEW_JSON`` so plain largest-wins would
#: select it.
_BIG_NON_REVIEW_JSON = json.dumps({"log": ["x" * 40] * 40})


def test_the_non_review_blob_is_larger_than_the_review():
    # Guards the premise of the `want`-selection tests below: without the shape
    # predicate, largest-wins WOULD pick this blob over the review.
    assert len(_BIG_NON_REVIEW_JSON) > len(_REVIEW_JSON)


def test_want_selects_the_review_over_a_larger_unrelated_blob():
    """Noisy stdout can carry a large unrelated JSON object (a log/tool dump)
    bigger than the review. Largest-wins alone returns it, and a dict with no
    ``comments`` reads downstream as a CLEAN, finding-less pass. ``want``
    (:func:`is_review_shaped`) selects the review-shaped candidate instead — the
    generic call still returns the larger blob, proving the predicate is what
    changes the selection (the calibrator's generic contract is untouched)."""
    text = f"{_BIG_NON_REVIEW_JSON}\n{_REVIEW_JSON}\ntrailing prose"
    assert extract_json(text, want=is_review_shaped) == _REVIEW
    assert extract_json(text) != _REVIEW


def test_want_raises_loudly_when_no_review_shaped_object_is_present():
    """A pass whose ONLY object is off-shape must fail LOUD (→ BackendError →
    the #76 salvage), never return the blob as a silent clean pass. This also
    exercises the fast-path gate: the blob parses directly, but ``want`` rejects
    it so the parse falls through to the scan, which finds nothing wanted."""
    with pytest.raises(ValueError, match="Could not parse valid JSON"):
        extract_json(_BIG_NON_REVIEW_JSON, want=is_review_shaped)


def test_is_review_shaped_predicate():
    assert is_review_shaped(_REVIEW)
    assert not is_review_shaped({"log": []})
    # `summary` present but not an object, or `comments` not a list → off-shape.
    assert not is_review_shaped({"summary": "ok", "comments": []})
    assert not is_review_shaped({"summary": {}, "comments": {}})


def test_deeply_nested_object_blob_raises_value_error_not_recursion_error():
    """Deeply nested untrusted output would exhaust the JSON decoder's recursion
    limit; the parser must degrade to a plain ``ValueError`` (→ BackendError),
    never a ``RecursionError`` crash — and must do so FAST, without re-decoding
    the blob from each of its interior braces."""
    blob = '{"a":' * 60000 + "1" + "}" * 60000
    with pytest.raises(ValueError, match="Could not parse valid JSON"):
        extract_json(blob)


def test_a_deeply_nested_array_blob_does_not_hide_a_following_review():
    """A pathological nested-array blob before the review must not crash the parse
    or shadow the real object — the scan steps over the brace-free array region
    and recovers the review after it."""
    blob = "[" * 5000 + "]" * 5000
    assert extract_json(f"{blob}\n{_REVIEW_JSON}") == _REVIEW
