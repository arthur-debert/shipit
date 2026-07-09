"""Unit tests for the reviewer-request service (`shipit.prstate.request`).

The attach-verify helper at its domain home (CLI01-WS03 promoted it out of
``verbs/pr/``), tested prstate-style with an INJECTED/FAKED boundary (no
network, no real `gh`, no click): it confirms a remote request attached; it
reports `dropped` (→ not-ok) when GitHub silently drops the edge; a bare run
skips reviewers already DONE; `force=True` requests one regardless of state.
Each outcome's durable log twin (LOG02 / ADR-0029) is pinned here too — the
records live with the service now, not the verb.

The engine itself (adapter detection, the state machine) is NOT re-tested here.
"""

from __future__ import annotations

import logging

import pytest

from shipit import events
from shipit.execrun import ExecError
from shipit.identity import repo_from_slug
from shipit.pr import PrId
from shipit.prstate.model import ReviewLifecycle
from shipit.prstate.request import (
    Boundary,
    RequestResult,
    ReviewerOutcome,
    request_reviewers,
)
from shipit.prstate.reviewers import ReviewerAdapter
from shipit.prstate.roster import Roster

# The typed PR target (CLI01-WS02 / ADR-0030): the service threads a PrId —
# repo + number as ONE value — never a bare int.
REPO = repo_from_slug("owner/repo")
TARGET = PrId(repo=REPO, number=7)
EMPTY_ROSTER = Roster()


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
        self.requested_with: list[PrId] = []

    def matches(self, login: str) -> bool:
        return self.name in login.lower()

    def detect(self, ctx) -> ReviewLifecycle:  # noqa: ANN001
        return self._lifecycle

    def request(self, pr: PrId, entry=None, policy=None) -> bool:
        self.requested_with.append(pr)
        return self._request_returns


def _boundary(
    *,
    requested_logins: list[str] | None = None,
    reviews: list[tuple[int, str]] | None = None,
) -> Boundary:
    """A faked boundary: `attach_state` returns the given pending logins + review
    tail; `gather_reviews` returns a sentinel ctx (adapters' fake `detect` ignores
    it); `sleep` is a no-op so the poll runs instantly."""
    logins = requested_logins or []
    revs = reviews or []
    return Boundary(
        attach_state=lambda pr: (logins, revs),
        gather_reviews=lambda pr, roster: object(),
        sleep=lambda _seconds: None,
    )


# --- the attach-verify service -------------------------------------------------


def test_verifies_when_edge_attaches():
    """A remote request whose login shows up in pending requests verifies."""
    adapter = _FakeAdapter("copilot")
    result = request_reviewers(
        TARGET,
        [adapter],
        EMPTY_ROSTER,
        force=True,
        boundary=_boundary(requested_logins=["Copilot"]),
    )
    # The adapter received the TYPED target — repo riding on the identity.
    assert adapter.requested_with == [TARGET]
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

    boundary = Boundary(
        attach_state=attach_state,
        gather_reviews=lambda pr, roster: object(),
        sleep=lambda s: None,
    )
    result = request_reviewers(
        TARGET, [adapter], EMPTY_ROSTER, force=True, boundary=boundary
    )
    assert result.ok
    assert result.verified == ["copilot"]


def test_dropped_when_edge_never_appears():
    """A silently-dropped attach (edge never appears, no fresh review) is a hard
    failure: status `dropped`, result not ok."""
    adapter = _FakeAdapter("copilot")
    result = request_reviewers(
        TARGET,
        [adapter],
        EMPTY_ROSTER,
        force=True,
        boundary=_boundary(requested_logins=[], reviews=[]),
    )
    assert not result.ok
    assert result.dropped == ["copilot"]


def test_bare_run_skips_already_done_reviewer():
    """A bare run drops a reviewer already DONE on the head — never requested."""
    done = _FakeAdapter("copilot", lifecycle=ReviewLifecycle.DONE_CLEAN)
    result = request_reviewers(
        TARGET, [done], EMPTY_ROSTER, force=False, boundary=_boundary()
    )
    assert done.requested_with == []  # not re-poked
    assert result.skipped == ["copilot"]
    assert result.verified == []


def test_bare_run_requests_pending_reviewer():
    """A bare run DOES request a reviewer not yet done, and verifies it."""
    pending = _FakeAdapter("copilot", lifecycle=ReviewLifecycle.NOT_REQUESTED)
    result = request_reviewers(
        TARGET,
        [pending],
        EMPTY_ROSTER,
        force=False,
        boundary=_boundary(requested_logins=["Copilot"]),
    )
    assert pending.requested_with == [TARGET]
    assert result.verified == ["copilot"]


def test_force_requests_already_done_reviewer():
    """`force=True` (the --reviewer escape hatch) requests even a DONE reviewer."""
    done = _FakeAdapter("copilot", lifecycle=ReviewLifecycle.DONE_CLEAN)
    result = request_reviewers(
        TARGET,
        [done],
        EMPTY_ROSTER,
        force=True,
        boundary=_boundary(requested_logins=["Copilot"]),
    )
    assert done.requested_with == [TARGET]  # forced despite being done
    assert result.skipped == []
    assert result.verified == ["copilot"]


def test_local_reviewer_in_flight_not_edge_verified():
    """A local reviewer (no edge) that returns True is `in_flight`, never polled."""
    local = _FakeAdapter("codex", has_edge=False, request_returns=True)
    # attach_state would raise if the poll ran — proving locals skip verification.

    def boom(pr):
        raise AssertionError("local reviewer must not be edge-verified")

    boundary = Boundary(
        attach_state=boom,
        gather_reviews=lambda pr, roster: object(),
        sleep=lambda s: None,
    )
    result = request_reviewers(
        TARGET, [local], EMPTY_ROSTER, force=True, boundary=boundary
    )
    assert result.ok
    assert result.in_flight == ["codex"]
    assert result.verified == []


def test_no_mechanism_backend_is_no_op():
    """A backend whose request() returns False records a no-op, never verified."""
    auto = _FakeAdapter("gemini", has_edge=False, request_returns=False)
    result = request_reviewers(
        TARGET, [auto], EMPTY_ROSTER, force=True, boundary=_boundary()
    )
    assert result.ok
    assert result.no_op == ["gemini"]


def test_local_request_failure_propagates_and_records_no_in_flight(caplog):
    """#347: a local adapter whose placement RAISES (`PrStateError` — e.g. the
    detach died on an auth/env precondition) propagates out of the service: no
    result is returned to claim the reviewer, and no `in flight` durable record
    is left for the failed reviewer — a failed placement can never surface as a
    placed one."""
    from shipit.prstate.errors import PrStateError

    class _BoomLocal(_FakeAdapter):
        def request(self, pr: PrId, entry=None, policy=None) -> bool:
            raise PrStateError("codex-local review failed on #7: auth unavailable")

    adapter = _BoomLocal("codex", has_edge=False)
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        with pytest.raises(PrStateError, match="codex-local"):
            request_reviewers(
                TARGET, [adapter], EMPTY_ROSTER, force=True, boundary=_boundary()
            )
    assert not [r for r in caplog.records if "in flight" in r.getMessage()]


def test_gh_failure_in_skip_read_propagates():
    """A gh failure while reading who-is-done propagates (never a false success)."""
    adapter = _FakeAdapter("copilot")

    def boom(pr, roster):
        raise ExecError(["gh"], rc=1, stderr="gh exploded reading reviews")

    boundary = Boundary(
        attach_state=lambda pr: ([], []),
        gather_reviews=boom,
        sleep=lambda s: None,
    )
    with pytest.raises(ExecError):
        request_reviewers(
            TARGET, [adapter], EMPTY_ROSTER, force=False, boundary=boundary
        )


# --- the RequestResult verdict surface -----------------------------------------


def test_result_groups_outcomes_by_status():
    result = RequestResult(
        outcomes=[
            ReviewerOutcome("copilot", "verified"),
            ReviewerOutcome("codex", "in_flight"),
            ReviewerOutcome("gemini", "no_op"),
            ReviewerOutcome("coderabbit", "skipped"),
            ReviewerOutcome("sourcery", "dropped"),
        ]
    )
    assert result.verified == ["copilot"]
    assert result.in_flight == ["codex"]
    assert result.no_op == ["gemini"]
    assert result.skipped == ["coderabbit"]
    assert result.dropped == ["sourcery"]
    assert not result.ok


# --- the durable log twins (LOG02 / ADR-0029) ----------------------------------
#
# Each outcome records at the SERVICE that produced it, on the engine's logger,
# carrying flat ``pr``/``reviewer`` keys: INFO for a placed/in-flight request,
# DEBUG for a deliberate non-act, WARNING for a dropped request. The convention
# is pinned (levels + keys), not the prose.


def _prstate_records(caplog, level: int):
    return [
        r
        for r in caplog.records
        if r.name == "shipit.prstate" and r.levelno == level and hasattr(r, "reviewer")
    ]


def test_verified_and_in_flight_outcomes_are_info_records(caplog):
    remote = _FakeAdapter("copilot")
    local = _FakeAdapter("codex", has_edge=False)
    with caplog.at_level(logging.DEBUG, logger="shipit.prstate"):
        request_reviewers(
            TARGET,
            [remote, local],
            EMPTY_ROSTER,
            force=True,
            boundary=_boundary(requested_logins=["Copilot"]),
        )
    infos = _prstate_records(caplog, logging.INFO)
    assert {r.reviewer for r in infos} == {"copilot", "codex"}
    assert all(r.pr == 7 for r in infos)


def test_skip_and_no_op_outcomes_are_debug_mechanics(caplog):
    done = _FakeAdapter("copilot", lifecycle=ReviewLifecycle.DONE_CLEAN)
    auto = _FakeAdapter("gemini", has_edge=False, request_returns=False)
    with caplog.at_level(logging.DEBUG, logger="shipit.prstate"):
        request_reviewers(
            TARGET, [done, auto], EMPTY_ROSTER, force=False, boundary=_boundary()
        )
    assert not _prstate_records(caplog, logging.INFO)  # nothing transitioned
    mechanics = _prstate_records(caplog, logging.DEBUG)
    assert {r.reviewer for r in mechanics} == {"copilot", "gemini"}
    assert all(r.pr == 7 for r in mechanics)


def test_dropped_outcome_is_a_warning_record(caplog):
    adapter = _FakeAdapter("copilot")
    with caplog.at_level(logging.DEBUG, logger="shipit.prstate"):
        result = request_reviewers(
            TARGET, [adapter], EMPTY_ROSTER, force=True, boundary=_boundary()
        )
    assert not result.ok
    warnings = _prstate_records(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert warnings[0].reviewer == "copilot"
    assert warnings[0].pr == 7


# --- the review.requested dev-cycle event (LOG04-WS01 / ADR-0032) ---------------
#
# The placed-request milestones ARE the event: one `event="review.requested"`
# record per reviewer whose request took effect (remote edge verified, or the
# local review detached in-flight). A dropped request and the deliberate
# non-acts stay untagged — the milestone trail records only requests that
# actually happened. On the raw LogRecord the tag rides under
# `events.EXTRA_KEY` (the render seam lands it as the durable `event` field —
# pinned end-to-end by test_events).


def _event_tag(record) -> str | None:
    return getattr(record, events.EXTRA_KEY, None)


def test_placed_requests_emit_the_review_requested_event(caplog):
    """One review.requested record per reviewer requested — remote (verified)
    and local (in-flight) alike, carrying the flat pr/reviewer keys."""
    remote = _FakeAdapter("copilot")
    local = _FakeAdapter("codex", has_edge=False)
    with caplog.at_level(logging.DEBUG, logger="shipit.prstate"):
        request_reviewers(
            TARGET,
            [remote, local],
            EMPTY_ROSTER,
            force=True,
            boundary=_boundary(requested_logins=["Copilot"]),
        )
    tagged = [r for r in caplog.records if _event_tag(r)]
    assert {_event_tag(r) for r in tagged} == {"review.requested"}
    assert {r.reviewer for r in tagged} == {"copilot", "codex"}
    assert all(r.pr == 7 and r.levelno == logging.INFO for r in tagged)


def test_non_requests_carry_no_event_tag(caplog):
    """A dropped attach, a review-once skip, and a no-mechanism no-op never
    tag a review.requested event — no request took effect."""
    dropped = _FakeAdapter("copilot")
    with caplog.at_level(logging.DEBUG, logger="shipit.prstate"):
        request_reviewers(
            TARGET, [dropped], EMPTY_ROSTER, force=True, boundary=_boundary()
        )
    done = _FakeAdapter("coderabbit", lifecycle=ReviewLifecycle.DONE_CLEAN)
    auto = _FakeAdapter("gemini", has_edge=False, request_returns=False)
    with caplog.at_level(logging.DEBUG, logger="shipit.prstate"):
        request_reviewers(
            TARGET, [done, auto], EMPTY_ROSTER, force=False, boundary=_boundary()
        )
    assert not [r for r in caplog.records if _event_tag(r)]
