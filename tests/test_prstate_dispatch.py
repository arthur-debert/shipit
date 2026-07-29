from __future__ import annotations

import logging
import pathlib

import pytest
from conftest import load_context

from shipit.identity import repo_from_slug
from shipit.pr import PrId
from shipit.prstate import dispatch as dispatch_mod
from shipit.prstate.dispatch import NextActs, dispatch
from shipit.prstate.errors import PrStateError
from shipit.prstate.model import ReviewFunnelCheck
from shipit.prstate.request import RequestResult, ReviewerOutcome
from shipit.prstate.reviewers import by_name
from shipit.prstate.roster import Roster, RosterEntry
from shipit.prstate.state import TaskState, TaskStatus, evaluate


class RecordingActs:
    def __init__(self) -> None:
        self.called: str | None = None

    def report(self, status: TaskStatus) -> str:
        self.called = "report"
        return "reported"

    def request_review(self, status: TaskStatus) -> str:
        self.called = "request_review"
        return "requested"

    def flip_ready(self, status: TaskStatus) -> str:
        self.called = "flip_ready"
        return "flipped"


def _status(
    state: TaskState,
    next_action: str = "x",
    *,
    to_request: list[str] | None = None,
    degraded: dict[str, str] | None = None,
) -> TaskStatus:
    return TaskStatus(
        state=state,
        next_action=next_action,
        pr=42,
        to_request=list(to_request or []),
        degraded=dict(degraded or {}),
    )


@pytest.mark.parametrize(
    "state,expected",
    [
        (TaskState.NO_PR, "report"),
        (TaskState.ADDRESSING, "report"),
        (TaskState.REVIEWED, "report"),
        (TaskState.VALIDATING, "report"),
        (TaskState.BLOCKED, "report"),
        (TaskState.READY, "flip_ready"),
    ],
)
def test_each_state_routes_to_expected_act(state, expected):
    acts = RecordingActs()
    dispatch(_status(state), acts)
    assert acts.called == expected


def test_reviews_pending_with_a_reviewer_to_request_requests():
    acts = RecordingActs()
    status = _status(TaskState.REVIEWS_PENDING, to_request=["copilot"])
    dispatch(status, acts)
    assert acts.called == "request_review"


def test_reviews_pending_rerequest_also_requests():
    acts = RecordingActs()
    status = _status(TaskState.REVIEWS_PENDING, to_request=["copilot"])
    dispatch(status, acts)
    assert acts.called == "request_review"


def test_reviews_pending_only_waiting_reports():
    acts = RecordingActs()
    status = _status(TaskState.REVIEWS_PENDING, to_request=[])
    dispatch(status, acts)
    assert acts.called == "report"


def test_reviews_pending_routing_ignores_next_action_wording():
    for prose in (
        "waiting on required review(s): copilot — wait (already requested ...)",
        "request for the current head: copilot",
        "any arbitrary wording at all",
    ):
        acts = RecordingActs()
        dispatch(_status(TaskState.REVIEWS_PENDING, prose, to_request=[]), acts)
        assert acts.called == "report"
    acts = RecordingActs()
    dispatch(
        _status(
            TaskState.REVIEWS_PENDING,
            "wait (already requested / in flight on the current head): copilot",
            to_request=["copilot"],
        ),
        acts,
    )
    assert acts.called == "request_review"


def test_degraded_but_ready_still_flips():
    acts = RecordingActs()
    status = _status(
        TaskState.READY,
        "run `pr ready`",
        degraded={"codex-local": "failed"},
    )
    dispatch(status, acts)
    assert acts.called == "flip_ready"


def test_dispatch_returns_the_acts_line():
    acts = RecordingActs()
    line = dispatch(_status(TaskState.READY), acts)
    assert line == "flipped"


_REQUIRED = [by_name("copilot"), by_name("codex")]


def _codex_breadcrumb(status, conclusion):
    return ReviewFunnelCheck(
        reviewer="codex-local",
        status=status,
        conclusion=conclusion,
        started_at="2026-01-01T00:25:00Z",
    )


def test_e2e_never_requested_routes_to_request():
    status = evaluate(load_context("copilot_never_requested"))
    assert status.state is TaskState.REVIEWS_PENDING
    assert status.to_request
    acts = RecordingActs()
    dispatch(status, acts)
    assert acts.called == "request_review"


def test_e2e_failing_checks_route_to_report_not_request():
    ctx = load_context("copilot_never_requested")
    ctx.checks = [
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "FAILURE"}
    ]
    status = evaluate(ctx)
    assert status.state is TaskState.BLOCKED
    assert status.to_request == []
    assert "fix CI first" in status.next_action
    acts = RecordingActs()
    dispatch(status, acts)
    assert acts.called == "report"


def test_e2e_cancelled_run_routes_to_report_with_rerun_advice():
    ctx = load_context("copilot_never_requested")
    ctx.checks = [
        {"__typename": "CheckRun", "status": "COMPLETED", "conclusion": "CANCELLED"}
    ]
    status = evaluate(ctx)
    assert status.state is TaskState.BLOCKED
    assert status.to_request == []
    assert "gh run rerun" in status.next_action
    assert "fix and push" not in status.next_action
    acts = RecordingActs()
    dispatch(status, acts)
    assert acts.called == "report"


def test_e2e_stale_after_push_routes_to_request():
    ctx = load_context("copilot_stale_needs_rerequest")
    ctx.roster = Roster((RosterEntry(name="copilot", required=True, rerun=True),))
    status = evaluate(ctx)
    assert status.state is TaskState.REVIEWS_PENDING
    assert status.to_request == ["copilot"]
    assert "RE-REQUEST" in status.next_action
    acts = RecordingActs()
    dispatch(status, acts)
    assert acts.called == "request_review"


def test_e2e_in_flight_within_window_routes_to_wait():
    ctx = load_context("local_reviewer_otherwise_ready")
    ctx.review_funnel = [_codex_breadcrumb("IN_PROGRESS", None)]
    status = evaluate(ctx, required=_REQUIRED)
    assert status.state is TaskState.REVIEWS_PENDING
    assert status.to_request == []
    acts = RecordingActs()
    dispatch(status, acts)
    assert acts.called == "report"


def test_e2e_degraded_but_ready_routes_to_flip():
    ctx = load_context("local_reviewer_otherwise_ready")
    ctx.review_funnel = [_codex_breadcrumb("COMPLETED", "FAILURE")]
    status = evaluate(ctx, required=_REQUIRED)
    assert status.state is TaskState.READY
    assert status.degraded == {"codex-local": "failed"}
    acts = RecordingActs()
    dispatch(status, acts)
    assert acts.called == "flip_ready"


REPO = repo_from_slug("owner/repo")
TARGET = PrId(repo=REPO, number=42)


class FakeAdapter:
    def __init__(self, name, *, has_requested_edge=True):
        self.name = name
        self.has_requested_edge = has_requested_edge

    def matches(self, login):
        return self.name in login.lower()


def _fake_request_result(names):
    return RequestResult(outcomes=[ReviewerOutcome(n, "verified") for n in names])


def _pending(to_request, reviewers=None) -> TaskStatus:
    return TaskStatus(
        state=TaskState.REVIEWS_PENDING,
        next_action="waiting on required review(s)",
        pr=42,
        reviewers=dict(reviewers or {}),
        to_request=list(to_request),
    )


def test_request_act_requests_the_engines_to_request_set(monkeypatch):
    monkeypatch.setattr(
        dispatch_mod, "required_adapters", lambda roster: [FakeAdapter("copilot")]
    )
    seen = {}

    def fake_request(pr, adapters, roster, *, force):
        seen["pr"] = pr
        seen["names"] = [a.name for a in adapters]
        seen["force"] = force
        return _fake_request_result([a.name for a in adapters])

    monkeypatch.setattr(dispatch_mod, "request_reviewers", fake_request)
    line = NextActs(TARGET).request_review(_pending(["copilot"]))
    assert line == "requested review(s): copilot"
    assert seen["pr"] == TARGET
    assert (seen["names"], seen["force"]) == (["copilot"], True)


def test_request_act_selects_only_the_not_requested_reviewer(monkeypatch):
    monkeypatch.setattr(
        dispatch_mod,
        "required_adapters",
        lambda roster: [FakeAdapter("copilot"), FakeAdapter("coderabbit")],
    )
    selected = {}

    def fake_request(pr, adapters, roster, *, force):
        selected["names"] = [a.name for a in adapters]
        return _fake_request_result([a.name for a in adapters])

    monkeypatch.setattr(dispatch_mod, "request_reviewers", fake_request)
    line = NextActs(TARGET).request_review(
        _pending(
            ["copilot"],
            reviewers={"copilot": "not_requested", "coderabbit": "requested"},
        )
    )
    assert selected["names"] == ["copilot"]
    assert "coderabbit" not in line


def test_request_act_excludes_in_flight_local_agent(monkeypatch):
    monkeypatch.setattr(
        dispatch_mod,
        "required_adapters",
        lambda roster: [FakeAdapter("copilot"), FakeAdapter("codex")],
    )
    selected = {}

    def fake_request(pr, adapters, roster, *, force):
        selected["names"] = [a.name for a in adapters]
        return _fake_request_result([a.name for a in adapters])

    monkeypatch.setattr(dispatch_mod, "request_reviewers", fake_request)
    line = NextActs(TARGET).request_review(
        _pending(
            ["copilot"],
            reviewers={"copilot": "not_requested", "codex": "not_requested"},
        )
    )
    assert selected["names"] == ["copilot"]
    assert "codex" not in line


def test_request_act_selects_never_requested_and_stale(monkeypatch):
    monkeypatch.setattr(
        dispatch_mod,
        "required_adapters",
        lambda roster: [FakeAdapter("copilot"), FakeAdapter("coderabbit")],
    )
    selected = {}

    def fake_request(pr, adapters, roster, *, force):
        selected["names"] = [a.name for a in adapters]
        return _fake_request_result([a.name for a in adapters])

    monkeypatch.setattr(dispatch_mod, "request_reviewers", fake_request)
    line = NextActs(TARGET).request_review(_pending(["copilot", "coderabbit"]))
    assert selected["names"] == ["copilot", "coderabbit"]
    assert line == "requested review(s): copilot, coderabbit"


def test_request_act_tries_local_before_remote(monkeypatch):
    adapters = [
        FakeAdapter("copilot"),
        FakeAdapter("codex", has_requested_edge=False),
    ]
    monkeypatch.setattr(dispatch_mod, "required_adapters", lambda roster: adapters)
    seen = {}

    def fail_local_auth(pr, selected, roster, *, force):
        seen["order"] = [adapter.name for adapter in selected]
        raise PrStateError("codex review failed on #42: no App credentials here")

    monkeypatch.setattr(dispatch_mod, "request_reviewers", fail_local_auth)

    with pytest.raises(PrStateError, match="no App credentials here"):
        NextActs(TARGET).request_review(_pending(["copilot", "codex"]))

    assert seen["order"] == ["codex", "copilot"]


def test_request_act_dropped_edge_raises_prstate_error(monkeypatch):
    monkeypatch.setattr(
        dispatch_mod, "required_adapters", lambda roster: [FakeAdapter("copilot")]
    )
    monkeypatch.setattr(
        dispatch_mod,
        "request_reviewers",
        lambda pr, adapters, roster, *, force: RequestResult(
            outcomes=[ReviewerOutcome("copilot", "dropped")]
        ),
    )
    with pytest.raises(PrStateError, match="dropped") as exc:
        NextActs(TARGET).request_review(_pending(["copilot"]))
    assert "copilot" in str(exc.value)


def test_request_act_without_a_requestable_adapter_reports(monkeypatch):
    monkeypatch.setattr(dispatch_mod, "required_adapters", lambda roster: [])

    def boom(pr, adapters, roster, *, force):
        raise AssertionError("request service must not be called")

    monkeypatch.setattr(dispatch_mod, "request_reviewers", boom)
    line = NextActs(TARGET).request_review(_pending(["ghost"]))
    assert line.startswith("no requestable reviewer")


def test_dispatcher_never_reruns_itself_in_a_pixi_env():
    src = pathlib.Path(dispatch_mod.__file__).read_text()
    assert "run_in_env" not in src
    assert "-e review" not in src
    assert not hasattr(dispatch_mod, "rerun_pr_next_in_review_env")
    assert not hasattr(dispatch_mod, "REVIEW_ENV_NAME")


def test_flip_act_goes_through_the_shared_guard(monkeypatch):
    flipped: list[PrId] = []

    def fake_guard(target, roster, **kw):
        flipped.append(target)
        return _status(TaskState.READY, "human validates + merges")

    monkeypatch.setattr(dispatch_mod, "guarded_flip", fake_guard)
    line = NextActs(TARGET).flip_ready(_status(TaskState.READY))
    assert flipped == [TARGET]
    assert line == "flipped draft→ready — ready for human validation"


def test_report_act_surfaces_the_engines_next_action():
    line = NextActs(TARGET).report(_status(TaskState.BLOCKED, "the real blocker"))
    assert line == "no action taken — the real blocker"


def test_dispatch_action_taken_is_an_info_milestone_with_the_pr_key(caplog):
    acts = RecordingActs()
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        dispatch(_status(TaskState.BLOCKED), acts)
    milestones = [
        r
        for r in caplog.records
        if r.name == "shipit.prstate"
        and r.levelno == logging.INFO
        and getattr(r, "pr", None) == 42
    ]
    assert len(milestones) == 1
