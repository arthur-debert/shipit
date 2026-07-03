"""Smoke tests for `shipit pr next` + `pr ready` CLI wiring — glue + renderers.

Proves the verbs register on the `pr` group and that `pr next`'s run shell
resolve → gather → evaluate → dispatch → render path fires the engine's act
boundary and renders through the seam. The acts themselves (reviewer selection,
the request delegation, the guarded flip) are the engine's and are unit-tested
in test_prstate_dispatch.py / test_prstate_flip.py; here the boundary
(resolver / gather / evaluate / the promoted services) is monkeypatched — no
network, no engine re-test.
"""

from __future__ import annotations

import json

import pytest

from shipit import cli
from shipit.identity import repo_from_slug
from shipit.pr import PrId
from shipit.prstate import dispatch as dispatch_mod
from shipit.prstate.flip import NotReady
from shipit.prstate.request import RequestResult, ReviewerOutcome
from shipit.prstate.state import ChecksState, TaskState, TaskStatus
from shipit.verbs.pr import next_action as next_verb

REPO = repo_from_slug("owner/repo")


def _status(state: TaskState, pr: int = 42, next_action: str = "do x") -> TaskStatus:
    return TaskStatus(
        state=state,
        next_action=next_action,
        pr=pr,
        reviewers={"copilot": "done_clean"},
        checks=ChecksState.GREEN,
        mergeable="MERGEABLE",
    )


# --- wiring ------------------------------------------------------------------


def test_pr_help_lists_next_and_ready(capsys):
    rc = cli.main(["pr", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "next" in out
    assert "ready" in out


def test_next_help(capsys):
    rc = cli.main(["pr", "next", "--help"])
    assert rc == 0
    assert "--json" in capsys.readouterr().out


def test_ready_help(capsys):
    rc = cli.main(["pr", "ready", "--help"])
    assert rc == 0
    assert "--undo" in capsys.readouterr().out


def test_next_malformed_pr_argument_is_usage_tier_exit_2(capsys):
    """The shared PR-target param (ADR-0030): a bad primitive dies at parse."""
    rc = cli.main(["pr", "next", "0"])
    assert rc == 2
    assert "Usage:" in capsys.readouterr().err


# --- pr next run shell -------------------------------------------------------


@pytest.fixture
def patched_next(monkeypatch):
    """resolve → the typed PrId target (#42); gather passes the target through;
    evaluate yields the state under test. The act boundary is the engine's real
    `NextActs` exercised via the dispatcher; tests that need a specific act stub
    the promoted service at the dispatch module."""
    monkeypatch.setattr(
        next_verb,
        "resolve_pr",
        lambda pr, repo: PrId(repo=repo, number=pr if pr is not None else 42),
    )
    monkeypatch.setattr(next_verb, "gather", lambda target: target)
    monkeypatch.setattr(next_verb, "required_reviewers", lambda: [])


def test_next_reports_blocked(patched_next, monkeypatch, capsys):
    monkeypatch.setattr(
        next_verb,
        "evaluate",
        lambda ctx, required: _status(
            TaskState.BLOCKED, ctx.number, "the real blocker"
        ),
    )
    rc = cli.main(["pr", "next"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "action:" in out
    assert "the real blocker" in out
    assert "blocked" in out


def test_next_json_carries_action_and_status(patched_next, monkeypatch, capsys):
    monkeypatch.setattr(
        next_verb,
        "evaluate",
        lambda ctx, required: _status(TaskState.VALIDATING, ctx.number),
    )
    rc = cli.main(["pr", "next", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "action" in payload
    assert payload["status"]["state"] == "validating"


def test_next_no_pr_is_exit_zero_report(monkeypatch, capsys):
    monkeypatch.setattr(next_verb, "resolve_pr", lambda pr, repo: None)
    rc = cli.main(["pr", "next", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"]["state"] == "no_pr"


def test_next_ready_flips(patched_next, monkeypatch, capsys):
    """READY routes to the flip act; the engine's guarded flip is stubbed to flip."""
    monkeypatch.setattr(
        next_verb,
        "evaluate",
        lambda ctx, required: _status(TaskState.READY, ctx.number),
    )
    monkeypatch.setattr(
        dispatch_mod,
        "guarded_flip",
        lambda target: _status(TaskState.READY, target.number),
    )
    rc = cli.main(["pr", "next"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "flipped draft→ready" in out


def test_next_ready_refusal_is_uniform_error_exit_1(patched_next, monkeypatch, capsys):
    """If the PR moved out of READY between gather and the guarded flip, the
    engine's NotReady reaches the shared error shell: `error: …` + exit 1."""
    monkeypatch.setattr(
        next_verb,
        "evaluate",
        lambda ctx, required: _status(TaskState.READY, ctx.number),
    )

    def refuse(target):
        raise NotReady(_status(TaskState.BLOCKED, target.number))

    monkeypatch.setattr(dispatch_mod, "guarded_flip", refuse)
    rc = cli.main(["pr", "next"])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "not Ready" in err


def test_next_request_act_renders_and_dropped_edge_is_error(
    patched_next, monkeypatch, capsys
):
    """REVIEWS_PENDING wiring: the engine's request act fires through the verb
    (happy path renders the requested reviewer; a dropped edge surfaces via the
    shell as `error: …` + exit 1). Selection/force semantics are unit-tested at
    the dispatch module's home."""

    class FakeAdapter:
        def __init__(self, name):
            self.name = name

    monkeypatch.setattr(
        dispatch_mod, "required_reviewers", lambda: [FakeAdapter("copilot")]
    )
    monkeypatch.setattr(
        next_verb,
        "evaluate",
        lambda ctx, required: TaskStatus(
            state=TaskState.REVIEWS_PENDING,
            next_action="request for the current head: copilot",
            pr=ctx.number,
            reviewers={"copilot": "not_requested"},
            to_request=["copilot"],
        ),
    )
    monkeypatch.setattr(
        dispatch_mod,
        "request_reviewers",
        lambda pr, adapters, *, force: RequestResult(
            outcomes=[ReviewerOutcome(a.name, "verified") for a in adapters]
        ),
    )
    rc = cli.main(["pr", "next"])
    assert rc == 0
    assert "requested review(s): copilot" in capsys.readouterr().out

    monkeypatch.setattr(
        dispatch_mod,
        "request_reviewers",
        lambda pr, adapters, *, force: RequestResult(
            outcomes=[ReviewerOutcome("copilot", "dropped")]
        ),
    )
    rc = cli.main(["pr", "next"])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "dropped" in err
    assert "copilot" in err
