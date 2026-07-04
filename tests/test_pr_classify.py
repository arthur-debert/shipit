"""The `shipit pr classify` CLI surface (#423) — through the ADR-0030 seam.

These prove the WIRING and the verb's own rules (list vs record mode, the
latest-round membership check, the no-PR refusal, the exit contract) — the
verdict STORE is unit-tested in test_prstate_verdicts.py and the engine's gate
in test_prstate_breakers.py. The boundary (`resolve_pr` / `gather` /
`load_roster` / `record_verdict`) is monkeypatched so there is no network and
no real log file.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from shipit import cli
from shipit.identity import Sha, repo_from_slug
from shipit.pr import PrId
from shipit.prstate.model import readiness_view, Review, ReviewComment, Thread
from shipit.prstate.reviewers_config import default_roster
from shipit.verbs.pr import classify as classify_verb

REPO = repo_from_slug("owner/repo")
HEAD = Sha(hashlib.sha1(b"head").hexdigest())

# Real #412 finding bodies — untagged Copilot cosmetics.
BODY_1 = "capitalize the first word of the sentence for correct English grammar"
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


def _ctx(verdicts=None, threads=None):
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
        verdicts=verdicts or {},
        # The SHIPPED default roster (copilot-only) as a value: `build_rounds`
        # derives its required set from `ctx.roster`, and the empty Roster
        # requires no one — no reviews would fold into rounds.
        roster=default_roster(),
    )


@pytest.fixture
def recorded():
    """The record_verdict spy's call log."""
    return []


@pytest.fixture
def patched(monkeypatch, recorded):
    def resolve(pr, repo, branch):
        assert repo is not None  # the ambient identity arrived at the boundary
        # Pin the target's repo so assertions on the record spy are stable
        # regardless of which checkout runs the suite.
        return PrId(repo=REPO, number=pr if pr is not None else 42)

    monkeypatch.setattr(classify_verb, "resolve_pr", resolve)
    monkeypatch.setattr(classify_verb, "load_roster", lambda: default_roster())
    monkeypatch.setattr(classify_verb, "gather", lambda target, roster: _ctx())
    monkeypatch.setattr(
        classify_verb,
        "record_verdict",
        lambda repo, pr, cid, verdict, reason=None: recorded.append(
            (repo, pr, cid, verdict, reason)
        ),
    )


def test_pr_help_lists_classify(capsys):
    rc = cli.main(["pr", "--help"])
    assert rc == 0
    assert "classify" in capsys.readouterr().out


def test_list_mode_prints_unclassified_findings_with_the_record_command(
    patched, capsys
):
    rc = cli.main(["pr", "classify"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "2 unclassified finding(s) of 2" in out
    assert "101" in out and "202" in out
    assert BODY_2 in out
    assert "shipit pr classify 42 --comment <id> nitpick|substantive" in out


def test_list_mode_json_carries_ids_and_excerpts(patched, capsys):
    rc = cli.main(["pr", "classify", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pr"] == 42
    assert payload["round"] == 1
    assert payload["total"] == 2
    assert [f["comment_id"] for f in payload["unclassified"]] == [101, 202]
    assert payload["unclassified"][0]["excerpt"] == BODY_1


def test_list_mode_reports_fully_classified(patched, monkeypatch, capsys):
    monkeypatch.setattr(
        classify_verb,
        "gather",
        lambda target, roster: _ctx(verdicts={101: "nitpick", 202: "nitpick"}),
    )
    rc = cli.main(["pr", "classify"])
    assert rc == 0
    assert "all 2 finding(s) of round 1 are classified" in capsys.readouterr().out


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


def test_record_mode_writes_the_verdict_once(patched, recorded, capsys):
    rc = cli.main(
        ["pr", "classify", "--comment", "101", "nitpick", "--reason", "grammar only"]
    )
    assert rc == 0
    assert recorded == [(REPO, 42, 101, "nitpick", "grammar only")]
    out = capsys.readouterr().out
    assert "classified finding 101 as nitpick" in out
    assert "1 unclassified finding(s) remaining" in out


def test_record_mode_reports_the_fully_classified_round(
    patched, monkeypatch, recorded, capsys
):
    monkeypatch.setattr(
        classify_verb,
        "gather",
        lambda target, roster: _ctx(verdicts={202: "nitpick"}),
    )
    rc = cli.main(["pr", "classify", "--comment", "101", "substantive"])
    assert rc == 0
    assert "the latest round is fully classified" in capsys.readouterr().out


def test_record_refuses_a_comment_outside_the_latest_round(patched, recorded, capsys):
    rc = cli.main(["pr", "classify", "--comment", "999", "nitpick"])
    assert rc == 1
    assert recorded == []
    err = capsys.readouterr().err
    assert "error:" in err
    assert "not a finding of the latest review round" in err


def test_record_verdict_is_a_usage_validated_choice(patched, capsys):
    # The verdict half of --comment validates at argv parse: exit 2, never verb code.
    rc = cli.main(["pr", "classify", "--comment", "101", "cosmetic"])
    assert rc == 2


def test_reason_without_comment_is_refused(patched, capsys):
    rc = cli.main(["pr", "classify", "--reason", "stray"])
    assert rc == 1
    assert "pass --comment too" in capsys.readouterr().err


def test_no_pr_is_an_error(patched, monkeypatch, capsys):
    monkeypatch.setattr(classify_verb, "resolve_pr", lambda pr, repo, branch: None)
    rc = cli.main(["pr", "classify"])
    assert rc == 1
    assert "no PR for this branch" in capsys.readouterr().err
