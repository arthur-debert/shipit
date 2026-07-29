"""List / reply / resolve PR review threads, over the `gh` boundary."""

from __future__ import annotations

import logging

from .. import gh
from ..pr import PrId
from . import fetch
from .model import Thread
from .roster import Roster

logger = logging.getLogger("shipit.prstate")

_RESOLVE = """
mutation($threadId: ID!) {
  resolveReviewThread(input: { threadId: $threadId }) {
    thread { isResolved }
  }
}
"""


def open_threads(pr: PrId) -> list[Thread]:
    """All unresolved review threads on the PR, each carrying path/line/ids.

    Nothing here consults per-reviewer settings, so it passes the empty
    Roster rather than paying for a config read.
    """
    return fetch.gather(pr, Roster()).open_threads()


def reply(pr: PrId, comment_id: int, body: str) -> None:
    """Reply to a review comment, keeping the thread (does not resolve it)."""
    try:
        gh.pr_review_reply(pr, comment_id, body)
    except Exception:
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
    """Mark a review thread resolved; `pr` rides along so the milestone
    record carries it rather than relying on an ambient bind."""
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
