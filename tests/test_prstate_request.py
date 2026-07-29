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
from shipit.prstate.roster import Roster, RosterEntry
from shipit.review.calibrator import CalibratorConfig

REPO = repo_from_slug("owner/repo")
TARGET = PrId(repo=REPO, number=7)
EMPTY_ROSTER = Roster()


class _FakeAdapter(ReviewerAdapter):
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
        self.request_calls: list[tuple] = []

    def matches(self, login: str) -> bool:
        return self.name in login.lower()

    def detect(self, ctx) -> ReviewLifecycle:  # noqa: ANN001
        return self._lifecycle

    def request(self, pr: PrId, entry=None, policy=None) -> bool:
        self.requested_with.append(pr)
        self.request_calls.append((pr, entry, policy))
        return self._request_returns


def _boundary(
    *,
    requested_logins: list[str] | None = None,
    reviews: list[tuple[int, str]] | None = None,
) -> Boundary:
    logins = requested_logins or []
    revs = reviews or []
    return Boundary(
        attach_state=lambda pr: (logins, revs),
        gather_reviews=lambda pr, roster: object(),
        sleep=lambda _seconds: None,
    )


def test_verifies_when_edge_attaches():
    adapter = _FakeAdapter("copilot")
    result = request_reviewers(
        TARGET,
        [adapter],
        EMPTY_ROSTER,
        force=True,
        boundary=_boundary(requested_logins=["Copilot"]),
    )
    assert adapter.requested_with == [TARGET]
    assert result.ok
    assert result.verified == ["copilot"]
    assert result.dropped == []


def test_request_threads_the_reviewer_entry_and_table_policy():
    adapter = _FakeAdapter("copilot")
    roster = Roster(
        entries=(
            RosterEntry(name="copilot", dimensions=("correctness", "test-quality")),
        ),
        nit_cap=2,
        calibrator=CalibratorConfig(backend="claude"),
    )
    request_reviewers(
        TARGET,
        [adapter],
        roster,
        force=True,
        boundary=_boundary(requested_logins=["Copilot"]),
    )
    [(pr, entry, policy)] = adapter.request_calls
    assert pr == TARGET
    assert entry == roster.entry("copilot")
    assert policy == roster.policy


def test_verifies_via_fresh_review_when_bot_consumed_request():
    adapter = _FakeAdapter("copilot")
    calls = {"n": 0}

    def attach_state(pr):
        calls["n"] += 1
        if calls["n"] == 1:
            return [], []
        return [], [(99, "Copilot")]

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
    done = _FakeAdapter("copilot", lifecycle=ReviewLifecycle.DONE_CLEAN)
    result = request_reviewers(
        TARGET, [done], EMPTY_ROSTER, force=False, boundary=_boundary()
    )
    assert done.requested_with == []
    assert result.skipped == ["copilot"]
    assert result.verified == []


def test_bare_run_requests_pending_reviewer():
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
    done = _FakeAdapter("copilot", lifecycle=ReviewLifecycle.DONE_CLEAN)
    result = request_reviewers(
        TARGET,
        [done],
        EMPTY_ROSTER,
        force=True,
        boundary=_boundary(requested_logins=["Copilot"]),
    )
    assert done.requested_with == [TARGET]
    assert result.skipped == []
    assert result.verified == ["copilot"]


def test_local_reviewer_in_flight_not_edge_verified():
    local = _FakeAdapter("codex", has_edge=False, request_returns=True)

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
    auto = _FakeAdapter("gemini", has_edge=False, request_returns=False)
    result = request_reviewers(
        TARGET, [auto], EMPTY_ROSTER, force=True, boundary=_boundary()
    )
    assert result.ok
    assert result.no_op == ["gemini"]


def test_local_request_failure_propagates_and_records_no_in_flight(caplog):
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
    assert not _prstate_records(caplog, logging.INFO)
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


def _event_tag(record) -> str | None:
    return getattr(record, events.EXTRA_KEY, None)


def test_placed_requests_emit_the_review_requested_event(caplog):
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
