"""The `shipit pr push-gate` pre-push tripwire (#423).

Exit-code contract only: 1 with the CLASSIFY message on stderr while the
current branch's PR has unclassified findings in its latest round; 0 in every
pass state (no PR, no round, empty round, fully classified, round-cap) and on
any inability to evaluate (fail OPEN — the hook must never block git on a
broken read; the `pr next`/`pr status` gate is the arbiter). The boundary is
monkeypatched; the engine pieces it consumes are unit-tested in
test_prstate_breakers.py.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from shipit import cli
from shipit.execrun import ExecError
from shipit.identity import Sha, repo_from_slug
from shipit.pr import PrId
from shipit.prstate.model import Review, ReviewComment, Thread, readiness_view
from shipit.prstate.reviewers_config import default_roster
from shipit.verbs.pr import push_gate as gate_verb

REPO = repo_from_slug("owner/repo")
HEAD = Sha(hashlib.sha1(b"head").hexdigest())


def _thread(cid: int, review_id: int = 900, resolved: bool = True) -> Thread:
    comment = ReviewComment(
        comment_id=cid,
        path="a.py",
        line=1,
        body="consider referencing the CLI02 PRD inline",
        author="Copilot",
        review_id=review_id,
    )
    return Thread(thread_id=f"PRT_{cid}", is_resolved=resolved, comments=(comment,))


def _ctx(reviews=None, threads=None, verdicts=None, roster=None):
    return readiness_view(
        number=42,
        head_sha=HEAD,
        is_draft=True,
        repo=REPO,
        reviews=reviews
        if reviews is not None
        else [
            Review(
                review_id=900,
                author="Copilot",
                state="COMMENTED",
                commit_id=HEAD,
                body="",
            )
        ],
        threads=threads if threads is not None else [_thread(101)],
        verdicts=verdicts or {},
        roster=roster if roster is not None else default_roster(),
    )


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(
        gate_verb,
        "resolve_pr",
        lambda pr, repo, branch: PrId(repo=repo, number=42),
    )
    monkeypatch.setattr(gate_verb, "load_roster", lambda: default_roster())
    monkeypatch.setattr(gate_verb, "gather", lambda target, roster: _ctx())


def test_trips_on_an_unclassified_latest_round(patched, capsys):
    rc = cli.main(["pr", "push-gate"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "push blocked (pr#42)" in err
    assert "1 unclassified finding(s)" in err
    assert "shipit pr classify 42" in err


def test_passes_once_the_round_is_classified(patched, monkeypatch, capsys):
    # Trips at most once per round: the verdicts are durable, so a classified
    # round passes for good.
    monkeypatch.setattr(
        gate_verb, "gather", lambda target, roster: _ctx(verdicts={101: "nitpick"})
    )
    assert cli.main(["pr", "push-gate"]) == 0
    assert capsys.readouterr().err == ""


def test_passes_with_no_pr_for_the_branch(patched, monkeypatch):
    monkeypatch.setattr(gate_verb, "resolve_pr", lambda pr, repo, branch: None)
    assert cli.main(["pr", "push-gate"]) == 0


def test_passes_with_no_review_round(patched, monkeypatch):
    monkeypatch.setattr(
        gate_verb, "gather", lambda target, roster: _ctx(reviews=[], threads=[])
    )
    assert cli.main(["pr", "push-gate"]) == 0


def test_passes_on_an_empty_round(patched, monkeypatch):
    # A clean/approving pass leaves no findings — nothing to classify.
    monkeypatch.setattr(gate_verb, "gather", lambda target, roster: _ctx(threads=[]))
    assert cli.main(["pr", "push-gate"]) == 0


def test_passes_at_the_round_cap(patched, monkeypatch):
    # At the cap the mechanical stop owns the outcome — no verdict can decide
    # anything, so an unclassified capped round must not block the push (the
    # same exception the engine's gate makes).
    capped = replace(default_roster(), round_cap=1)
    monkeypatch.setattr(gate_verb, "gather", lambda target, roster: _ctx(roster=capped))
    assert cli.main(["pr", "push-gate"]) == 0


def test_fails_open_when_the_read_path_is_broken(patched, monkeypatch):
    # A gh outage / auth failure must never block git: exit 0, arbiter intact.
    def boom(target, roster):
        raise ExecError(["gh", "api"], rc=1, stderr="gh: network unreachable")

    monkeypatch.setattr(gate_verb, "gather", boom)
    assert cli.main(["pr", "push-gate"]) == 0
