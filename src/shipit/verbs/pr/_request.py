"""Request (or re-request) reviewers and VERIFY the request attached — the
reusable, boundary-injected helper shared across `pr` verbs.

This is the extraction of release's `#614` attach-verify logic (release's
`cli/review.py`) into a composable function with NO click/CLI concerns: it takes
a PR number and a list of reviewer adapters, places each request, and polls the
PR's pending review-requests until every placed request's `review_requested`
edge actually exists — failing loud (via the returned result) when GitHub
silently drops an attach, so a dropped request never parks the PR invisibly at
reviews-pending.

Why a separate helper (not buried in the verb): WS05's `pr review request` AND
WS06's `pr next` both need to "request the pending required reviewers and make
sure it stuck". Keeping the request+verify here — pure orchestration over an
injected boundary — lets both call it and lets it be unit-tested without the
network or click.

The split that makes #614 correct (carried over verbatim from release):

  * REMOTE reviewers (`has_requested_edge == True` — Copilot, CodeRabbit) place
    a real GitHub `review_requested` edge whose attach can be silently dropped.
    They are edge-VERIFIED: after placing, poll `attach_state` until the
    reviewer appears in the pending requests OR has submitted a FRESH review (a
    fast bot can consume the request before the poll sees the edge). A baseline
    of the newest review ids is taken BEFORE placing so a review that lands
    between placement and the poll still reads as fresh.
  * LOCAL reviewers (`has_requested_edge == False` — codex, agy) DETACH an async
    review inside `request()` (OBS03); a True return means the review is now
    IN-FLIGHT — a detached child is running it and the funnel check run is the
    result store (a failure in the synchronous detach raises). There is no edge to
    poll, so they are NEVER edge-verified and NEVER reported dropped.
  * No-mechanism backends (`request()` returns False — auto-triggering Gemini)
    are a recorded no-op and never verified.

The boundary (`attach_state`, `gather_reviews`, `sleep`) is injected so a test
fakes GitHub deterministically; the default wires the real engine functions.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from ...prstate import fetch as _fetch
from ...prstate.model import ReviewLifecycle
from ...prstate.reviewers import ReviewerAdapter
from ...prstate.roster import Roster

# Attach-verification poll (release#614). The request edge is normally created
# synchronously — the first check usually verifies; the later checks absorb
# propagation lag without burning minutes on an outage a retry won't fix.
ATTACH_VERIFY_CHECKS = 4
ATTACH_VERIFY_INTERVAL_SECONDS = 12  # checks at t=0/12/24/36s — ~36s worst case

# The lifecycles that count as "already reviewed on this head" for the bare-run
# skip: both mean the reviewer is DONE, so re-requesting would re-poke a
# finished reviewer (and cost a token / model run).
_DONE_LIFECYCLES = {ReviewLifecycle.DONE_CLEAN, ReviewLifecycle.DONE_COMMENTS}


@dataclass
class ReviewerOutcome:
    """What happened for one reviewer in a `request_reviewers` run."""

    name: str
    # One of: "verified" (remote edge attached), "in_flight" (local review
    # detached, running async), "no_op" (no request mechanism), "skipped"
    # (already done, bare run), "dropped" (remote edge never attached — a hard
    # failure).
    status: str


@dataclass
class RequestResult:
    """The outcome of a `request_reviewers` call — what to render + the verdict.

    `ok` is the overall success: True unless at least one remote request was
    silently dropped. A caller maps `ok=False` to a non-zero exit; the dropped
    reviewers are named in `outcomes` (status == "dropped") so the caller can
    print a precise error.
    """

    outcomes: list[ReviewerOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(o.status == "dropped" for o in self.outcomes)

    @property
    def dropped(self) -> list[str]:
        return [o.name for o in self.outcomes if o.status == "dropped"]

    def _by_status(self, status: str) -> list[str]:
        return [o.name for o in self.outcomes if o.status == status]

    @property
    def verified(self) -> list[str]:
        return self._by_status("verified")

    @property
    def in_flight(self) -> list[str]:
        return self._by_status("in_flight")

    @property
    def no_op(self) -> list[str]:
        return self._by_status("no_op")

    @property
    def skipped(self) -> list[str]:
        return self._by_status("skipped")


@dataclass
class _Boundary:
    """The injected GitHub read side. Defaults wire the real engine functions;
    a test swaps in fakes so the poll runs without the network."""

    attach_state: Callable[[int], tuple[list[str], list[tuple[int, str]]]] = (
        _fetch.attach_state
    )
    gather_reviews: Callable[[int, Roster], object] = _fetch.gather_reviews
    sleep: Callable[[float], None] = time.sleep


def request_reviewers(
    pr: int,
    adapters: Sequence[ReviewerAdapter],
    roster: Roster,
    *,
    force: bool = False,
    boundary: _Boundary | None = None,
    checks: int = ATTACH_VERIFY_CHECKS,
    interval_seconds: float = ATTACH_VERIFY_INTERVAL_SECONDS,
) -> RequestResult:
    """Request `adapters` on `pr`, then verify each remote edge actually attached.

    `roster` is the reviewer configuration as ONE value (CLI01-WS04), loaded
    once at the calling verb's boundary: the skip decision reads the rerun flag
    off it (via the light snapshot it is threaded onto), and each adapter's
    `request` receives ITS entry so a local reviewer's run options (`model` /
    `instructions` / `timeout`) arrive as values — settings are never
    re-resolved from config inside this path.

    `force=False` (the bare/default scope): reviewers already DONE on the current
    head are SKIPPED (review-once — don't re-poke a finished reviewer); a
    never-reviewed or push-staled (rerun=True) reviewer is kept and requested.
    `force=True` (the `--reviewer NAME` manual escape hatch): request every given
    adapter regardless of state.

    Remote reviewers (real `review_requested` edge) are edge-verified by polling
    `boundary.attach_state`; a dropped attach lands as a `"dropped"` outcome and
    flips `result.ok` False. Local reviewers DETACH an async review (recorded
    `"in_flight"`); no-mechanism backends record `"no_op"`. Neither is verified.

    Raises `execrun.ExecError` straight through when a `gh` call fails (the skip read,
    a `request()` placement, or the attach poll) — the caller renders it as a
    clean stderr + non-zero exit, exactly as the read verbs do. This helper never
    swallows a boundary failure into a false success.
    """
    bound = boundary or _Boundary()
    result = RequestResult()

    targets = list(adapters)
    if not force:
        targets = _drop_already_done(pr, targets, roster, result, bound)
        if not targets:
            return result

    # Baseline the newest review ids BEFORE placing any request: a review that
    # lands between placement and the poll is then "fresh" and still verifies a
    # fast bot that consumed the request before the edge was observable. Only
    # needed when a remote (edge-placing) adapter is in play.
    baseline_ids: set[int] = set()
    if any(a.has_requested_edge for a in targets):
        _, baseline_reviews = bound.attach_state(pr)
        baseline_ids = {rid for rid, _ in baseline_reviews}

    remote_placed: list[ReviewerAdapter] = []
    for adapter in targets:
        if adapter.request(pr, roster.entry(adapter.name)):
            if adapter.has_requested_edge:
                remote_placed.append(adapter)
            else:
                # Local reviewer: request() detached an async review (OBS03) — it
                # is now in-flight; there is no edge to poll.
                result.outcomes.append(ReviewerOutcome(adapter.name, "in_flight"))
        else:
            # No request mechanism (auto-triggering backend) — a no-op.
            result.outcomes.append(ReviewerOutcome(adapter.name, "no_op"))

    dropped = _verify_attached(
        pr,
        remote_placed,
        baseline_ids=baseline_ids,
        boundary=bound,
        checks=checks,
        interval_seconds=interval_seconds,
    )
    for adapter in remote_placed:
        status = "dropped" if adapter in dropped else "verified"
        result.outcomes.append(ReviewerOutcome(adapter.name, status))
    return result


def _drop_already_done(
    pr: int,
    adapters: list[ReviewerAdapter],
    roster: Roster,
    result: RequestResult,
    boundary: _Boundary,
) -> list[ReviewerAdapter]:
    """Return the adapters NOT already DONE on `pr`, recording each skip.

    Builds ONE light context (`gather_reviews` — head SHA + reviews + requested
    logins + the Roster, no thread/reaction pagination) and runs each adapter's
    rerun-aware `detect`: a review-once reviewer that has reviewed reads DONE and
    is dropped; a never-reviewed or push-staled reviewer is kept. A `gh` failure
    here propagates (we can't tell who is done — requesting blind would re-poke
    finished reviewers)."""
    ctx = boundary.gather_reviews(pr, roster)
    keep: list[ReviewerAdapter] = []
    for adapter in adapters:
        if adapter.detect(ctx) in _DONE_LIFECYCLES:
            result.outcomes.append(ReviewerOutcome(adapter.name, "skipped"))
        else:
            keep.append(adapter)
    return keep


def _verify_attached(
    pr: int,
    placed: list[ReviewerAdapter],
    *,
    baseline_ids: set[int],
    boundary: _Boundary,
    checks: int,
    interval_seconds: float,
) -> list[ReviewerAdapter]:
    """The placed adapters whose request edge never appeared (empty = all good).

    An adapter is verified when its reviewer shows up in the PR's pending review
    requests, OR when a FRESH review by it (one not in `baseline_ids`) has been
    submitted — a fast bot can consume the request before the poll sees the edge.
    Only adapters that placed a real edge enter here.
    """
    pending = list(placed)
    for check in range(checks):
        if not pending:
            break
        if check:
            boundary.sleep(interval_seconds)
        requested_logins, reviews = boundary.attach_state(pr)
        fresh_authors = [author for rid, author in reviews if rid not in baseline_ids]
        pending = [
            a
            for a in pending
            if not any(a.matches(login) for login in requested_logins)
            and not any(a.matches(author) for author in fresh_authors)
        ]
    return pending
