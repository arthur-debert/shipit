"""The PR lifecycle state machine: a `ReadinessView` snapshot to one
`TaskStatus`. See docs/adr/0006-readiness-with-degraded-reviewers.md."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from .. import events
from .breakers import build_rounds, evaluate_breakers
from .model import FunnelState, ReadinessView, ReviewLifecycle
from .reviewers import REGISTRY, ReviewerAdapter, required_adapters

logger = logging.getLogger("shipit.prstate")

# A required reviewer HOLDS the PR only while never-requested or still
# pending; DEGRADED is the non-blocking subset of settled.
_HOLDS = {
    FunnelState.NEVER_REQUESTED,
    FunnelState.REQUESTED,
    FunnelState.IN_FLIGHT,
}
_DEGRADED = {FunnelState.FAILED, FunnelState.EMPTY, FunnelState.TIMED_OUT}

# CANCELLED is deliberately not a failure: a killed run never judged.
_FAIL_CONCLUSIONS = {
    "FAILURE",
    "TIMED_OUT",
    "ACTION_REQUIRED",
    "STARTUP_FAILURE",
}
# Only CheckRuns carry this; StatusContext has no cancelled state.
_CANCELLED_CONCLUSIONS = {"CANCELLED"}
_FAIL_STATES = {"FAILURE", "ERROR"}
_PENDING_STATUSES = {
    "QUEUED",
    "IN_PROGRESS",
    "PENDING",
    "WAITING",
    "REQUESTED",
    "EXPECTED",
}


class TaskState(StrEnum):
    NO_PR = "no_pr"
    REVIEWS_PENDING = "reviews_pending"
    ADDRESSING = "addressing"
    REVIEWED = "reviewed"
    VALIDATING = "validating"
    READY = "ready"
    BLOCKED = "blocked"


class ChecksState(StrEnum):
    NONE = "none"  # no checks configured
    GREEN = "green"
    PENDING = "pending"
    FAILING = "failing"
    # A killed run with nothing running: the cure is a rerun, not a fix.
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class ReviewerFunnel:
    """A reviewer's lifecycle paired with its funnel check-run breadcrumb."""

    lifecycle: ReviewLifecycle
    state: FunnelState = FunnelState.NEVER_REQUESTED
    check_status: str | None = None
    check_conclusion: str | None = None
    check_started_at: str | None = None


@dataclass
class TaskStatus:
    """The snapshot result: lifecycle position + the one next action."""

    state: TaskState
    next_action: str
    pr: int | None = None
    reviewers: dict[str, str] = field(default_factory=dict)
    open_threads: int = 0
    checks: ChecksState = ChecksState.NONE
    mergeable: str | None = None
    cycles: int = 0
    breaker: str | None = None
    reviewer_funnel: dict[str, ReviewerFunnel] = field(default_factory=dict)
    # Settled-non-success required reviewers, by display name → reason.
    degraded: dict[str, str] = field(default_factory=dict)
    # Holding required reviewers needing a (re-)request now.
    to_request: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "pr": self.pr,
            "state": self.state.value,
            "next_action": self.next_action,
            "reviewers": self.reviewers,
            "open_threads": self.open_threads,
            "checks": self.checks.value,
            "mergeable": self.mergeable,
            "cycles": self.cycles,
            "breaker": self.breaker,
            "to_request": self.to_request,
            "reviewer_funnel": {
                name: {
                    "lifecycle": rf.lifecycle.value,
                    "state": rf.state.value,
                    "check_status": rf.check_status,
                    "check_conclusion": rf.check_conclusion,
                    "check_started_at": rf.check_started_at,
                }
                for name, rf in self.reviewer_funnel.items()
            },
            "degraded": self.degraded,
        }


def no_pr() -> TaskStatus:
    return TaskStatus(
        state=TaskState.NO_PR,
        next_action="no PR for this branch — create a draft PR to start the review loop",
    )


def evaluate(
    ctx: ReadinessView,
    registry: list[ReviewerAdapter] | None = None,
    required: list[ReviewerAdapter] | None = None,
) -> TaskStatus:
    """Compute the PR's lifecycle state from a snapshot, logging the decision."""
    status = _evaluate(ctx, registry, required)
    fields: dict[str, object] = {
        "pr": status.pr,
        "state": status.state.value,
        "checks": status.checks.value,
        "open_threads": status.open_threads,
        "cycles": status.cycles,
        "funnel": ",".join(
            f"{name}={rf.state.value}" for name, rf in status.reviewer_funnel.items()
        ),
    }
    if status.mergeable is not None:
        fields["mergeable"] = status.mergeable
    if status.breaker is not None:
        fields["breaker"] = status.breaker
    if status.to_request:
        fields["to_request"] = ",".join(status.to_request)
    logger.info(
        "decision pr#%s: state=%s checks=%s mergeable=%s open_threads=%s "
        "cycles=%s breaker=%s -> next_action=%r",
        status.pr,
        status.state.value,
        status.checks.value,
        status.mergeable,
        status.open_threads,
        status.cycles,
        status.breaker,
        status.next_action,
        extra=fields,
    )
    if status.degraded:
        logger.warning(
            "pr#%s: degraded reviewer(s) — settled non-success, not holding Ready: %s",
            status.pr,
            ", ".join(f"{name}={why}" for name, why in status.degraded.items()),
            extra={
                "pr": status.pr,
                "degraded": ",".join(
                    f"{name}={why}" for name, why in status.degraded.items()
                ),
            },
        )
    if ctx.emit_events:
        _emit_snapshot_events(ctx, status, required)
    return status


def _emit_snapshot_events(
    ctx: ReadinessView,
    status: TaskStatus,
    required: list[ReviewerAdapter] | None,
) -> None:
    slug = ctx.pr.repo.slug
    sightings = ctx.sightings
    required = required if required is not None else required_adapters(ctx.roster)
    for rnd in build_rounds(ctx, required=required):
        head = str(rnd.commit_id) if rnd.commit_id is not None else None
        events.emit_once(
            sightings,
            logger,
            "round.detected",
            (slug, status.pr, head),
            "review round %d detected on pr#%s (%d finding(s))",
            rnd.index,
            status.pr,
            len(rnd.findings),
            extra={
                "pr": status.pr,
                "round": rnd.index,
                "findings": len(rnd.findings),
                **({"commit": head} if head else {}),
            },
        )
    if status.breaker is not None:
        events.emit_once(
            sightings,
            logger,
            "breaker.fired",
            (slug, status.pr, status.breaker),
            "review-loop breaker %s fired on pr#%s after %d round(s)",
            status.breaker,
            status.pr,
            status.cycles,
            extra={"pr": status.pr, "breaker": status.breaker, "cycles": status.cycles},
        )
    for name, why in status.degraded.items():
        events.emit_once(
            sightings,
            logger,
            "review.degraded",
            (slug, status.pr, name, why),
            "reviewer %s settled degraded on pr#%s (%s)",
            name,
            status.pr,
            why,
            extra={"pr": status.pr, "reviewer": name, "reason": why},
        )


def _evaluate(
    ctx: ReadinessView,
    registry: list[ReviewerAdapter] | None = None,
    required: list[ReviewerAdapter] | None = None,
) -> TaskStatus:
    """The pure engine: `required` defaults to the snapshot Roster's blocking set."""
    registry = registry if registry is not None else REGISTRY
    required = required if required is not None else required_adapters(ctx.roster)
    # The union, so a required reviewer outside `registry` is still evaluated.
    to_detect = {r.name: r for r in (*registry, *required)}.values()
    lifecycles = {r.name: r.detect(ctx) for r in to_detect}
    reviewers = {name: lc.value for name, lc in lifecycles.items()}
    funnel_states = {r.name: r.funnel_state(ctx, lifecycles[r.name]) for r in to_detect}
    reviewer_funnel = {}
    for r in to_detect:
        fc = r.funnel_check(ctx)
        reviewer_funnel[r.name] = ReviewerFunnel(
            lifecycle=lifecycles[r.name],
            state=funnel_states[r.name],
            check_status=fc.status if fc else None,
            check_conclusion=fc.conclusion if fc else None,
            check_started_at=fc.started_at if fc else None,
        )
    open_threads = len(ctx.open_threads())
    checks = classify_checks(ctx.checks)
    breaker = evaluate_breakers(ctx, required=required)
    breaker_stops = breaker.stop

    degraded = {
        r.display_name: funnel_states[r.name].value
        for r in required
        if funnel_states[r.name] in _DEGRADED
    }

    status = TaskStatus(
        state=TaskState.REVIEWS_PENDING,  # provisional; set below
        next_action="",
        pr=ctx.number,
        reviewers=reviewers,
        open_threads=open_threads,
        checks=checks,
        mergeable=ctx.mergeable,
        cycles=breaker.cycles,
        reviewer_funnel=reviewer_funnel,
        degraded=degraded,
    )

    # 1. Every required reviewer must be settled; best-effort ones never hold.
    holding = [r for r in required if funnel_states[r.name] in _HOLDS]
    # A fired breaker must not let the fix push re-open the loop.
    if breaker_stops:
        holding = [r for r in holding if not _has_stale_review(ctx, r)]
    if holding:
        request_names, rerequest_names, waiting_names = _classify_pending(
            ctx, holding, funnel_states
        )
        # Failing checks outrank review requests: a CI fix always pushes a
        # new head. Pending checks deliberately do not defer.
        if checks == ChecksState.FAILING:
            status.state = TaskState.BLOCKED
            status.next_action = _checks_failing_deferral_action(
                request_names + rerequest_names, waiting_names
            )
            return status
        # A dead run also outranks: `pr next` performs one act, so spending
        # it on requests would hide the rerun advice for the whole round.
        if checks == ChecksState.CANCELLED:
            status.state = TaskState.BLOCKED
            status.next_action = _checks_cancelled_deferral_action(
                request_names + rerequest_names, waiting_names
            )
            return status
        status.state = TaskState.REVIEWS_PENDING
        status.to_request = request_names + rerequest_names
        status.next_action = _reviews_pending_action(
            holding, request_names, rerequest_names, waiting_names
        )
        return status

    # 2. Reviews in; open threads are addressed before Ready either way.
    if breaker_stops:
        status.breaker = breaker.breaker
    if open_threads:
        status.state = TaskState.ADDRESSING
        if breaker_stops:
            status.next_action = (
                f"review loop stopped ({breaker.breaker}) — resolve the "
                f"{open_threads} remaining open thread(s) in severity order "
                "(fix-or-reply + resolve each); minor/nit findings never mint "
                "another round, and no re-review will be requested"
            )
        else:
            status.next_action = (
                f"triage {open_threads} open thread(s) in severity order "
                "(critical → nit): read them with `gh pr view --comments`, "
                "then fix-or-reply + resolve each"
            )
        return status

    # 3. Reviewed. Now evaluate mergeability + CI.
    #
    # `mergeStateStatus` is the signal the merge obeys; `mergeable` is
    # computed asynchronously and reads stale until the recompute lands, so
    # CONFLICTING is only a fallback for an uncomputed merge state.
    if ctx.merge_state == "DIRTY" or (
        ctx.merge_state in (None, "UNKNOWN") and ctx.mergeable == "CONFLICTING"
    ):
        status.state = TaskState.BLOCKED
        status.next_action = "merge conflict — rebase/resolve against the base branch"
        return status

    # Checked before CI: a moved base re-stales the review and the checks.
    if ctx.merge_state == "BEHIND":
        status.state = TaskState.BLOCKED
        status.next_action = "branch is behind its base — update it (merge/rebase the base) before this can be Ready"
        return status

    if checks == ChecksState.FAILING:
        status.state = TaskState.BLOCKED
        status.next_action = (
            "CI check(s) failing — fix and push before this can be Ready"
        )
        return status

    # BLOCKED, not VALIDATING: a waiter would poll a dead run until timeout.
    if checks == ChecksState.CANCELLED:
        status.state = TaskState.BLOCKED
        status.next_action = _checks_cancelled_action()
        return status

    if checks == ChecksState.PENDING:
        status.state = TaskState.VALIDATING
        status.next_action = "reviews done; CI check(s) running — wait for checks"
        return status

    # GitHub re-runs a skipped check on `ready_for_review`, flipping
    # UNSTABLE for a beat while the rollup still reads green. GREEN is
    # required, not merely "not failing/pending": an absent rollup is no
    # evidence. BLOCKED/HAS_HOOKS can reflect a status the rollup never lists.
    if ctx.merge_state == "UNSTABLE" and checks == ChecksState.GREEN:
        status.state = TaskState.READY
        if ctx.is_draft:
            status.next_action = (
                "reviewed + threads resolved + CI green; merge state UNSTABLE only "
                "from a non-required check re-running on ready_for_review (the rollup "
                "is green) — run `shipit pr ready` to flip draft->ready and page "
                "the human"
            )
        else:
            status.next_action = (
                "reviewed + threads resolved + CI green; merge state UNSTABLE only "
                "from a non-required check re-running on ready_for_review (the rollup "
                "is green), already ready-for-review — done; await the human's verify "
                "+ merge"
            )
        return status

    # CLEAN is the only merge-ready state and the single hand-off point.
    if ctx.merge_state == "CLEAN":
        status.state = TaskState.READY
        if ctx.is_draft:
            status.next_action = (
                "reviewed + threads resolved + CI green + CLEAN merge state — run "
                "`shipit pr ready` to flip draft->ready and page the human"
            )
        else:
            status.next_action = (
                "reviewed + threads resolved + CI green + CLEAN merge state, already "
                "ready-for-review — done; await the human's verify + merge"
            )
        return status

    # Merge state not yet computed (UNKNOWN / null) — GitHub is working; re-poll.
    if ctx.merge_state in (None, "UNKNOWN"):
        status.state = TaskState.REVIEWED
        status.next_action = (
            "reviews done; mergeability not yet determined — re-check shortly"
        )
        return status

    # A computed non-CLEAN state the rollup can't disprove: surface, don't flip.
    status.state = TaskState.BLOCKED
    status.next_action = (
        f"merge blocked by GitHub (mergeStateStatus={ctx.merge_state}) — a status "
        "check or branch-protection rule is unsatisfied; resolve before this can be Ready"
    )
    return status


def reviews_in(status: TaskStatus, required_names: Sequence[str]) -> bool:
    """True iff no required reviewer holds; a name missing from
    `reviewer_funnel` counts as holding."""
    return all(
        name in status.reviewer_funnel
        and status.reviewer_funnel[name].state not in _HOLDS
        for name in required_names
    )


def _classify_pending(
    ctx: ReadinessView,
    pending: list[ReviewerAdapter],
    funnel_states: dict[str, FunnelState],
) -> tuple[list[str], list[str], list[str]]:
    """Split the holding required reviewers into `(request, rerequest, wait)`."""
    request_names: list[str] = []  # no signal → request
    rerequest_names: list[str] = []  # reviewed an earlier head → re-request
    waiting_names: list[str] = []  # already requested/in-flight on head → wait

    for adapter in pending:
        fs = funnel_states[adapter.name]
        if fs in (FunnelState.REQUESTED, FunnelState.IN_FLIGHT):
            waiting_names.append(adapter.name)
        elif _has_stale_review(ctx, adapter):
            rerequest_names.append(adapter.name)
        else:
            request_names.append(adapter.name)
    return request_names, rerequest_names, waiting_names


def _reviews_pending_action(
    pending: list[ReviewerAdapter],
    request_names: list[str],
    rerequest_names: list[str],
    waiting_names: list[str],
) -> str:
    """Render the REVIEWS_PENDING next-action prose; it never drives routing."""
    clauses: list[str] = []
    if request_names:
        clauses.append(f"request for the current head: {', '.join(request_names)}")
    if rerequest_names:
        clauses.append(
            "RE-REQUEST for the current head (a prior review is stale after a push): "
            f"{', '.join(rerequest_names)}"
        )
    if waiting_names:
        clauses.append(
            "wait (already requested / in flight on the current head): "
            f"{', '.join(waiting_names)}"
        )

    all_names = [a.name for a in pending]
    return f"waiting on required review(s): {', '.join(all_names)} — " + "; ".join(
        clauses
    )


def _checks_failing_deferral_action(
    deferred_names: list[str], waiting_names: list[str]
) -> str:
    """Render the fix-CI-first prose, naming the deferred reviewers."""
    action = "CI check(s) failing — fix CI first"
    if deferred_names:
        action += f"; review requests deferred ({', '.join(deferred_names)} pending)"
    if waiting_names:
        action += f"; already requested / in flight: {', '.join(waiting_names)}"
    return action


def _checks_cancelled_action() -> str:
    """Render the rerun prose for a dead-run head, including the "do NOT
    fix-and-push" instruction."""
    return (
        "CI run cancelled/superseded, nothing still running — no code failure "
        "to fix: rerun the workflow on this head (`gh run rerun <run-id> "
        "--failed`; `gh run list` to find the run), do NOT fix-and-push"
    )


def _checks_cancelled_deferral_action(
    deferred_names: list[str], waiting_names: list[str]
) -> str:
    """Render the rerun-first prose, naming the deferred reviewers."""
    action = _checks_cancelled_action()
    if deferred_names:
        action += (
            f"; review requests deferred until the rerun is live "
            f"({', '.join(deferred_names)} pending)"
        )
    if waiting_names:
        action += f"; already requested / in flight: {', '.join(waiting_names)}"
    return action


def _has_stale_review(ctx: ReadinessView, adapter: ReviewerAdapter) -> bool:
    """True iff a push staled this reviewer's review; a commit-less review
    counts as not-on-this-head."""
    if not adapter._rerun(ctx):
        return False
    return any(
        adapter.matches(r.author)
        and r.state != "DISMISSED"
        and (r.commit_id is None or r.commit_id != ctx.head_sha)
        for r in ctx.reviews
    )


def classify_checks(rollup: list[dict]) -> ChecksState:
    """Reduce a `statusCheckRollup`: failing > pending > cancelled > green."""
    if not rollup:
        return ChecksState.NONE
    saw_pending = False
    saw_cancelled = False
    saw_green = False
    for entry in rollup:
        if _is_failing(entry):
            return ChecksState.FAILING
        if _is_pending(entry):
            saw_pending = True
        elif _is_cancelled(entry):
            saw_cancelled = True
        else:
            saw_green = True
    if saw_pending:
        return ChecksState.PENDING
    if saw_cancelled:
        return ChecksState.CANCELLED
    return ChecksState.GREEN if saw_green else ChecksState.NONE


def _is_failing(entry: dict) -> bool:
    if entry.get("conclusion") in _FAIL_CONCLUSIONS:
        return True
    return entry.get("state") in _FAIL_STATES


def _is_cancelled(entry: dict) -> bool:
    # CheckRun only: StatusContext has no cancelled state.
    return entry.get("conclusion") in _CANCELLED_CONCLUSIONS


def _is_pending(entry: dict) -> bool:
    # CheckRun: not COMPLETED is running. StatusContext: a pending `state`.
    status = entry.get("status")
    if status is not None:
        return status != "COMPLETED"
    return entry.get("state") in _PENDING_STATUSES
