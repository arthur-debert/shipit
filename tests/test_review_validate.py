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
    off_shape = json.dumps(
        {"findings": [{"file": "x", "line": 1, "severity": "nit", "description": "y"}]}
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(off_shape))
    rc = review_verb.run_validate(None)
    assert rc == 1
    err = capsys.readouterr().err
    assert "missing required key 'summary'" in err
    assert "missing required key 'comments'" in err
    assert "unexpected key 'findings'" in err
    assert "no JSON object could be extracted" not in err


def test_validate_diagnoses_empty_and_wrong_typed_envelope(monkeypatch, capsys):
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
    rc = review_verb.run_validate("/no/such/review.json")
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "cannot read review JSON" in err
