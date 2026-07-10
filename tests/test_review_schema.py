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

from shipit.review.schema import extract_json

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
