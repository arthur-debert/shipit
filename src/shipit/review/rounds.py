"""Decide a round's scope: full, or an incremental fix range. See docs/adr/0045-dimension-fanout-single-calibrator.md."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .. import git
from ..identity import Sha
from . import roundrecord

logger = logging.getLogger("shipit.review")


@dataclass(frozen=True)
class RoundPlan:
    """The resolved scope of one review round; ``base``/``head`` are diff endpoints only when ``incremental``."""

    incremental: bool
    base: Sha
    head: Sha
    fallback_reason: str | None = None


def decide_round(
    *,
    base_ref: Sha,
    new_head: Sha,
    last_reviewed_head: Sha | None,
    last_is_ancestor: bool,
) -> RoundPlan:
    """Decide one round's scope; every ambiguous case falls back to a full round."""
    if last_reviewed_head is None or last_reviewed_head == new_head:
        return RoundPlan(incremental=False, base=base_ref, head=new_head)
    if not last_is_ancestor:
        return RoundPlan(
            incremental=False,
            base=base_ref,
            head=new_head,
            fallback_reason=(
                f"last-reviewed head {last_reviewed_head} is not an ancestor of "
                f"new head {new_head} (rebase/force-push) — reviewing the full PR"
            ),
        )
    return RoundPlan(incremental=True, base=last_reviewed_head, head=new_head)


def plan_for_view(
    ctx,
    reviewer: str,
    *,
    base_dir=None,
) -> RoundPlan:
    """Resolve the round scope for ``ctx``; callers gate on :func:`planable` first."""
    new_head = _as_sha(ctx.head_sha)
    base_ref = _as_sha(ctx.base_sha)
    repo_slug = ctx.repo
    if not repo_slug:
        return RoundPlan(incremental=False, base=base_ref, head=new_head)

    raw_last = roundrecord.last_reviewed_head(
        repo_slug=repo_slug,
        pr=ctx.number,
        reviewer=reviewer,
        new_head=str(new_head),
        base_dir=base_dir,
    )
    if raw_last is None:
        return RoundPlan(incremental=False, base=base_ref, head=new_head)
    try:
        last_head = Sha(raw_last)
    except ValueError:
        # A malformed stored head is not a usable fix-range base.
        logger.warning(
            "review-round store held an unusable last-reviewed head %r for pr#%s "
            "(reviewer=%s) — reviewing the full PR",
            raw_last,
            ctx.number,
            reviewer,
        )
        return RoundPlan(incremental=False, base=base_ref, head=new_head)

    last_is_ancestor = git.is_ancestor(last_head, new_head, cwd=ctx.workdir)
    plan = decide_round(
        base_ref=base_ref,
        new_head=new_head,
        last_reviewed_head=last_head,
        last_is_ancestor=last_is_ancestor,
    )
    if plan.fallback_reason:
        logger.info(
            "review round for pr#%s (reviewer=%s) falls back to a full round: %s",
            ctx.number,
            reviewer,
            plan.fallback_reason,
            extra={"pr": ctx.number, "reviewer": reviewer},
        )
    elif plan.incremental:
        logger.info(
            "review round for pr#%s (reviewer=%s) is INCREMENTAL over %s..%s",
            ctx.number,
            reviewer,
            plan.base,
            plan.head,
            extra={"pr": ctx.number, "reviewer": reviewer},
        )
    return plan


def planable(ctx) -> bool:
    """True iff ``ctx`` carries a base sha, head sha, repo slug, and workdir."""
    return bool(
        getattr(ctx, "base_sha", None)
        and getattr(ctx, "head_sha", None)
        and getattr(ctx, "repo", None)
        and getattr(ctx, "workdir", None)
    )


def _as_sha(value) -> Sha:
    return value if isinstance(value, Sha) else Sha(str(value))
