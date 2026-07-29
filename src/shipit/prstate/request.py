"""Request (or re-request) reviewers and verify the request attached.

A remote reviewer's `review_requested` edge can be silently dropped by
GitHub, so it is polled until it appears; a local reviewer detaches an
async review instead and has no edge to poll."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .. import events
from ..pr import PrId
from . import fetch as _fetch
from .model import ReviewLifecycle
from .reviewers import ReviewerAdapter
from .roster import Roster

logger = logging.getLogger("shipit.prstate")

# The edge is normally created synchronously; the later checks absorb lag.
ATTACH_VERIFY_CHECKS = 4
ATTACH_VERIFY_INTERVAL_SECONDS = 12  # checks at t=0/12/24/36s — ~36s worst case

# Both mean the reviewer is DONE, so re-requesting would re-poke it.
_DONE_LIFECYCLES = {ReviewLifecycle.DONE_CLEAN, ReviewLifecycle.DONE_COMMENTS}


@dataclass
class ReviewerOutcome:
    name: str
    # "verified" | "in_flight" | "no_op" | "skipped" | "dropped"
    status: str


@dataclass
class RequestResult:
    """The outcome of a `request_reviewers` call; `ok` is False iff a remote
    request was silently dropped."""

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
class Boundary:
    """The injected GitHub read side; a test swaps in fakes."""

    attach_state: Callable[[PrId], tuple[list[str], list[tuple[int, str]]]] = (
        _fetch.attach_state
    )
    gather_reviews: Callable[[PrId, Roster], object] = _fetch.gather_reviews
    sleep: Callable[[float], None] = time.sleep


def _record(result: RequestResult, pr: PrId, name: str, status: str) -> None:
    """Append one outcome and leave its durable log twin; a placed request is
    the ``review.requested`` dev-cycle event, a dropped one a warning."""
    result.outcomes.append(ReviewerOutcome(name, status))
    extra = {"pr": pr.number, "reviewer": name}
    if status == "verified":
        events.emit(
            logger,
            "review.requested",
            "review request from %s attached on pr#%s (verified)",
            name,
            pr.number,
            extra=extra,
        )
    elif status == "in_flight":
        events.emit(
            logger,
            "review.requested",
            "review in flight from %s on pr#%s (detached)",
            name,
            pr.number,
            extra=extra,
        )
    elif status == "dropped":
        logger.warning(
            "review request from %s dropped by GitHub on pr#%s — no "
            "review_requested edge created",
            name,
            pr.number,
            extra=extra,
        )
    elif status == "skipped":
        logger.debug(
            "reviewer %s already reviewed pr#%s (review-once) — skipped",
            name,
            pr.number,
            extra=extra,
        )
    else:  # no_op
        logger.debug(
            "reviewer %s auto-triggers on pr#%s — no request mechanism, no-op",
            name,
            pr.number,
            extra=extra,
        )


def request_reviewers(
    pr: PrId,
    adapters: Sequence[ReviewerAdapter],
    roster: Roster,
    *,
    force: bool = False,
    boundary: Boundary | None = None,
    checks: int = ATTACH_VERIFY_CHECKS,
    interval_seconds: float = ATTACH_VERIFY_INTERVAL_SECONDS,
) -> RequestResult:
    """Request `adapters` on `pr`, then verify each remote edge attached;
    `force=False` skips reviewers already DONE on the current head."""
    bound = boundary or Boundary()
    result = RequestResult()

    targets = list(adapters)
    if not force:
        targets = _drop_already_done(pr, targets, roster, result, bound)
        if not targets:
            return result

    # Baselined before placing, so a review landing mid-poll reads fresh.
    baseline_ids: set[int] = set()
    if any(a.has_requested_edge for a in targets):
        _, baseline_reviews = bound.attach_state(pr)
        baseline_ids = {rid for rid, _ in baseline_reviews}

    remote_placed: list[ReviewerAdapter] = []
    for adapter in targets:
        if adapter.request(pr, roster.entry(adapter.name), policy=roster.policy):
            if adapter.has_requested_edge:
                remote_placed.append(adapter)
            else:
                _record(result, pr, adapter.name, "in_flight")
        else:
            _record(result, pr, adapter.name, "no_op")

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
        _record(result, pr, adapter.name, status)
    return result


def _drop_already_done(
    pr: PrId,
    adapters: list[ReviewerAdapter],
    roster: Roster,
    result: RequestResult,
    boundary: Boundary,
) -> list[ReviewerAdapter]:
    """The adapters not already DONE on `pr`, recording each skip; a `gh`
    failure propagates rather than requesting blind."""
    ctx = boundary.gather_reviews(pr, roster)
    keep: list[ReviewerAdapter] = []
    for adapter in adapters:
        if adapter.detect(ctx) in _DONE_LIFECYCLES:
            _record(result, pr, adapter.name, "skipped")
        else:
            keep.append(adapter)
    return keep


def _verify_attached(
    pr: PrId,
    placed: list[ReviewerAdapter],
    *,
    baseline_ids: set[int],
    boundary: Boundary,
    checks: int,
    interval_seconds: float,
) -> list[ReviewerAdapter]:
    """The placed adapters whose request edge never appeared; a fresh review
    also verifies, since a fast bot can consume the request first."""
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
