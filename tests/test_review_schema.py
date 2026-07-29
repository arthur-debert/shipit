from __future__ import annotations

import json

import pytest

from shipit.review.schema import extract_json, is_review_shaped

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
    text = f'{{"level": "info"}}\n{_REVIEW_JSON}\nbye'
    assert extract_json(text) == _REVIEW


def test_a_recovered_object_is_never_a_splice():
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


_BIG_NON_REVIEW_JSON = json.dumps({"log": ["x" * 40] * 40})


def test_the_non_review_blob_is_larger_than_the_review():
    assert len(_BIG_NON_REVIEW_JSON) > len(_REVIEW_JSON)


def test_want_selects_the_review_over_a_larger_unrelated_blob():
    text = f"{_BIG_NON_REVIEW_JSON}\n{_REVIEW_JSON}\ntrailing prose"
    assert extract_json(text, want=is_review_shaped) == _REVIEW
    assert extract_json(text) != _REVIEW


def test_want_raises_loudly_when_no_review_shaped_object_is_present():
    with pytest.raises(ValueError, match="Could not parse valid JSON"):
        extract_json(_BIG_NON_REVIEW_JSON, want=is_review_shaped)


def test_is_review_shaped_predicate():
    assert is_review_shaped(_REVIEW)
    assert not is_review_shaped({"log": []})
    assert not is_review_shaped({"summary": "ok", "comments": []})
    assert not is_review_shaped({"summary": {}, "comments": {}})


_DEEP_OBJECT = '{"a":' * 60000 + "1" + "}" * 60000


def test_deeply_nested_object_blob_raises_value_error_not_recursion_error():
    with pytest.raises(ValueError, match="Could not parse valid JSON"):
        extract_json(_DEEP_OBJECT)


def test_a_deeply_nested_object_blob_does_not_hide_a_following_review():
    text = f"{_DEEP_OBJECT}\n{_REVIEW_JSON}"
    assert extract_json(text) == _REVIEW
    assert extract_json(text, want=is_review_shaped) == _REVIEW


def test_a_truncated_deeply_nested_object_blob_stops_the_scan():
    truncated = '{"a":' * 60000
    with pytest.raises(ValueError, match="Could not parse valid JSON"):
        extract_json(f"{truncated}\n{_REVIEW_JSON}")


def test_a_deeply_nested_array_blob_does_not_hide_a_following_review():
    blob = "[" * 5000 + "]" * 5000
    assert extract_json(f"{blob}\n{_REVIEW_JSON}") == _REVIEW


def test_a_bare_non_object_json_value_is_not_accepted():
    for bare in ("[1, 2, 3]", '"just a string"', "42"):
        with pytest.raises(ValueError, match="Could not parse valid JSON"):
            extract_json(bare)


def _valid_review() -> dict:
    return json.loads(_REVIEW_JSON)


def test_validate_review_accepts_a_conforming_payload():
    from shipit.review.schema import validate_review

    assert validate_review(_valid_review()) == []


def test_validate_review_accepts_null_line_and_empty_skipped():
    from shipit.review.schema import validate_review

    review = _valid_review()
    review["comments"][0]["line"] = None
    assert validate_review(review) == []


def test_validate_review_rejects_a_non_object():
    from shipit.review.schema import validate_review

    problems = validate_review([1, 2, 3])
    assert len(problems) == 1
    assert "expected an object" in problems[0]


def test_validate_review_reports_missing_envelope_keys():
    from shipit.review.schema import validate_review

    problems = validate_review({})
    joined = "\n".join(problems)
    assert "missing required key 'summary'" in joined
    assert "missing required key 'comments'" in joined


def test_validate_review_rejects_a_free_form_severity():
    from shipit.review.schema import validate_review

    review = _valid_review()
    review["comments"][0]["severity"] = "blocker"
    problems = validate_review(review)
    assert any(
        "comments[0].severity" in p and "'blocker'" in p and "critical" in p
        for p in problems
    ), problems


def test_validate_review_rejects_a_bad_status_enum():
    from shipit.review.schema import validate_review

    review = _valid_review()
    review["summary"]["status"] = "LGTM"
    problems = validate_review(review)
    assert any("summary.status" in p and "'LGTM'" in p for p in problems), problems


def test_validate_review_rejects_a_bool_line():
    from shipit.review.schema import validate_review

    review = _valid_review()
    review["comments"][0]["line"] = True
    problems = validate_review(review)
    assert any("comments[0].line" in p for p in problems), problems


def test_validate_review_rejects_confidence_out_of_range():
    from shipit.review.schema import validate_review

    review = _valid_review()
    review["comments"][0]["confidence"] = 1.5
    problems = validate_review(review)
    assert any(
        "comments[0].confidence" in p and "out of range" in p for p in problems
    ), problems


def test_validate_review_rejects_a_non_number_confidence():
    from shipit.review.schema import validate_review

    review = _valid_review()
    review["comments"][0]["confidence"] = "high"
    problems = validate_review(review)
    assert any(
        "comments[0].confidence" in p and "expected a number" in p for p in problems
    ), problems


def test_validate_review_flags_unknown_keys():
    from shipit.review.schema import validate_review

    review = _valid_review()
    review["comments"][0]["blocking"] = True
    problems = validate_review(review)
    assert any("unexpected key 'blocking'" in p for p in problems), problems


def test_validate_review_reports_a_missing_comment_field_once():
    from shipit.review.schema import validate_review

    review = _valid_review()
    del review["comments"][0]["fix"]
    problems = validate_review(review)
    matches = [p for p in problems if "comments[0]" in p and "'fix'" in p]
    assert len(matches) == 1, problems
    assert "missing required key 'fix'" in matches[0]


def test_validate_review_validates_coverage_skipped_entries():
    from shipit.review.schema import validate_review

    review = _valid_review()
    review["summary"]["coverage"]["skipped"] = [{"file": "x.py"}]
    problems = validate_review(review)
    assert any(
        "summary.coverage.skipped[0]" in p and "'reason'" in p for p in problems
    ), problems


def test_validate_review_reports_every_problem_at_once():
    from shipit.review.schema import validate_review

    review = _valid_review()
    review["summary"]["status"] = "LGTM"
    review["comments"][0]["severity"] = "blocker"
    review["comments"][0]["confidence"] = 2
    problems = validate_review(review)
    assert len(problems) >= 3
