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
from shipit.identity import repo_from_slug
from shipit.pr import PrId
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


# --- pr next run shell -------------------------------------------------------


@pytest.fixture
def patched_next(monkeypatch):
    """resolve → the typed PrId target (#42); gather passes the target through;
    evaluate yields the state under test. The act boundary is the real
    `_NextActs` but its methods are exercised via the dispatcher; tests that
    need a specific act stub it directly."""
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
    """READY routes to the flip act; the guarded flip is stubbed to flip."""
    monkeypatch.setattr(
        next_verb,
        "evaluate",
        lambda ctx, required: _status(TaskState.READY, ctx.number),
    )
    monkeypatch.setattr(
        next_verb.ready_verb,
        "guarded_flip",
        lambda target: _status(TaskState.READY, target.number),
    )
    rc = cli.main(["pr", "next"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "flipped draft→ready" in out


def test_next_ready_refusal_is_nonzero(patched_next, monkeypatch, capsys):
    """If the PR moved out of READY between gather and the guarded flip, refuse."""
    monkeypatch.setattr(
        next_verb,
        "evaluate",
        lambda ctx, required: _status(TaskState.READY, ctx.number),
    )

    def refuse(target):
        raise next_verb.ready_verb.NotReady(_status(TaskState.BLOCKED, target.number))

    monkeypatch.setattr(next_verb.ready_verb, "guarded_flip", refuse)
    rc = cli.main(["pr", "next"])
    assert rc != 0
    assert "refusing to flip" in capsys.readouterr().err


class FakeAdapter:
    def __init__(self, name):
        self.name = name

    def matches(self, login):
        return self.name in login.lower()


def _fake_request_result(names):
    """A RequestResult whose `verified` are the given names — `ok` is True."""
    from shipit.verbs.pr._request import RequestResult, ReviewerOutcome

    return RequestResult(outcomes=[ReviewerOutcome(n, "verified") for n in names])


def test_next_request_act_requests_reviewer(patched_next, monkeypatch, capsys):
    """REVIEWS_PENDING with a reviewer to request fires the request act, which
    delegates execution to WS05's `request_reviewers` (attach-verify)."""
    monkeypatch.setattr(
        next_verb, "required_reviewers", lambda: [FakeAdapter("copilot")]
    )
    monkeypatch.setattr(
        next_verb,
        "evaluate",
        lambda ctx, required: TaskStatus(
            state=TaskState.REVIEWS_PENDING,
            next_action="waiting on required review(s): copilot — request for the current head: copilot",
            pr=ctx.number,
            reviewers={"copilot": "not_requested"},
            to_request=["copilot"],
        ),
    )
    seen = {}

    def fake_request(pr, adapters, *, force):
        seen["pr"] = pr
        seen["names"] = [a.name for a in adapters]
        seen["force"] = force
        return _fake_request_result([a.name for a in adapters])

    monkeypatch.setattr(next_verb, "request_reviewers", fake_request)
    rc = cli.main(["pr", "next"])
    assert rc == 0
    assert "requested review(s): copilot" in capsys.readouterr().out
    # The act hands the request helper the TYPED target — the same PrId the
    # resolver minted (repo + number), never a bare int.
    assert seen["pr"].number == 42
    assert seen["pr"].repo is not None
    assert (seen["names"], seen["force"]) == (["copilot"], True)


def test_next_request_act_skips_already_requested_reviewer(
    patched_next, monkeypatch, capsys
):
    """A MIXED REVIEWS_PENDING (one not_requested, one already requested) must
    SELECT only the not_requested reviewer for the request helper — never re-poke
    a reviewer already mid-review (Copilot review on PR #19). The selection is
    what `request_reviewers` receives; execution is delegated to that helper."""
    monkeypatch.setattr(
        next_verb,
        "required_reviewers",
        lambda: [FakeAdapter("copilot"), FakeAdapter("coderabbit")],
    )
    monkeypatch.setattr(
        next_verb,
        "evaluate",
        lambda ctx, required: TaskStatus(
            state=TaskState.REVIEWS_PENDING,
            next_action=(
                "waiting on required review(s): copilot, coderabbit — "
                "request for the current head: copilot; "
                "wait (already requested / in flight on the current head): coderabbit"
            ),
            pr=ctx.number,
            reviewers={"copilot": "not_requested", "coderabbit": "requested"},
            to_request=["copilot"],
        ),
    )
    selected = {}

    def fake_request(pr, adapters, *, force):
        selected["names"] = [a.name for a in adapters]
        return _fake_request_result([a.name for a in adapters])

    monkeypatch.setattr(next_verb, "request_reviewers", fake_request)
    rc = cli.main(["pr", "next"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "requested review(s): copilot" in out
    # Selection excluded the mid-review reviewer — only copilot reached the helper.
    assert selected["names"] == ["copilot"]
    assert "coderabbit" not in out.split("action:")[1].split("\n")[0]


def test_next_request_act_excludes_in_flight_local_agent(
    patched_next, monkeypatch, capsys
):
    """OBS04 convergence regression: a required LOCAL-agent reviewer genuinely
    IN_FLIGHT (its `review: codex-local` check run still running) reads lifecycle
    `not_requested` — a local agent has no native `review_requested` edge — so the
    OLD lifecycle-based selection (`not_requested` ∉ skip) would re-poke it
    mid-review. The act now consumes the engine's `to_request`, which EXCLUDES the
    in-flight reviewer; only the genuinely never-requested copilot is selected and
    reaches the helper."""
    monkeypatch.setattr(
        next_verb,
        "required_reviewers",
        lambda: [FakeAdapter("copilot"), FakeAdapter("codex")],
    )
    monkeypatch.setattr(
        next_verb,
        "evaluate",
        lambda ctx, required: TaskStatus(
            state=TaskState.REVIEWS_PENDING,
            next_action=(
                "waiting on required review(s): copilot, codex — "
                "request for the current head: copilot; "
                "wait (already requested / in flight on the current head): codex"
            ),
            pr=ctx.number,
            # codex's detached run is in-flight, but its lifecycle reads
            # not_requested (no requested edge) — the OLD selection would pick it.
            reviewers={"copilot": "not_requested", "codex": "not_requested"},
            # the engine already excluded the in-flight codex; only copilot needs it.
            to_request=["copilot"],
        ),
    )
    selected = {}

    def fake_request(pr, adapters, *, force):
        selected["names"] = [a.name for a in adapters]
        return _fake_request_result([a.name for a in adapters])

    monkeypatch.setattr(next_verb, "request_reviewers", fake_request)
    rc = cli.main(["pr", "next"])
    assert rc == 0
    # Only the never-requested reviewer reached the helper — codex was NOT re-poked.
    assert selected["names"] == ["copilot"]
    out = capsys.readouterr().out
    assert "requested review(s): copilot" in out
    assert "codex" not in out.split("action:")[1].split("\n")[0]


def test_next_request_act_selects_never_requested_and_stale(
    patched_next, monkeypatch, capsys
):
    """The act selects EVERY name the engine placed in `to_request` — both a
    never-requested reviewer and a stale-after-push (RE-REQUEST) one. At the act
    seam both read lifecycle `not_requested`; the engine's `to_request` is the
    authority, so both reach the helper."""
    monkeypatch.setattr(
        next_verb,
        "required_reviewers",
        lambda: [FakeAdapter("copilot"), FakeAdapter("coderabbit")],
    )
    monkeypatch.setattr(
        next_verb,
        "evaluate",
        lambda ctx, required: TaskStatus(
            state=TaskState.REVIEWS_PENDING,
            next_action=(
                "waiting on required review(s): copilot, coderabbit — "
                "request for the current head: copilot; "
                "RE-REQUEST for the current head (a prior review is stale after a "
                "push): coderabbit"
            ),
            pr=ctx.number,
            reviewers={"copilot": "not_requested", "coderabbit": "not_requested"},
            to_request=["copilot", "coderabbit"],
        ),
    )
    selected = {}

    def fake_request(pr, adapters, *, force):
        selected["names"] = [a.name for a in adapters]
        return _fake_request_result([a.name for a in adapters])

    monkeypatch.setattr(next_verb, "request_reviewers", fake_request)
    rc = cli.main(["pr", "next"])
    assert rc == 0
    assert selected["names"] == ["copilot", "coderabbit"]
    assert "requested review(s): copilot, coderabbit" in capsys.readouterr().out


def test_next_request_act_dropped_edge_is_error(patched_next, monkeypatch, capsys):
    """A silently-dropped request edge (#614) → non-zero exit, named in stderr."""
    monkeypatch.setattr(
        next_verb, "required_reviewers", lambda: [FakeAdapter("copilot")]
    )
    monkeypatch.setattr(
        next_verb,
        "evaluate",
        lambda ctx, required: TaskStatus(
            state=TaskState.REVIEWS_PENDING,
            next_action="waiting on required review(s): copilot — request for the current head: copilot",
            pr=ctx.number,
            reviewers={"copilot": "not_requested"},
            to_request=["copilot"],
        ),
    )
    from shipit.verbs.pr._request import RequestResult, ReviewerOutcome

    monkeypatch.setattr(
        next_verb,
        "request_reviewers",
        lambda pr, adapters, *, force: RequestResult(
            outcomes=[ReviewerOutcome("copilot", "dropped")]
        ),
    )
    rc = cli.main(["pr", "next"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "dropped" in err
    assert "copilot" in err
