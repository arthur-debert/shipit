from __future__ import annotations

import hashlib
import json

import pytest

from shipit import cli
from shipit.finding import Severity
from shipit.identity import Sha, repo_from_slug
from shipit.pr import PrId
from shipit.prstate.model import Review, ReviewComment, Thread, readiness_view
from shipit.prstate.reviewers_config import default_roster
from shipit.verbs.pr import classify as classify_verb

REPO = repo_from_slug("owner/repo")
HEAD = Sha(hashlib.sha1(b"head").hexdigest())

BODY_1 = "<!-- shipit:finding severity=nit -->\nnitpick: capitalize the sentence"
BODY_2 = "consider referencing the CLI02 PRD inline"


def _thread(cid: int, body: str, review_id: int = 900) -> Thread:
    comment = ReviewComment(
        comment_id=cid,
        path="docs/dev-cycle.lex",
        line=cid % 100,
        body=body,
        author="Copilot",
        review_id=review_id,
    )
    return Thread(thread_id=f"PRT_{cid}", is_resolved=True, comments=(comment,))


def _ctx(overrides=None, threads=None):
    return readiness_view(
        number=42,
        head_sha=HEAD,
        is_draft=True,
        repo=REPO,
        reviews=[
            Review(
                review_id=900,
                author="Copilot",
                state="COMMENTED",
                commit_id=HEAD,
                body="",
            )
        ],
        threads=threads
        if threads is not None
        else [_thread(101, BODY_1), _thread(202, BODY_2)],
        overrides=overrides or {},
        roster=default_roster(),
    )


@pytest.fixture
def recorded():
    return []


@pytest.fixture
def patched(monkeypatch, recorded):
    def resolve(pr, repo, branch):
        assert repo is not None
        return PrId(repo=REPO, number=pr if pr is not None else 42)

    monkeypatch.setattr(classify_verb, "resolve_pr", resolve)
    monkeypatch.setattr(classify_verb, "load_roster", lambda: default_roster())
    monkeypatch.setattr(classify_verb, "gather", lambda target, roster: _ctx())
    monkeypatch.setattr(
        classify_verb,
        "record_override",
        lambda repo, pr, cid, severity, reason=None: recorded.append(
            (repo, pr, cid, severity, reason)
        ),
    )


def test_pr_help_lists_classify(capsys):
    rc = cli.main(["pr", "--help"])
    assert rc == 0
    assert "classify" in capsys.readouterr().out


def test_list_mode_prints_resolved_severities_with_the_override_command(
    patched, capsys
):
    rc = cli.main(["pr", "classify"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "2 finding(s), severity-resolved" in out
    assert "101" in out and "202" in out
    assert "nit (marker)" in out
    assert "minor (policy)" in out
    assert BODY_2 in out
    assert "shipit pr classify 42 --comment <id> {critical|major|minor|nit}" in out


def test_list_mode_json_carries_ids_severities_and_sources(patched, capsys):
    rc = cli.main(["pr", "classify", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pr"] == 42
    assert payload["round"] == 1
    assert [f["comment_id"] for f in payload["findings"]] == [101, 202]
    assert payload["findings"][0]["severity"] == "nit"
    assert payload["findings"][0]["source"] == "marker"
    assert payload["findings"][1]["severity"] == "minor"
    assert payload["findings"][1]["source"] == "policy"


def test_list_mode_shows_a_recorded_override_as_the_source(
    patched, monkeypatch, capsys
):
    monkeypatch.setattr(
        classify_verb,
        "gather",
        lambda target, roster: _ctx(overrides={202: Severity.MINOR}),
    )
    rc = cli.main(["pr", "classify", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["findings"][1]["severity"] == "minor"
    assert payload["findings"][1]["source"] == "override"


def test_list_mode_reports_no_round(patched, monkeypatch, capsys):
    monkeypatch.setattr(
        classify_verb,
        "gather",
        lambda target, roster: readiness_view(
            number=42, head_sha=HEAD, is_draft=True, repo=REPO, roster=default_roster()
        ),
    )
    rc = cli.main(["pr", "classify"])
    assert rc == 0
    assert "no review round yet" in capsys.readouterr().out


def test_record_mode_writes_the_override_once(patched, recorded, capsys):
    rc = cli.main(
        ["pr", "classify", "--comment", "202", "nit", "--reason", "grammar only"]
    )
    assert rc == 0
    assert recorded == [(REPO, 42, 202, Severity.NIT, "grammar only")]
    out = capsys.readouterr().out
    assert "severity of finding 202" in out
    assert "overridden to nit" in out


def test_record_refuses_a_comment_outside_the_latest_round(patched, recorded, capsys):
    rc = cli.main(["pr", "classify", "--comment", "999", "nit"])
    assert rc == 1
    assert recorded == []
    err = capsys.readouterr().err
    assert "error:" in err
    assert "not a finding of the latest review round" in err


def test_record_severity_is_a_usage_validated_choice(patched, capsys):
    assert cli.main(["pr", "classify", "--comment", "101", "cosmetic"]) == 2
    assert cli.main(["pr", "classify", "--comment", "101", "nitpick"]) == 2
    assert cli.main(["pr", "classify", "--comment", "101", "substantive"]) == 2


def test_reason_without_comment_is_refused(patched, capsys):
    rc = cli.main(["pr", "classify", "--reason", "stray"])
    assert rc == 1
    assert "pass --comment too" in capsys.readouterr().err


def test_no_pr_is_an_error(patched, monkeypatch, capsys):
    monkeypatch.setattr(classify_verb, "resolve_pr", lambda pr, repo, branch: None)
    rc = cli.main(["pr", "classify"])
    assert rc == 1
    assert "no PR for this branch" in capsys.readouterr().err
