"""Tests for `shipit review validate` — the top-level, backend-agnostic agent
self-check verb (#826).

The verb was moved OUT of `pr review` to the top level (a schema check touches no
PR state). These prove the WIRING: read stdin/FILE -> the funnel's own tolerant
parse -> `validate_review` -> `valid`/exit 0 or the path-anchored problems/exit 1,
and the unreadable-file / no-JSON failure paths surfacing cleanly. The key case is
that a syntactically valid but OFF-SHAPE payload (the #825 `{"findings": …}`) is
DIAGNOSED with real schema problems, not bounced with a generic parse error — that
is the whole point of the command. The validator itself is unit-tested in
test_review_schema.py.
"""

from __future__ import annotations

import io
import json

from shipit.verbs import review as review_verb


def _valid_review_json() -> str:
    return json.dumps(
        {
            "summary": {
                "status": "COMMENT",
                "overall_feedback": "ok",
                "coverage": {"reviewed": ["a.py"], "skipped": []},
            },
            "comments": [
                {
                    "file": "a.py",
                    "line": 3,
                    "text": "off-by-one",
                    "severity": "major",
                    "category": "correctness",
                    "confidence": 0.9,
                    "evidence": "range(n + 1)",
                    "fix": "range(n)",
                }
            ],
        }
    )


def test_validate_accepts_conforming_stdin(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(_valid_review_json()))
    rc = review_verb.run_validate(None)
    assert rc == 0
    assert capsys.readouterr().out.strip() == "valid"


def test_validate_tolerates_fences_and_prose_around_the_json(monkeypatch, capsys):
    """Same tolerant parse the funnel uses: fences + prose are stripped."""
    wrapped = f"Here it is:\n```json\n{_valid_review_json()}\n```\nDone."
    monkeypatch.setattr("sys.stdin", io.StringIO(wrapped))
    assert review_verb.run_validate(None) == 0
    assert capsys.readouterr().out.strip() == "valid"


def test_validate_rejects_bad_severity_with_exit_1(monkeypatch, capsys):
    payload = json.loads(_valid_review_json())
    payload["comments"][0]["severity"] = "blocker"
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = review_verb.run_validate(None)
    assert rc == 1
    err = capsys.readouterr().err
    assert "comments[0].severity" in err
    assert "'blocker'" in err


def test_validate_diagnoses_a_valid_but_off_shape_payload(monkeypatch, capsys):
    """The whole point of the command (#826): a syntactically VALID but OFF-SHAPE
    payload — the literal #825 failure `{"findings": [...]}` — must reach
    `validate_review` and get the actionable schema problems (missing `summary`,
    missing `comments`, unexpected key `findings`), NOT the generic
    no-JSON-found bounce that `want=is_review_shaped` alone would produce."""
    off_shape = json.dumps(
        {"findings": [{"file": "x", "line": 1, "severity": "nit", "description": "y"}]}
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(off_shape))
    rc = review_verb.run_validate(None)
    assert rc == 1
    err = capsys.readouterr().err
    # The real schema problems, not the generic parse-failure line.
    assert "missing required key 'summary'" in err
    assert "missing required key 'comments'" in err
    assert "unexpected key 'findings'" in err
    assert "no JSON object could be extracted" not in err


def test_validate_diagnoses_empty_and_wrong_typed_envelope(monkeypatch, capsys):
    """`{}` and `{"summary": {}, "comments": {}}` are valid JSON but off-shape —
    both flow into the schema check and report the specific structural problems."""
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert review_verb.run_validate(None) == 1
    err = capsys.readouterr().err
    assert "missing required key 'summary'" in err
    assert "missing required key 'comments'" in err

    monkeypatch.setattr("sys.stdin", io.StringIO('{"summary": {}, "comments": {}}'))
    assert review_verb.run_validate(None) == 1
    err = capsys.readouterr().err
    assert "comments: expected an array, got object" in err


def test_validate_unparseable_input_is_a_clean_failure(monkeypatch, capsys):
    """Truly-unparseable input (no JSON object at all) still gives the clean
    no-JSON message and exit 1 — never a crash."""
    monkeypatch.setattr("sys.stdin", io.StringIO("not json at all"))
    rc = review_verb.run_validate(None)
    assert rc == 1
    out = capsys.readouterr()
    assert "invalid: no JSON object could be extracted" in out.err
    assert out.err.count("Traceback") == 0


def test_validate_reads_a_file(tmp_path, capsys):
    path = tmp_path / "review.json"
    path.write_text(_valid_review_json(), encoding="utf-8")
    assert review_verb.run_validate(str(path)) == 0
    assert capsys.readouterr().out.strip() == "valid"


def test_validate_unreadable_file_is_a_clean_error(capsys):
    """The local file-read failure raises `ReviewError` (the fitting domain
    exception, as `run_replay` uses for its instructions-file read) and renders
    as one clean `error: …` line through the shared shell — never a crash."""
    rc = review_verb.run_validate("/no/such/review.json")
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "cannot read review JSON" in err
