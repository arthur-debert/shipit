"""Review-round stopping rule: address every comment each round, except
stop at the round cap or on a round whose findings are all minor/nit. A
*round* is one head SHA re-reviewed, not one review object."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..finding import Severity
from ..identity import Sha
from .model import ReadinessView, ReviewComment
from .reviewers import ReviewerAdapter, required_adapters
from .severity import finding_severity

# The shipped default; repo policy overrides it via `Roster.round_cap`.
ROUND_CAP = 6

NO_MAJOR_FINDING = "no-major-finding"


@dataclass(frozen=True)
class Round:
    index: int
    commit_id: Sha | None  # None when the wire carried no commit
    findings: tuple[ReviewComment, ...]


@dataclass(frozen=True)
class BreakerVerdict:
    stop: bool
    breaker: str | None
    reason: str
    cycles: int


def build_rounds(
    ctx: ReadinessView,
    required: list[ReviewerAdapter] | None = None,
) -> list[Round]:
    """One Round per head SHA reviewed by a required reviewer; login
    matching is the adapter's job, never re-rolled here."""
    required = required if required is not None else required_adapters(ctx.roster)
    reviews = sorted(
        (r for r in ctx.reviews if any(a.matches(r.author) for a in required)),
        key=lambda r: r.review_id,
    )
    thread_comments = [c for t in ctx.threads for c in t.comments]

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
    """True iff any finding resolves major-or-worse — the merge-block test."""
    return any(finding_severity(f, overrides).blocks_merge for f in rnd.findings)


def evaluate_breakers(
    ctx: ReadinessView,
    required: list[ReviewerAdapter] | None = None,
) -> BreakerVerdict:
    """Apply the stopping rule; the first condition to hit wins. An empty
    latest round has nothing to address and does not fire the stop."""
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
