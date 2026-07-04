"""Review-round stopping rule — stop the loop instead of iterating forever.

The rule is deliberately simple and mechanical (it REPLACES the older
divergent-cycle model — there is no diff-trajectory / comment-set /
repeat-finding / divergent-counting machinery any more):

    Each round, address every review comment, EXCEPT stop when either
      • the round cap has been reached (default 6 — there is no 7th round;
        repo policy can override it via `round_cap` in the `[reviewers]` table
        of `.shipit.toml`, carried on `Roster.round_cap`), or
      • the current round's RECORDED verdicts are all nitpick (#423): the agent
        that addressed the round classified every finding, and every verdict is
        `nitpick` (docstring/wording fixes, cosmetic style already settled —
        nothing that changes correctness or behaviour).

There is NO auto-classification of any kind — no marker list, no body regex,
no model call (the old ``_NITPICK_MARKERS`` machinery is DELETED, #423): any
auto-classifier just chases reviewer phrasing forever. The agent addressing
the round has already judged each finding's weight by deciding fix-vs-reply;
`shipit pr classify` records that judgment into the dev-cycle event log
(write-once, keyed by finding comment id — :mod:`.verdicts`), the snapshot
carries it (``ReadinessView.verdicts``), and this rule CONSUMES it. A round
with any unclassified finding is not all-nitpick — and the state machine's
CLASSIFY gate refuses to advance past an unclassified round anyway.

A *round* is one ITERATION — one head SHA that got re-reviewed — NOT one review
object. That distinction is load-bearing once there are several required
reviewers (release#622): N reviewers each reviewing one head would otherwise
read as N rounds. A round is keyed by commit SHA, so every required reviewer's
findings on the same head fold into ONE round. Its findings are the review-thread
comments attached to those reviews (GraphQL `reviewThreads`, the single source
of truth for inline comments; release#515).

When the rule fires on an otherwise-ready PR (CI green, merge state CLEAN), the
state machine routes to READY and hands it to the human — it does NOT open
another round. An all-nitpick stop additionally suppresses every RE-REQUEST:
the nit-fix push stales no one back into the loop (not even a `rerun: true`
reviewer) — the loop terminates by simply not asking again. When the PR is not
otherwise ready (failing CI, conflict), the real reason BLOCKS it; the stopping
rule never invents a block of its own.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..identity import Sha
from .model import ReadinessView, ReviewComment
from .reviewers import ReviewerAdapter, required_adapters
from .verdicts import NITPICK

# The SHIPPED default round cap: the 6th round is the last; there is no 7th.
# Only the default — repo policy overrides it via `Roster.round_cap` (the
# `round_cap` key in `.shipit.toml` `[reviewers]`), threaded onto the
# `ReadinessView` at the verb boundary; no config is read in this module.
ROUND_CAP = 6


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
    body + location): the comment id is the verdict key (#423) and the body the
    human-facing excerpt, so every consumer — the breaker, the classify verb,
    the gate — reads the same finding identity. Login matching is the adapter's
    job — never re-roll an author filter here (a reviewer's review login and
    comment author can render differently; release#455).

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


def unclassified_findings(
    rnd: Round, verdicts: Mapping[int, str]
) -> tuple[ReviewComment, ...]:
    """The round's findings with NO recorded verdict, in round order.

    The CLASSIFY gate's structured input (#423): while this is non-empty for
    the LATEST round, the state machine reports CLASSIFY and refuses to
    advance (no RE-REQUEST, no READY) — and the pre-push tripwire blocks the
    push with the same message. An id is unclassified iff absent from
    ``verdicts``; nothing here inspects a body.
    """
    return tuple(f for f in rnd.findings if f.comment_id not in verdicts)


def is_all_nitpick_round(rnd: Round, verdicts: Mapping[int, str]) -> bool:
    """True iff the round has findings and EVERY finding's RECORDED verdict is
    ``nitpick`` (#423).

    Consumes verdicts only — the agent's recorded fix-vs-reply judgment, keyed
    by finding comment id — never the comment body: there is no marker list and
    no fallback. A finding with no verdict is NOT a nitpick (the round is
    simply not all-nitpick yet; the CLASSIFY gate keeps the loop from advancing
    past it). An empty round (a clean/approving pass, no findings) is not "all
    nitpicks" — there is nothing to address, so the normal readiness checks
    handle it.
    """
    return bool(rnd.findings) and all(
        verdicts.get(f.comment_id) == NITPICK for f in rnd.findings
    )


def evaluate_breakers(
    ctx: ReadinessView,
    required: list[ReviewerAdapter] | None = None,
) -> BreakerVerdict:
    """Apply the stopping rule. First condition to hit wins.

    `required` is threaded through to `build_rounds` so round counting uses the
    SAME required set the engine evaluates (release#622).

    Stop when the round cap has been reached, or when the latest round's
    recorded verdicts (``ctx.verdicts``, the dev-cycle log read folded onto the
    snapshot at the gather seam) are all nitpick. The cap is the snapshot
    Roster's `round_cap` (`ctx.roster`, the ONE boundary-loaded config value —
    the same value `build_rounds` defaults its required set from), falling back
    to the shipped :data:`ROUND_CAP` when unset. Either way the reported
    `cycles` is the raw round count (what the human sees). The state machine
    decides what a stop means for routing (READY when otherwise ready, else the
    real blocker — and for all-nitpick, no re-request at all).
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

    if rounds and is_all_nitpick_round(rounds[-1], ctx.verdicts):
        return BreakerVerdict(
            True,
            "all-nitpick",
            "every finding of the latest review round is classified nitpick "
            "(nothing that changes correctness or behaviour) — stop rather "
            "than open another round",
            n,
        )

    return BreakerVerdict(False, None, "", n)
