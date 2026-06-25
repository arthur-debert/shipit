"""Tests for `shipit pr review request` + the shared `_request` attach-verify
helper.

Two layers, matching the WS05 split:

  * the reusable `_request.request_reviewers` helper, unit-tested with an
    INJECTED/FAKED boundary (no network, no real `gh`): it confirms a remote
    request attached; it reports `dropped` (→ not-ok) when GitHub silently drops
    the edge; a bare run skips reviewers already DONE; `force=True` requests one
    regardless of state.
  * the `pr review request` CLI verb: the local-agent guard surfaces as a clean
    error (non-zero), and a smoke test proves the subgroup + command are wired.

The engine itself (adapter detection, the state machine) is NOT re-tested here.
"""

from __future__ import annotations

import pytest

from shipit import cli
from shipit.prstate import ghapi
from shipit.prstate.model import ReviewLifecycle
from shipit.prstate.reviewers import ReviewerAdapter
from shipit.verbs.pr import _request, review as review_verb
from shipit.verbs.pr._request import (
    ReviewerOutcome,
    _Boundary,
    request_reviewers,
)


# --- test doubles -------------------------------------------------------------


class _FakeAdapter(ReviewerAdapter):
    """A controllable adapter: declares its edge model + lifecycle, records the
    request call, and reports placement via `request_returns`."""

    def __init__(
        self,
        name: str,
        *,
        has_edge: bool = True,
        request_returns: bool = True,
        lifecycle: ReviewLifecycle = ReviewLifecycle.NOT_REQUESTED,
    ) -> None:
        self.name = name
        self.has_requested_edge = has_edge
        self._request_returns = request_returns
        self._lifecycle = lifecycle
        self.requested_with: list[int] = []

    def matches(self, login: str) -> bool:
        return self.name in login.lower()

    def detect(self, ctx) -> ReviewLifecycle:  # noqa: ANN001
        return self._lifecycle

    def request(self, pr: int) -> bool:
        self.requested_with.append(pr)
        return self._request_returns


def _boundary(
    *,
    requested_logins: list[str] | None = None,
    reviews: list[tuple[int, str]] | None = None,
) -> _Boundary:
    """A faked boundary: `attach_state` returns the given pending logins + review
    tail; `gather_reviews` returns a sentinel ctx (adapters' fake `detect` ignores
    it); `sleep` is a no-op so the poll runs instantly."""
    logins = requested_logins or []
    revs = reviews or []
    return _Boundary(
        attach_state=lambda pr: (logins, revs),
        gather_reviews=lambda pr: object(),
        sleep=lambda _seconds: None,
    )


# --- the attach-verify helper -------------------------------------------------


def test_verifies_when_edge_attaches():
    """A remote request whose login shows up in pending requests verifies."""
    adapter = _FakeAdapter("copilot")
    result = request_reviewers(
        7,
        [adapter],
        force=True,
        boundary=_boundary(requested_logins=["Copilot"]),
    )
    assert adapter.requested_with == [7]
    assert result.ok
    assert result.verified == ["copilot"]
    assert result.dropped == []


def test_verifies_via_fresh_review_when_bot_consumed_request():
    """A fast bot that submits a fresh review before the poll sees the edge still
    verifies (the review id is not in the pre-request baseline)."""
    adapter = _FakeAdapter("copilot")
    # baseline (first attach_state call, pre-place) is empty; the poll then sees
    # a NEW review by copilot — fresh, so verified.
    calls = {"n": 0}

    def attach_state(pr):
        calls["n"] += 1
        if calls["n"] == 1:
            return [], []  # baseline: no reviews yet
        return [], [(99, "Copilot")]  # poll: fresh review consumed the request

    boundary = _Boundary(
        attach_state=attach_state,
        gather_reviews=lambda pr: object(),
        sleep=lambda s: None,
    )
    result = request_reviewers(7, [adapter], force=True, boundary=boundary)
    assert result.ok
    assert result.verified == ["copilot"]


def test_dropped_when_edge_never_appears():
    """A silently-dropped attach (edge never appears, no fresh review) is a hard
    failure: status `dropped`, result not ok."""
    adapter = _FakeAdapter("copilot")
    result = request_reviewers(
        7,
        [adapter],
        force=True,
        boundary=_boundary(requested_logins=[], reviews=[]),
    )
    assert not result.ok
    assert result.dropped == ["copilot"]


def test_bare_run_skips_already_done_reviewer():
    """A bare run drops a reviewer already DONE on the head — never requested."""
    done = _FakeAdapter("copilot", lifecycle=ReviewLifecycle.DONE_CLEAN)
    result = request_reviewers(7, [done], force=False, boundary=_boundary())
    assert done.requested_with == []  # not re-poked
    assert result.skipped == ["copilot"]
    assert result.verified == []


def test_bare_run_requests_pending_reviewer():
    """A bare run DOES request a reviewer not yet done, and verifies it."""
    pending = _FakeAdapter("copilot", lifecycle=ReviewLifecycle.NOT_REQUESTED)
    result = request_reviewers(
        7,
        [pending],
        force=False,
        boundary=_boundary(requested_logins=["Copilot"]),
    )
    assert pending.requested_with == [7]
    assert result.verified == ["copilot"]


def test_force_requests_already_done_reviewer():
    """`force=True` (the --reviewer escape hatch) requests even a DONE reviewer."""
    done = _FakeAdapter("copilot", lifecycle=ReviewLifecycle.DONE_CLEAN)
    result = request_reviewers(
        7,
        [done],
        force=True,
        boundary=_boundary(requested_logins=["Copilot"]),
    )
    assert done.requested_with == [7]  # forced despite being done
    assert result.skipped == []
    assert result.verified == ["copilot"]


def test_local_reviewer_posted_not_edge_verified():
    """A local reviewer (no edge) that returns True is `posted`, never polled."""
    local = _FakeAdapter("codex", has_edge=False, request_returns=True)
    # attach_state would raise if the poll ran — proving locals skip verification.

    def boom(pr):
        raise AssertionError("local reviewer must not be edge-verified")

    boundary = _Boundary(
        attach_state=boom, gather_reviews=lambda pr: object(), sleep=lambda s: None
    )
    result = request_reviewers(7, [local], force=True, boundary=boundary)
    assert result.ok
    assert result.posted == ["codex"]
    assert result.verified == []


def test_no_mechanism_backend_is_no_op():
    """A backend whose request() returns False records a no-op, never verified."""
    auto = _FakeAdapter("gemini", has_edge=False, request_returns=False)
    result = request_reviewers(7, [auto], force=True, boundary=_boundary())
    assert result.ok
    assert result.no_op == ["gemini"]


def test_gh_failure_in_skip_read_propagates():
    """A gh failure while reading who-is-done propagates (never a false success)."""
    adapter = _FakeAdapter("copilot")

    def boom(pr):
        raise ghapi.GhError("gh exploded reading reviews")

    boundary = _Boundary(
        attach_state=lambda pr: ([], []),
        gather_reviews=boom,
        sleep=lambda s: None,
    )
    with pytest.raises(ghapi.GhError):
        request_reviewers(7, [adapter], force=False, boundary=boundary)


# --- the local-agent guard via the CLI verb -----------------------------------


def test_local_agent_request_surfaces_clean_guard(monkeypatch, capsys):
    """Requesting a local-agent reviewer surfaces the foundation's guard as a
    clean GhError -> stderr + non-zero exit, never a crash."""
    monkeypatch.setattr(review_verb, "resolve_pr", lambda pr: 7)
    rc = review_verb.run(7, reviewer="codex")
    assert rc != 0
    err = capsys.readouterr().err
    assert "not yet available" in err
    assert "codex" in err


@pytest.mark.parametrize("name", ["codex-local", "agy-local"])
def test_local_agent_spec_alias_surfaces_guard(monkeypatch, capsys, name):
    """The PRD/glossary spell these `codex-local`/`agy-local`; the `-local` alias
    resolves the base adapter so the guard surfaces, not an unknown-name error."""
    monkeypatch.setattr(review_verb, "resolve_pr", lambda pr: 7)
    rc = review_verb.run(7, reviewer=name)
    assert rc != 0
    err = capsys.readouterr().err
    assert "not yet available" in err
    assert "unknown reviewer" not in err


def test_local_alias_does_not_match_app_reviewer(capsys):
    """`-local` aliases only the local-agent family — `copilot-local` is unknown
    (an app reviewer has a requested edge and is not a local backend)."""
    rc = review_verb.run(7, reviewer="copilot-local")
    assert rc != 0
    assert "unknown reviewer" in capsys.readouterr().err


# --- CLI verb wiring + behavior ----------------------------------------------


def test_unknown_reviewer_is_rejected(monkeypatch, capsys):
    """A typo'd --reviewer name fails loud (non-zero) listing the known names."""
    rc = review_verb.run(7, reviewer="copliot")
    assert rc != 0
    err = capsys.readouterr().err
    assert "unknown reviewer" in err
    assert "copilot" in err  # the known-names list


def test_no_pr_for_branch_is_fatal(monkeypatch, capsys):
    """A mutating verb treats a branch with no PR as fatal (non-zero)."""
    monkeypatch.setattr(review_verb, "resolve_pr", lambda pr: None)
    rc = review_verb.run(None, reviewer="copilot")
    assert rc != 0
    assert "no PR" in capsys.readouterr().err


def test_gh_failure_resolving_is_fatal(monkeypatch, capsys):
    """A real gh/auth failure resolving the branch's PR -> clean stderr + non-zero."""

    def boom(pr):
        raise ghapi.GhError("gh auth exploded")

    monkeypatch.setattr(review_verb, "resolve_pr", boom)
    rc = review_verb.run(None, reviewer="copilot")
    assert rc != 0
    assert "gh auth exploded" in capsys.readouterr().err


def test_verb_renders_verified(monkeypatch, capsys):
    """The bare-run happy path: resolve -> request_reviewers -> render verified."""
    monkeypatch.setattr(review_verb, "resolve_pr", lambda pr: 7)
    monkeypatch.setattr(
        review_verb,
        "request_reviewers",
        lambda pr, adapters, force: _request.RequestResult(
            outcomes=[ReviewerOutcome("copilot", "verified")]
        ),
    )
    monkeypatch.setattr(review_verb, "required_reviewers", lambda: [object()])
    rc = review_verb.run(7)
    assert rc == 0
    assert "verified: copilot" in capsys.readouterr().out


def test_dropped_request_exits_nonzero(monkeypatch, capsys):
    """A dropped remote request -> stderr line + non-zero exit (never a silent park)."""
    monkeypatch.setattr(review_verb, "resolve_pr", lambda pr: 7)
    monkeypatch.setattr(
        review_verb,
        "request_reviewers",
        lambda pr, adapters, force: _request.RequestResult(
            outcomes=[ReviewerOutcome("copilot", "dropped")]
        ),
    )
    monkeypatch.setattr(review_verb, "required_reviewers", lambda: [object()])
    rc = review_verb.run(7)
    assert rc != 0
    assert "dropped by GitHub" in capsys.readouterr().err


# --- group/command registration smoke ----------------------------------------


def test_pr_review_subgroup_registered(capsys):
    rc = cli.main(["pr", "--help"])
    assert rc == 0
    assert "review" in capsys.readouterr().out


def test_pr_review_lists_request(capsys):
    rc = cli.main(["pr", "review", "--help"])
    assert rc == 0
    assert "request" in capsys.readouterr().out


def test_pr_review_request_help(capsys):
    rc = cli.main(["pr", "review", "request", "--help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "--reviewer" in out
