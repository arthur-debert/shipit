"""Unit tests for the next-action dispatcher (`shipit.prstate.dispatch`).

The dispatcher at its domain home (CLI01-WS03 promoted it out of
``verbs/pr/``). Per `TaskState`, assert the PURE decision routes to the
EXPECTED act on the injected `Acts` boundary. No network, no engine re-test:
each test hands a hand-built `TaskStatus` (the engine's output shape) to
`dispatch` with a recording fake and asserts which act fired. This is the deep
module's test seam. The concrete :class:`NextActs` boundary (reviewer
SELECTION over the engine's `to_request`, delegation to the canonical request
service and the shared guarded flip) is unit-tested below with the services
monkeypatched at the dispatch module — prstate style, no click.
"""

from __future__ import annotations

import logging

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
    """A fake `Acts` that records which method fired (no GitHub)."""

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
    """A REVIEWS_PENDING with a required reviewer to request (NEVER_REQUESTED, so
    the engine listed it in `to_request`) → request, decided from structure."""
    acts = RecordingActs()
    status = _status(TaskState.REVIEWS_PENDING, to_request=["copilot"])
    dispatch(status, acts)
    assert acts.called == "request_review"


def test_reviews_pending_rerequest_also_requests():
    """A stale-after-push reviewer (re-request) rides the SAME `to_request` set and
    routes to the single request act — request and re-request are one act."""
    acts = RecordingActs()
    status = _status(TaskState.REVIEWS_PENDING, to_request=["copilot"])
    dispatch(status, acts)
    assert acts.called == "request_review"


def test_reviews_pending_only_waiting_reports():
    """When every holding reviewer is in-flight-within-window (`to_request` empty)
    → report, not re-poke (PRD user story 5/6: don't re-request a reviewer already
    mid-review). WS03 guarantees a past-window reviewer is already settled
    (TIMED_OUT), so an empty `to_request` here means strictly in-flight."""
    acts = RecordingActs()
    status = _status(TaskState.REVIEWS_PENDING, to_request=[])
    dispatch(status, acts)
    assert acts.called == "report"


def test_reviews_pending_routing_ignores_next_action_wording():
    """The #24.1 regression: routing is decided from `to_request`, NEVER the
    `next_action` prose. The SAME structured state with WILDLY different wording —
    including prose that names a "request" verb — routes identically; and an empty
    `to_request` reports even when the prose screams "request"."""
    # Same (empty) to_request, three different next_action strings → all report.
    for prose in (
        "waiting on required review(s): copilot — wait (already requested ...)",
        "request for the current head: copilot",  # misleading prose, empty signal
        "any arbitrary wording at all",
    ):
        acts = RecordingActs()
        dispatch(_status(TaskState.REVIEWS_PENDING, prose, to_request=[]), acts)
        assert acts.called == "report"
    # Same (non-empty) to_request, prose that says "wait" → still request.
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
    """A degraded set (required reviewers settled non-success) does NOT block the
    flip: a READY PR with degraded reviewers still routes to flip_ready (ADR-0006).
    The engine already let it reach READY; the dispatcher hands it off."""
    acts = RecordingActs()
    status = _status(
        TaskState.READY,
        "run `pr ready`",
        degraded={"codex-local": "failed"},
    )
    dispatch(status, acts)
    assert acts.called == "flip_ready"


def test_dispatch_returns_the_acts_line():
    """The dispatcher returns the act's line verbatim (what `pr next` prints)."""
    acts = RecordingActs()
    line = dispatch(_status(TaskState.READY), acts)
    assert line == "flipped"


# --- end-to-end: real engine FunnelState → to_request → routed act ----------
#
# The unit tests above hand `dispatch` a hand-built `TaskStatus`. These prove the
# WHOLE chain: a recorded snapshot → `evaluate` folds each reviewer's signals to a
# FunnelState and settles `to_request`/`degraded` → `dispatch` routes off that
# structure to the expected act. No prose anywhere on the path.

# Required set for the local-reviewer cases: an App reviewer (copilot, posted in
# the base fixture) + a local-agent reviewer (codex) whose breadcrumb we vary.
_REQUIRED = [by_name("copilot"), by_name("codex")]


def _codex_breadcrumb(status, conclusion):
    # started_at 5m before the base fixture's now (00:30) → WITHIN the 20m window,
    # so an IN_PROGRESS breadcrumb reads IN_FLIGHT (holds), not aged-out TIMED_OUT.
    return ReviewFunnelCheck(
        reviewer="codex-local",
        status=status,
        conclusion=conclusion,
        started_at="2026-01-01T00:25:00Z",
    )


def test_e2e_never_requested_routes_to_request():
    """A never-requested required reviewer → engine settles `to_request` non-empty
    → dispatch requests (the default copilot-only required set, copilot absent)."""
    status = evaluate(load_context("copilot_never_requested"))
    assert status.state is TaskState.REVIEWS_PENDING
    assert status.to_request  # the engine flagged a (re-)request
    acts = RecordingActs()
    dispatch(status, acts)
    assert acts.called == "request_review"


def test_e2e_failing_checks_route_to_report_not_request():
    """Failing checks outrank review requests (#352): the SAME never-requested
    snapshot with a red rollup → engine suppresses `to_request` and ranks the CI
    block → dispatch reports the fix-CI instruction, never `request_review` — no
    token-billed review is burned on a head that is about to change."""
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


def test_e2e_stale_after_push_routes_to_request():
    """A rerun=True reviewer with a review staled by a push → re-request: same
    `to_request` set, same request act (request and re-request are one act)."""
    ctx = load_context("copilot_stale_needs_rerequest")
    ctx.roster = Roster((RosterEntry(name="copilot", required=True, rerun=True),))
    status = evaluate(ctx)
    assert status.state is TaskState.REVIEWS_PENDING
    assert status.to_request == ["copilot"]
    assert "RE-REQUEST" in status.next_action  # prose says re-request…
    acts = RecordingActs()
    dispatch(status, acts)
    assert acts.called == "request_review"  # …and routing agrees, off structure


def test_e2e_in_flight_within_window_routes_to_wait():
    """An in-flight-within-window required reviewer → engine leaves `to_request`
    empty (it is the wait case) → dispatch reports, never re-pokes."""
    ctx = load_context("local_reviewer_otherwise_ready")
    ctx.review_funnel = [_codex_breadcrumb("IN_PROGRESS", None)]
    status = evaluate(ctx, required=_REQUIRED)
    assert status.state is TaskState.REVIEWS_PENDING
    assert status.to_request == []  # in-flight → wait, nothing to (re-)request
    acts = RecordingActs()
    dispatch(status, acts)
    assert acts.called == "report"


def test_e2e_degraded_but_ready_routes_to_flip():
    """A required reviewer settled non-success (degraded) on an otherwise-ready PR
    → engine reaches READY with a degraded set → dispatch flips (degraded does not
    block the hand-off)."""
    ctx = load_context("local_reviewer_otherwise_ready")
    ctx.review_funnel = [_codex_breadcrumb("COMPLETED", "FAILURE")]
    status = evaluate(ctx, required=_REQUIRED)
    assert status.state is TaskState.READY
    assert status.degraded == {"codex-local": "failed"}
    acts = RecordingActs()
    dispatch(status, acts)
    assert acts.called == "flip_ready"


# --- NextActs: the concrete boundary (selection + delegation) ------------------
#
# Moved here with the dispatcher (CLI01-WS03): reviewer SELECTION consumes the
# engine's structured `to_request`; EXECUTION is delegated to the canonical
# request service / the shared guarded flip, monkeypatched at this module.

REPO = repo_from_slug("owner/repo")
TARGET = PrId(repo=REPO, number=42)


class FakeAdapter:
    def __init__(self, name):
        self.name = name

    def matches(self, login):
        return self.name in login.lower()


def _fake_request_result(names):
    """A RequestResult whose `verified` are the given names — `ok` is True."""
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
    """The act consumes `to_request`, maps names to adapters, and hands the
    canonical request service the TYPED target with force=True (selection is
    already done — the service must request exactly these)."""
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
    # The act hands the request service the TYPED target — the same PrId the
    # resolver minted (repo + number), never a bare int.
    assert seen["pr"] == TARGET
    assert (seen["names"], seen["force"]) == (["copilot"], True)


def test_request_act_selects_only_the_not_requested_reviewer(monkeypatch):
    """A MIXED REVIEWS_PENDING (one not_requested, one already requested) must
    SELECT only the not_requested reviewer for the request service — never
    re-poke a reviewer already mid-review (Copilot review on PR #19)."""
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
    # Selection excluded the mid-review reviewer — only copilot reached the service.
    assert selected["names"] == ["copilot"]
    assert "coderabbit" not in line


def test_request_act_excludes_in_flight_local_agent(monkeypatch):
    """OBS04 convergence regression: a required LOCAL-agent reviewer genuinely
    IN_FLIGHT (its `review: codex-local` check run still running) reads lifecycle
    `not_requested` — a local agent has no native `review_requested` edge — so a
    lifecycle-based selection would re-poke it mid-review. The act consumes the
    engine's `to_request`, which EXCLUDES the in-flight reviewer."""
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
            # codex's detached run is in-flight, but its lifecycle reads
            # not_requested (no requested edge) — a lifecycle read would pick it.
            reviewers={"copilot": "not_requested", "codex": "not_requested"},
        )
    )
    # Only the never-requested reviewer reached the service — codex NOT re-poked.
    assert selected["names"] == ["copilot"]
    assert "codex" not in line


def test_request_act_selects_never_requested_and_stale(monkeypatch):
    """The act selects EVERY name the engine placed in `to_request` — both a
    never-requested reviewer and a stale-after-push (RE-REQUEST) one. The
    engine's `to_request` is the authority, so both reach the service."""
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


def test_request_act_dropped_edge_raises_prstate_error(monkeypatch):
    """A silently-dropped request edge (#614) → the domain refusal, naming the
    reviewer — the caller's error shell renders it as stderr + non-zero."""
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
    """`to_request` names with no matching required adapter → a report line,
    never a crash (and the request service is never called)."""
    monkeypatch.setattr(dispatch_mod, "required_adapters", lambda roster: [])

    def boom(pr, adapters, roster, *, force):
        raise AssertionError("request service must not be called")

    monkeypatch.setattr(dispatch_mod, "request_reviewers", boom)
    line = NextActs(TARGET).request_review(_pending(["ghost"]))
    assert line.startswith("no requestable reviewer")


def test_flip_act_goes_through_the_shared_guard(monkeypatch):
    """The ready act flips through the SAME guarded re-check `pr ready` uses —
    the typed target travels into the guard."""
    flipped: list[PrId] = []

    def fake_guard(target, roster, **kw):
        flipped.append(target)
        return _status(TaskState.READY, "human validates + merges")

    monkeypatch.setattr(dispatch_mod, "guarded_flip", fake_guard)
    line = NextActs(TARGET).flip_ready(_status(TaskState.READY))
    assert flipped == [TARGET]
    assert line == "flipped draft→ready — human validates + merges"


def test_report_act_surfaces_the_engines_next_action():
    line = NextActs(TARGET).report(_status(TaskState.BLOCKED, "the real blocker"))
    assert line == "no action taken — the real blocker"


# --- the durable log twin (LOG02 / ADR-0029) -----------------------------------


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
