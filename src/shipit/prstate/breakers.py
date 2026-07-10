"""Review-round stopping rule — stop the loop instead of iterating forever.

The rule is deliberately simple and mechanical (it REPLACES the older
divergent-cycle model — there is no diff-trajectory / comment-set /
repeat-finding / divergent-counting machinery any more):

    Each round, address every review comment, EXCEPT stop when either
      • the round cap has been reached (default 6 — there is no 7th round;
        repo policy can override it via `round_cap` in the `[reviewers]` table
        of `.shipit.toml`, carried on `Roster.round_cap`), or
      • the current round has findings but NONE major-or-worse (ADR-0044 /
        RVW02): every finding's resolved Severity fails the merge-block test —
        minor/nit only, nothing a competent reviewer would hold the merge for.

There is NO classification step anywhere: findings arrive PRE-classified on
the 4-tier Severity ladder, and this rule reads each finding's severity
through the precedence chain (:mod:`.severity` — machine marker →
reviewer-adapter mapping → ``major`` fail-safe, beaten only by a write-once
Severity override off the snapshot's ``ReadinessView.overrides``). The
``major`` default is what makes the rule fail-safe: an unparseable finding
forces another round rather than slipping the Breaker.

A *round* is one ITERATION — one head SHA that got re-reviewed — NOT one review
object. That distinction is load-bearing once there are several required
reviewers (release#622): N reviewers each reviewing one head would otherwise
read as N rounds. A round is keyed by commit SHA, so every required reviewer's
findings on the same head fold into ONE round. Its findings are the review-thread
comments attached to those reviews (GraphQL `reviewThreads`, the single source
of truth for inline comments; release#515).

When the rule fires, the loop is over but the round's leftover minor/nit
threads still require RESOLUTION before Ready (fix-or-reply + resolve — the
state machine holds ADDRESSING while any thread is open); they just never mint
another round: a fired breaker suppresses every RE-REQUEST, so the fix push
stales no one back into the loop (not even a `rerun: true` reviewer) — the
loop terminates by simply not asking again. When the PR is not otherwise ready
(failing CI, conflict), the real reason BLOCKS it; the stopping rule never
invents a block of its own.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..finding import Severity
from ..identity import Sha
from .model import ReadinessView, ReviewComment
from .reviewers import ReviewerAdapter, required_adapters
from .severity import finding_severity

# The SHIPPED default round cap: the 6th round is the last; there is no 7th.
# Only the default — repo policy overrides it via `Roster.round_cap` (the
# `round_cap` key in `.shipit.toml` `[reviewers]`), threaded onto the
# `ReadinessView` at the verb boundary; no config is read in this module.
ROUND_CAP = 6

#: The no-major+ stop's breaker name (ADR-0044): the latest round's findings
#: are all minor/nit — nothing passes the merge-block test — so no further round.
NO_MAJOR_FINDING = "no-major-finding"


@dataclass(frozen=True)
class Round:
    index: int  # 1-based, chronological
    commit_id: Sha | None  # the round's head; None when the wire carried no commit
    findings: tuple[ReviewComment, ...]  # the round's finding comments


@dataclass(frozen=True)
class BreakerVerdict:
    stop: bool
    breaker: str | None
    reason: str
    cycles: int  # raw round count (what the human sees)


def build_rounds(
    ctx: ReadinessView,
    required: list[ReviewerAdapter] | None = None,
) -> list[Round]:
    """One Round per HEAD SHA reviewed by a required reviewer, chronological.

    Rounds are iterations, not review objects: all required reviewers' reviews
    on the same head fold into a single round, so N required reviewers don't
    multiply the round count (release#622). A round's findings are the UNION of
    those reviews' thread comments (resolved or not — a resolved finding was
    still a finding of that round), keyed by the review each comment was
    submitted with (`ReviewComment.review_id`). The comments ride WHOLE (id +
    body + location): the body carries the machine marker / native severity
    format the precedence chain reads, and the comment id is the
    Severity-override key — so every consumer (the breaker, the classify verb)
    reads the same finding identity. Login matching is the adapter's job —
    never re-roll an author filter here (a reviewer's review login and comment
    author can render differently; release#455).

    `required` is the blocking set; defaults to the snapshot Roster's required
    adapters (`ctx.roster`, CLI01-WS04) but the engine threads its own set in so
    the stopping rule counts against the SAME reviewers everything else blocks on.
    """
    required = required if required is not None else required_adapters(ctx.roster)
    reviews = sorted(
        (r for r in ctx.reviews if any(a.matches(r.author) for a in required)),
        key=lambda r: r.review_id,
    )
    thread_comments = [c for t in ctx.threads for c in t.comments]

    # Group by head SHA, preserving first-seen (chronological) order. Each head
    # is one round; its findings union every required review on that head.
    review_ids_by_head: dict[Sha | None, list[int]] = {}
    for review in reviews:
        review_ids_by_head.setdefault(review.commit_id, []).append(review.review_id)

    rounds: list[Round] = []
    for index, (commit_id, review_ids) in enumerate(
        review_ids_by_head.items(), start=1
    ):
        id_set = set(review_ids)
        findings = tuple(c for c in thread_comments if c.review_id in id_set)
        rounds.append(Round(index, commit_id, findings))
    return rounds


def has_blocking_finding(rnd: Round, overrides: Mapping[int, Severity]) -> bool:
    """True iff any of the round's findings resolves major-or-worse (ADR-0044).

    Each finding's Severity comes from the precedence chain
    (:func:`~shipit.prstate.severity.finding_severity`): its machine marker,
    else its author's adapter mapping, else the ``major`` fail-safe — beaten
    only by a write-once override in ``overrides``. ``blocks_merge`` is the
    merge-block test: major-or-worse means a competent reviewer would hold the
    merge, and only such a finding keeps the review loop minting rounds.
    """
    return any(finding_severity(f, overrides).blocks_merge for f in rnd.findings)


def evaluate_breakers(
    ctx: ReadinessView,
    required: list[ReviewerAdapter] | None = None,
) -> BreakerVerdict:
    """Apply the stopping rule. First condition to hit wins.

    `required` is threaded through to `build_rounds` so round counting uses the
    SAME required set the engine evaluates (release#622).

    Stop when the round cap has been reached, or when the latest round has
    findings but none major-or-worse (each finding's severity resolved through
    the precedence chain against the snapshot's ``ctx.overrides`` — the
    dev-cycle log read folded on at the gather seam). An EMPTY latest round (a
    clean/approving pass, no findings) does not fire the no-major+ stop: there
    is nothing to address, so the normal readiness checks handle it. The cap is
    the snapshot Roster's `round_cap` (`ctx.roster`, the ONE boundary-loaded
    config value — the same value `build_rounds` defaults its required set
    from), falling back to the shipped :data:`ROUND_CAP` when unset. Either way
    the reported `cycles` is the raw round count (what the human sees). The
    state machine decides what a stop means for routing (leftover threads still
    resolve before READY, and a fired breaker suppresses every re-request).
    """
    rounds = build_rounds(ctx, required=required)
    n = len(rounds)
    cap = ctx.roster.round_cap if ctx.roster.round_cap is not None else ROUND_CAP

    if n >= cap:
        return BreakerVerdict(
            True,
            "round-cap",
            f"{n} review rounds reached the cap of {cap} — there is no further round",
            n,
        )

    if (
        rounds
        and rounds[-1].findings
        and not has_blocking_finding(rounds[-1], ctx.overrides)
    ):
        return BreakerVerdict(
            True,
            NO_MAJOR_FINDING,
            "no finding of the latest review round is major-or-worse (nothing "
            "a competent reviewer would hold the merge for) — stop rather "
            "than open another round",
            n,
        )

    return BreakerVerdict(False, None, "", n)
