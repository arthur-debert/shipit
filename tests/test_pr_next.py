"""Smoke tests for `shipit pr next` + `pr ready` CLI wiring (WS06).

Proves the verbs register on the `pr` group and that `pr next`'s run shell
resolve → gather → evaluate → dispatch → perform → report path fires the right
act and renders. The boundary (resolver / gather / evaluate / the acts) is
monkeypatched — no network, no engine re-test.
"""

from __future__ import annotations

import json

import pytest

from shipit import cli
from shipit.prstate.state import ChecksState, TaskState, TaskStatus
from shipit.verbs.pr import next_action as next_verb


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


# --- pr next run shell -------------------------------------------------------


@pytest.fixture
def patched_next(monkeypatch):
    """resolve → 42; gather passes the number; evaluate yields the state under
    test. The act boundary is the real `_NextActs` but its methods are exercised
    via the dispatcher; tests that need a specific act stub it directly."""
    monkeypatch.setattr(
        next_verb, "resolve_pr", lambda pr: pr if pr is not None else 42
    )
    monkeypatch.setattr(next_verb, "gather", lambda pr: pr)
    monkeypatch.setattr(next_verb, "required_reviewers", lambda: [])


def test_next_reports_blocked(patched_next, monkeypatch, capsys):
    monkeypatch.setattr(
        next_verb,
        "evaluate",
        lambda ctx, required: _status(TaskState.BLOCKED, ctx, "the real blocker"),
    )
    rc = cli.main(["pr", "next"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "action:" in out
    assert "the real blocker" in out
    assert "blocked" in out


def test_next_json_carries_action_and_status(patched_next, monkeypatch, capsys):
    monkeypatch.setattr(
        next_verb, "evaluate", lambda ctx, required: _status(TaskState.VALIDATING, ctx)
    )
    rc = cli.main(["pr", "next", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "action" in payload
    assert payload["status"]["state"] == "validating"


def test_next_no_pr_is_exit_zero_report(monkeypatch, capsys):
    monkeypatch.setattr(next_verb, "resolve_pr", lambda pr: None)
    rc = cli.main(["pr", "next", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"]["state"] == "no_pr"


def test_next_ready_flips(patched_next, monkeypatch, capsys):
    """READY routes to the flip act; the guarded flip is stubbed to flip."""
    monkeypatch.setattr(
        next_verb, "evaluate", lambda ctx, required: _status(TaskState.READY, ctx)
    )
    monkeypatch.setattr(
        next_verb.ready_verb, "guarded_flip", lambda pr: _status(TaskState.READY, pr)
    )
    rc = cli.main(["pr", "next"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "flipped draft→ready" in out


def test_next_ready_refusal_is_nonzero(patched_next, monkeypatch, capsys):
    """If the PR moved out of READY between gather and the guarded flip, refuse."""
    monkeypatch.setattr(
        next_verb, "evaluate", lambda ctx, required: _status(TaskState.READY, ctx)
    )

    def refuse(pr):
        raise next_verb.ready_verb.NotReady(_status(TaskState.BLOCKED, pr))

    monkeypatch.setattr(next_verb.ready_verb, "guarded_flip", refuse)
    rc = cli.main(["pr", "next"])
    assert rc != 0
    assert "refusing to flip" in capsys.readouterr().err


def test_next_request_act_requests_reviewer(patched_next, monkeypatch, capsys):
    """REVIEWS_PENDING with a reviewer to request fires the request act, which
    routes through the adapter + a basic attach check (both stubbed)."""

    class FakeAdapter:
        name = "copilot"

        def request(self, pr):
            return True

        def matches(self, login):
            return "copilot" in login

    monkeypatch.setattr(next_verb, "required_reviewers", lambda: [FakeAdapter()])
    monkeypatch.setattr(
        next_verb,
        "evaluate",
        lambda ctx, required: TaskStatus(
            state=TaskState.REVIEWS_PENDING,
            next_action="waiting on required review(s): copilot — request for the current head: copilot",
            pr=ctx,
            reviewers={"copilot": "not_requested"},
        ),
    )
    monkeypatch.setattr(next_verb, "attach_state", lambda pr: (["Copilot"], []))
    rc = cli.main(["pr", "next"])
    assert rc == 0
    assert "requested review(s): copilot" in capsys.readouterr().out


def test_next_request_act_skips_already_requested_reviewer(
    patched_next, monkeypatch, capsys
):
    """A MIXED REVIEWS_PENDING (one not_requested, one already requested) must
    request ONLY the not_requested reviewer — never re-poke a reviewer already
    mid-review (Copilot review on PR #19)."""

    class FakeAdapter:
        def __init__(self, name):
            self.name = name
            self.requested_for = None

        def request(self, pr):
            self.requested_for = pr
            return True

        def matches(self, login):
            return self.name in login.lower()

    fresh = FakeAdapter("copilot")  # not_requested → should be requested
    busy = FakeAdapter("coderabbit")  # requested → should be SKIPPED
    monkeypatch.setattr(next_verb, "required_reviewers", lambda: [fresh, busy])
    monkeypatch.setattr(
        next_verb,
        "evaluate",
        lambda ctx, required: TaskStatus(
            state=TaskState.REVIEWS_PENDING,
            next_action=(
                "waiting on required review(s): copilot, coderabbit — "
                "request for the current head: copilot; "
                "wait (already requested on the current head): coderabbit"
            ),
            pr=ctx,
            reviewers={"copilot": "not_requested", "coderabbit": "requested"},
        ),
    )
    monkeypatch.setattr(next_verb, "attach_state", lambda pr: (["Copilot"], []))
    rc = cli.main(["pr", "next"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "requested review(s): copilot" in out
    assert "coderabbit" not in out.split("action:")[1].split("\n")[0]
    assert fresh.requested_for == 42  # the fresh one was requested
    assert busy.requested_for is None  # the busy one was NOT re-poked
