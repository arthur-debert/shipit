"""Review-round stopping rule — stop the loop instead of iterating forever.

The rule is deliberately simple and mechanical (it REPLACES the older
divergent-cycle model — there is no diff-trajectory / comment-set /
repeat-finding / divergent-counting machinery any more):

    Each round, address every review comment, EXCEPT stop when either
      • 6 rounds have already happened (there is no 7th round), or
      • the current round is all nitpicks (docstring/wording fixes, micro perf
        with a low run-count, cosmetic style already settled — nothing that
        changes correctness or behaviour).

A *round* is one ITERATION — one head SHA that got re-reviewed — NOT one review
object. That distinction is load-bearing once there are several required
reviewers (release#622): N reviewers each reviewing one head would otherwise
read as N rounds. A round is keyed by commit SHA, so every required reviewer's
findings on the same head fold into ONE round. Its findings are the review-thread
comments attached to those reviews (GraphQL `reviewThreads`, the single source
of truth for inline comments; release#515).

When the rule fires on an otherwise-ready PR (CI green, merge state CLEAN), the
state machine routes to READY and hands it to the human — it does NOT open
another round. When the PR is not otherwise ready (failing CI, conflict), the
real reason BLOCKS it; the stopping rule never invents a block of its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .model import PullContext
from .reviewers import ReviewerAdapter, required_reviewers

ROUND_CAP = 6  # the 6th round is the last; there is no 7th

# Markers that tag a finding as a nitpick — matched case-insensitively against
# the comment body. A round whose findings are ALL nitpicks stops the loop early
# (the agent flips to READY rather than opening another round for cosmetic-only
# feedback). Reviewers (Copilot, CodeRabbit, …) tag low-stakes comments with
# these markers; a plain comment with none of them is treated as substantive, so
# the rule only ever stops EARLY when the round is unambiguously cosmetic.
#
# Each marker is matched on a LEFT word boundary (`\b`) so a short token like
# `nit:` cannot fire on a substring of an unrelated word — e.g. "unit: add a
# test" must NOT read as a nitpick. The right side is left open because several
# markers end in punctuation (`nit:`, `(nit)`, `optional:`) that already
# delimits them.
_NITPICK_MARKERS = (
    "nitpick",
    "nit:",
    "(nit)",
    "minor:",
    "minor nit",
    "super minor",
    "typo",
    "wording",
    "docstring",
    "cosmetic",
    "style suggestion",
    "optional:",
    "(optional)",
)


def _marker_pattern(marker: str) -> str:
    # Anchor a left word boundary only when the marker starts with a word
    # character (so `nit:` won't fire inside "unit:"). Markers that open with
    # punctuation (`(nit)`, `(optional)`) are already delimited by that punctuation.
    escaped = re.escape(marker)
    return (r"\b" + escaped) if marker[:1].isalnum() else escaped


_NITPICK_RE = re.compile(
    "|".join(_marker_pattern(marker) for marker in _NITPICK_MARKERS),
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Round:
    index: int  # 1-based, chronological
    commit_id: str
    bodies: tuple[str, ...]  # the round's finding comment bodies


@dataclass(frozen=True)
class BreakerVerdict:
    stop: bool
    breaker: str | None
    reason: str
    cycles: int  # raw round count (what the human sees)


def build_rounds(
    ctx: PullContext,
    required: list[ReviewerAdapter] | None = None,
) -> list[Round]:
    """One Round per HEAD SHA reviewed by a required reviewer, chronological.

    Rounds are iterations, not review objects: all required reviewers' reviews
    on the same head fold into a single round, so N required reviewers don't
    multiply the round count (release#622). A round's findings are the UNION of
    those reviews' thread comments (resolved or not — a resolved finding was
    still a finding of that round), keyed by the review each comment was
    submitted with (`ReviewComment.review_id`). Login matching is the adapter's
    job — never re-roll an author filter here (a reviewer's review login and
    comment author can render differently; release#455).

    `required` is the gating set; defaults to the config-resolved one but the
    engine threads its own set in so the stopping rule counts against the SAME
    reviewers everything else gates on.
    """
    required = required if required is not None else required_reviewers()
    reviews = sorted(
        (r for r in ctx.reviews if any(a.matches(r.author) for a in required)),
        key=lambda r: r.review_id,
    )
    thread_comments = [c for t in ctx.threads for c in t.comments]

    # Group by head SHA, preserving first-seen (chronological) order. Each head
    # is one round; its findings union every required review on that head.
    review_ids_by_head: dict[str, list[int]] = {}
    for review in reviews:
        review_ids_by_head.setdefault(review.commit_id, []).append(review.review_id)

    rounds: list[Round] = []
    for index, (commit_id, review_ids) in enumerate(
        review_ids_by_head.items(), start=1
    ):
        id_set = set(review_ids)
        bodies = tuple(c.body for c in thread_comments if c.review_id in id_set)
        rounds.append(Round(index, commit_id, bodies))
    return rounds


def _is_nitpick(body: str) -> bool:
    """True iff a comment body carries a nitpick marker (case-insensitive).

    Markers match on a left word boundary, so `nit:` fires on "nit: rename"
    but NOT on "unit: add a test".
    """
    return _NITPICK_RE.search(body) is not None


def is_all_nitpick_round(rnd: Round) -> bool:
    """True iff the round has findings and EVERY finding is a nitpick.

    An empty round (a clean/approving pass, no findings) is not "all nitpicks" —
    there is nothing to address, so the normal readiness gates handle it.
    """
    return bool(rnd.bodies) and all(_is_nitpick(b) for b in rnd.bodies)


def evaluate_breakers(
    ctx: PullContext,
    required: list[ReviewerAdapter] | None = None,
) -> BreakerVerdict:
    """Apply the stopping rule. First condition to hit wins.

    `required` is threaded through to `build_rounds` so round counting uses the
    SAME required set the engine gates on (release#622).

    Stop when 6 rounds have happened, or when the latest round is all nitpicks.
    Either way the reported `cycles` is the raw round count (what the human
    sees). The state machine decides what a stop means for routing (READY when
    otherwise ready, else the real blocker).
    """
    rounds = build_rounds(ctx, required=required)
    n = len(rounds)

    if n >= ROUND_CAP:
        return BreakerVerdict(
            True,
            "round-cap",
            f"{n} review rounds reached the cap of {ROUND_CAP} — there is no further round",
            n,
        )

    if rounds and is_all_nitpick_round(rounds[-1]):
        return BreakerVerdict(
            True,
            "all-nitpick",
            "the latest review round is all nitpicks (nothing that changes "
            "correctness or behaviour) — stop rather than open another round",
            n,
        )

    return BreakerVerdict(False, None, "", n)
