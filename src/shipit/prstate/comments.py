"""List / reply / resolve PR review threads, over the `gh` boundary.

Reply and resolve are the two write actions the review loop needs after
triaging a comment (fix-and-push or push-back-with-rationale). Listing returns
the open threads with the handles those actions require.
"""

from __future__ import annotations

import logging

from .. import gh
from ..pr import PrId
from . import fetch
from .model import Thread
from .roster import Roster

#: The engine's logger (shared name with the rest of ``shipit.prstate``): a
#: reply / resolve is a PR mutation, so it gets a lifecycle record here at the
#: act (LOG03) — before this, the Exec debug transport line was its only trace.
logger = logging.getLogger("shipit.prstate")

_RESOLVE = """
mutation($threadId: ID!) {
  resolveReviewThread(input: { threadId: $threadId }) {
    thread { isResolved }
  }
}
"""


def open_threads(pr: PrId) -> list[Thread]:
    """All unresolved review threads on the PR (each carries path/line/ids).

    Thread listing is independent of reviewer configuration — `open_threads`
    reads the raw thread set off the snapshot, which `gather` fetches regardless
    of the Roster — so this passes the EMPTY :class:`Roster` rather than paying
    for a config read (`load_roster`) whose per-reviewer settings nothing here
    consults."""
    return fetch.gather(pr, Roster()).open_threads()


def reply(pr: PrId, comment_id: int, body: str) -> None:
    """Reply to a review comment, keeping the thread (does not resolve it).

    The endpoint knowledge lives in the adapter (:func:`shipit.gh.pr_review_reply`)
    — before the PROC02-WS01 merge this module re-spelled the same REST call.
    """
    try:
        gh.pr_review_reply(pr, comment_id, body)
    except Exception:
        # A propagating failure (glassbox spray): the mutation died — record it
        # at ERROR with the exception attached, then let it propagate unchanged.
        logger.error(
            "review-thread reply failed on pr#%s (comment %s)",
            pr.number,
            comment_id,
            exc_info=True,
            extra={"pr": pr.number, "comment_id": comment_id},
        )
        raise
    logger.info(
        "review-thread reply posted on pr#%s (comment %s)",
        pr.number,
        comment_id,
        extra={"pr": pr.number, "comment_id": comment_id},
    )


def resolve(pr: PrId, thread_id: str) -> None:
    """Mark a review thread resolved via the GraphQL mutation.

    Takes the PR number alongside the thread handle so the mutation milestone
    carries ``pr`` on the record itself (every caller holds it — threads come
    from :func:`open_threads`), not only via an ambient context bind.
    """
    try:
        gh.graphql(_RESOLVE, threadId=thread_id)
    except Exception:
        logger.error(
            "review-thread resolve failed on pr#%s (thread %s)",
            pr.number,
            thread_id,
            exc_info=True,
            extra={"pr": pr.number, "thread_id": thread_id},
        )
        raise
    logger.info(
        "review-thread resolved on pr#%s (thread %s)",
        pr.number,
        thread_id,
        extra={"pr": pr.number, "thread_id": thread_id},
    )
