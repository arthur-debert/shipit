"""rounds — decide a review round's SCOPE: full round 1, or an incremental fix range.

The convergence half of the RVW02 review redesign (ADR-0045; PRD "Incremental
rounds"). Round 1 is exhaustive by design: the whole PR diff, reviewed as one
monolithic full-scope pass by default, or as the dimension fan-out when a
reviewer's ``dimensions`` config opts in (ADR-0052). Every round AFTER the
first is cheap and narrow: it
reviews only the *fix range* — ``last-reviewed-head..new-head`` — as ONE
incremental pass with dependency-neighborhood context, new nits suppressed. This
module owns the ONE decision between those two shapes and nothing else.

The decision splits into a PURE core and a thin I/O orchestration, the
value-objects-functional-core split (ADR-0021):

  * :func:`decide_round` is PURE — a :class:`RoundPlan` is a function of
    (the PR base ref tip, new head, the last head this reviewer reviewed, whether
    that last head is still an ancestor of the new head). Unit-testable from four
    shas and a bool; it encodes the whole policy, including the rebase/force-push
    fallback (a non-ancestor last head voids the incremental premise → a full
    round; fail toward over-reviewing).
  * :func:`plan_for_view` is the I/O boundary the review path calls: it reads
    the reviewer's last-reviewed head off the review-round store
    (:func:`shipit.review.roundrecord.last_reviewed_head`) and probes ancestry
    against the checkout (:func:`shipit.git.is_ancestor`), then delegates the
    verdict to :func:`decide_round`.

Both SHAs a plan needs are already known without asking GitHub: the PR's base ref
tip (``baseRefOid``) and new head come off the resolved
:class:`~shipit.review.diff.ReviewView` (``base_sha`` / ``head_sha``), and the
last-reviewed head is the head of this reviewer's most recent round record (rounds
are keyed by head SHA — PRD). ``plan.base`` is the diff base a caller re-diffs over
ONLY for an incremental round — the last-reviewed head — so its recorded range is
``last-reviewed-head..new-head`` (WS06 acceptance). A full round stamps the base ref
tip on ``plan.base`` for the record but the caller never re-diffs over it: the
resolved view already carries the full-PR diff (its own merge-base..head, computed
in :func:`~shipit.review.diff.resolve_pr`), so ``ctx.base_sha`` here is ``baseRefOid``,
NOT that merge base.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .. import git
from ..identity import Sha
from . import roundrecord

logger = logging.getLogger("shipit.review")


@dataclass(frozen=True)
class RoundPlan:
    """The resolved scope of one review round — full round 1, or an incremental
    fix range.

    ``incremental`` is the shape: ``False`` is a full round (round 1, or a
    fallback), reviewed over the resolved view's OWN full-PR diff (the default
    single pass, or the opted-in dimension fan-out — ADR-0052); its ``base`` carries the PR base ref tip (``baseRefOid``) for the
    record, but the caller does NOT re-diff over it. ``True`` is an incremental
    round, reviewed as ONE pass over the fix range ``base..head`` where ``base`` is
    the reviewer's last-reviewed head — here ``base`` / ``head`` ARE the diff
    endpoints the caller re-diffs the :class:`~shipit.review.diff.ReviewView` over,
    and the record keys the range as-is.

    ``fallback_reason`` is set ONLY when a round that COULD have been incremental
    (the reviewer has a prior head) was forced to a full round instead — today
    exactly the rebase/force-push case, where the last-reviewed head is no longer
    an ancestor of the new head. It is ``None`` for a genuine round 1 (no prior
    head at all) and for a real incremental round. The caller logs it so a
    full re-review on a rewritten history is explained, not silent.
    """

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
    """Decide one round's scope from its shas + the ancestry fact. PURE.

    ``base_ref`` is the PR's base ref tip (``baseRefOid``); it is stamped on a full
    round's ``plan.base`` for the record but is NOT used as a diff endpoint (the
    resolved view already carries the full-PR diff). The policy, in order:

      * NO prior head (``last_reviewed_head is None``) — this reviewer has never
        reviewed this PR: a full round 1. ``fallback_reason`` stays ``None`` (a
        first round is not a fallback).
      * The prior head EQUALS the new head — a re-review of the exact same head
        (an idempotent re-request, or a store quirk): there is no fix range to
        review, so a full round. (The store reader already filters this out, so
        it is a defensive belt.)
      * The prior head is NOT an ancestor of the new head
        (``not last_is_ancestor``) — a rebase or force-push rewrote history and
        voided the incremental premise: a full round, with ``fallback_reason``
        set so the over-review is explained. Fail toward over-reviewing (ADR-0045).
      * Otherwise — the prior head is a real ancestor: an INCREMENTAL round over
        the fix range ``last_reviewed_head..new_head`` (``base`` is that prior head).

    ``last_is_ancestor`` is meaningful only when ``last_reviewed_head`` is set;
    the caller passes ``False`` (or anything) when it is ``None`` and the first
    branch wins first.
    """
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
    """Resolve the round scope for a review of ``ctx`` by ``reviewer`` — the I/O
    boundary around :func:`decide_round`.

    Reads ``reviewer``'s last-reviewed head for ``ctx``'s PR off the review-round
    store, and — when there is one — probes whether it is still an ancestor of
    ``ctx``'s head in ``ctx.workdir`` (the checkout that resolved the PR, where
    both commits are present after :func:`~shipit.review.diff.resolve_pr`'s
    fetches). ``ctx`` is a resolved :class:`~shipit.review.diff.ReviewView`:
    ``ctx.base_sha`` is the PR base ref tip (``baseRefOid``, NOT the merge base —
    the view's own full-PR diff is computed over a separate merge base in
    :func:`~shipit.review.diff.resolve_pr`), ``ctx.head_sha`` the new head,
    ``ctx.repo`` the store key, ``ctx.number`` the PR. Callers gate on
    :func:`planable` first, so ``ctx`` here always carries a
    base sha, head sha, repo, and workdir; a resolved PR with no repo history
    (``ctx.repo`` None on a hand-built view) still plans a full round — the honest
    default. ``base_dir`` overrides the store root (tests).
    """
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
        # A malformed stored head is not a usable fix-range base — fall through
        # to a full round rather than diffing against a bad endpoint.
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
    """True iff ``ctx`` carries everything :func:`plan_for_view` needs — a base
    sha, a head sha, a repo slug, and a workdir.

    A resolved :class:`~shipit.review.diff.ReviewView` always does; a bare
    hand-built context (an ad-hoc caller, a test double that only fills the
    fields the fan-out reads) may not, and a round scope cannot be computed for
    one — so the caller (:func:`shipit.review.service.generate_review`) skips
    planning and runs the full round-1 path. Gating here keeps the incremental
    logic out of the way of callers that were never doing rounds at all, and lets
    :func:`plan_for_view` assume the fields are present.
    """
    return bool(
        getattr(ctx, "base_sha", None)
        and getattr(ctx, "head_sha", None)
        and getattr(ctx, "repo", None)
        and getattr(ctx, "workdir", None)
    )


def _as_sha(value) -> Sha:
    """Coerce ``value`` (a :class:`~shipit.identity.Sha` or a raw string) to a
    :class:`Sha` — validity is construction (PROC03)."""
    return value if isinstance(value, Sha) else Sha(str(value))
