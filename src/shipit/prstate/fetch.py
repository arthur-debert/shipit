"""Gather all raw GitHub state for one PR into a `ReadinessView`; the only
module that calls the gh adapter on the engine's read paths."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from .. import branchid, events, gh, logcontext
from ..finding import Severity
from ..identity import Repo, Sha
from ..pr import PrId, core_from_node
from .model import (
    _HANDBUILT_REPO,
    ReadinessView,
    Review,
    ReviewComment,
    ReviewFunnelCheck,
    Thread,
)
from .overrides import load_overrides
from .roster import Roster

logger = logging.getLogger("shipit.prstate")

# Funnel check runs arrive on the head commit's `statusCheckRollup`
# alongside the real CI checks, so the build site splits them out here.
_FUNNEL_CHECK_PREFIX = "review: "

# `comments(first: 100)` is un-paginated on purpose: the engine blocks on
# what lives in the first comment. Thread COUNT is the real risk, so
# reviewThreads IS paginated. `reviewRequests` lives here because the gh
# CLI silently omits Bot-typed requested reviewers from its own field.
_THREADS_QUERY = """
query($owner: String!, $name: String!, $pr: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      reviewRequests(first: 100) {
        nodes {
          requestedReviewer {
            ... on User { login }
            ... on Bot { login }
            ... on Team { slug }
          }
        }
      }
      timelineItems(itemTypes: [REVIEW_REQUESTED_EVENT], last: 100) {
        nodes {
          ... on ReviewRequestedEvent {
            createdAt
            requestedReviewer {
              ... on User { login }
              ... on Bot { login }
              ... on Team { slug }
            }
          }
        }
      }
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          comments(first: 100) {
            nodes {
              databaseId
              path
              line
              originalLine
              body
              author { login }
              pullRequestReview { databaseId }
            }
          }
        }
      }
    }
  }
}
"""


def _threads_and_review_requests(
    pr: PrId,
) -> tuple[list[dict], list[dict], dict[str, str]]:
    """Threads (following the cursor to the end), the pending review requests,
    and the per-login `review_requested` edge time."""
    nodes: list[dict] = []
    requests: list[dict] = []
    requested_at: dict[str, str] = {}
    cursor: str | None = None
    while True:
        data = gh.graphql(
            _THREADS_QUERY,
            owner=pr.repo.owner.login,
            name=pr.repo.name,
            pr=pr.number,
            cursor=cursor,
        )
        pull = data["repository"]["pullRequest"]
        if cursor is None:
            requests = [
                rr["requestedReviewer"]
                for rr in pull["reviewRequests"]["nodes"]
                if rr.get("requestedReviewer")
            ]
            requested_at = _requested_at_times(pull["timelineItems"]["nodes"])
        conn = pull["reviewThreads"]
        nodes.extend(conn["nodes"])
        page = conn["pageInfo"]
        if not page["hasNextPage"]:
            return nodes, requests, requested_at
        cursor = page["endCursor"]


def _requested_at_times(events: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for ev in events:
        reviewer = ev.get("requestedReviewer") or {}
        login = reviewer.get("login")
        created = ev.get("createdAt")
        if login and created:
            out[login] = created
    return out


# The attach-verification read. `reviews(last: 50)` suffices — verification
# diffs against a baseline taken seconds earlier.
_ATTACH_QUERY = """
query($owner: String!, $name: String!, $pr: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      reviewRequests(first: 100) {
        nodes {
          requestedReviewer {
            ... on User { login }
            ... on Bot { login }
            ... on Team { slug }
          }
        }
      }
      reviews(last: 50) {
        nodes {
          databaseId
          author { login }
        }
      }
    }
  }
}
"""


def attach_state(pr: PrId) -> tuple[list[str], list[tuple[int, str]]]:
    """Pending review-request logins + (review_id, author) of the newest
    reviews: GitHub can accept a request call yet silently drop the edge."""
    data = gh.graphql(
        _ATTACH_QUERY,
        owner=pr.repo.owner.login,
        name=pr.repo.name,
        pr=pr.number,
    )
    pull = data["repository"]["pullRequest"]
    logins = _requested_logins(
        [
            rr["requestedReviewer"]
            for rr in pull["reviewRequests"]["nodes"]
            if rr.get("requestedReviewer")
        ]
    )
    reviews = [
        (n["databaseId"], (n.get("author") or {}).get("login", ""))
        for n in pull["reviews"]["nodes"]
    ]
    return logins, reviews


# The skip-decision read: `pr review request` only needs to know who is
# already DONE, not the threads/comments/reactions the full `gather` pulls.
_REVIEWS_QUERY = """
query($owner: String!, $name: String!, $pr: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      number
      headRefOid
      headRefName
      baseRefName
      isDraft
      mergeStateStatus
      reviewRequests(first: 100) {
        nodes {
          requestedReviewer {
            ... on User { login }
            ... on Bot { login }
            ... on Team { slug }
          }
        }
      }
      reviews(last: 100) {
        nodes {
          databaseId
          state
          commit { oid }
          author { login }
        }
      }
    }
  }
}
"""


def _bind_branch_identity(head_ref: object) -> None:
    """The stale halves are unbound first: ``bind`` can never clear a key."""
    identity = branchid.derive(head_ref)
    logcontext.unbind("epic", "ws")
    logcontext.bind(epic=identity.epic, ws=identity.ws)


def bind_pr_identity(pr: PrId) -> None:
    """For a verb that mutates without ever building a snapshot."""
    logcontext.bind(pr=pr.number, repo=pr.repo.slug)
    meta = gh.pr_view(str(pr.number), repo=pr.slug, json_fields=["headRefName"])
    _bind_branch_identity(meta.get("headRefName"))


def gather_reviews(pr: PrId, roster: Roster) -> ReadinessView:
    """A light context sufficient for `detect()`: `threads`, `reactions`, and
    `issue_comments` come back empty."""
    start = time.monotonic()
    repo = pr.repo
    logcontext.bind(pr=pr.number, repo=repo.slug)
    data = gh.graphql(
        _REVIEWS_QUERY, owner=repo.owner.login, name=repo.name, pr=pr.number
    )
    pull = data["repository"]["pullRequest"]
    _bind_branch_identity(pull.get("headRefName"))
    requested = _requested_logins(
        [
            rr["requestedReviewer"]
            for rr in pull["reviewRequests"]["nodes"]
            if rr.get("requestedReviewer")
        ]
    )
    reviews = []
    for n in pull["reviews"]["nodes"]:
        review_id = n["databaseId"]
        if type(review_id) is not int:
            raise ValueError(
                f"malformed review node: databaseId must be int, got {review_id!r}"
            )
        reviews.append(
            Review(
                review_id=review_id,
                author=(n.get("author") or {}).get("login", ""),
                state=n.get("state", ""),
                commit_id=_commit_id((n.get("commit") or {}).get("oid")),
                body="",
            )
        )
    ctx = ReadinessView(
        pr=core_from_node(pull, repo),
        reviews=reviews,
        requested_logins=requested,
        roster=roster,
    )
    duration_ms = int((time.monotonic() - start) * 1000)
    logger.debug(
        "pr#%s light review snapshot fetched in %dms (%d review(s), "
        "%d pending request(s))",
        pr.number,
        duration_ms,
        len(reviews),
        len(requested),
        extra={
            "pr": pr.number,
            "duration_ms": duration_ms,
            "reviews": len(reviews),
            "requested": len(requested),
        },
    )
    return ctx


def gather(
    pr: PrId,
    roster: Roster,
    *,
    sightings: events.Sightings | None = None,
    emit_events: bool = True,
) -> ReadinessView:
    """Fetch every raw input the engine needs for `pr`, live, via `gh`; an
    omitted `sightings` mints one scoped to the snapshot built here."""
    start = time.monotonic()
    sightings = sightings if sightings is not None else events.Sightings()
    repo = pr.repo
    # Bound at the fetch seam, so the gh Exec records this fetch produces
    # already carry pr/repo.
    logcontext.bind(pr=pr.number, repo=repo.slug)
    base = f"repos/{repo.slug}"
    meta = gh.pr_meta(pr)
    _bind_branch_identity(meta.get("headRefName"))
    thread_nodes, review_requests, requested_at = _threads_and_review_requests(pr)
    meta["reviewRequests"] = review_requests
    ctx = context_from_raw(
        repo=repo,
        meta=meta,
        reviews_json=gh.rest(f"{base}/pulls/{pr.number}/reviews", paginate=True) or [],
        thread_nodes=thread_nodes,
        reactions=gh.rest(f"{base}/issues/{pr.number}/reactions", paginate=True) or [],
        issue_comments=gh.rest(f"{base}/issues/{pr.number}/comments", paginate=True)
        or [],
        roster=roster,
        requested_at=requested_at,
        overrides=load_overrides(repo, pr.number),
        # Stamped once here: the engine never calls a clock.
        now=datetime.now(UTC),
        sightings=sightings,
        emit_events=emit_events,
    )
    # The gather is the engine's first sight of a landed review; a PENDING
    # review is an unsubmitted draft and has not landed.
    if emit_events:
        for review in ctx.reviews:
            if review.state == "PENDING":
                continue
            events.emit_once(
                sightings,
                logger,
                "review.received",
                (repo.slug, pr.number, review.review_id),
                "review received from %s on pr#%s (%s)",
                review.author,
                pr.number,
                review.state.lower(),
                extra={
                    "pr": pr.number,
                    "reviewer": review.author,
                    "review_id": review.review_id,
                    "review_state": review.state,
                },
            )
    duration_ms = int((time.monotonic() - start) * 1000)
    logger.info(
        "pr#%s snapshot gathered in %dms (%d review(s), %d thread(s), %d check(s))",
        pr.number,
        duration_ms,
        len(ctx.reviews),
        len(ctx.threads),
        len(ctx.checks),
        extra={
            "pr": pr.number,
            "duration_ms": duration_ms,
            "reviews": len(ctx.reviews),
            "threads": len(ctx.threads),
            "checks_total": len(ctx.checks),
        },
    )
    return ctx


def context_from_raw(
    *,
    meta: dict,
    reviews_json: list[dict],
    thread_nodes: list[dict],
    reactions: list[dict],
    issue_comments: list[dict],
    repo: Repo | None = None,
    roster: Roster | None = None,
    requested_at: dict[str, str] | None = None,
    overrides: dict[int, Severity] | None = None,
    now: datetime | None = None,
    sightings: events.Sightings | None = None,
    emit_events: bool = True,
) -> ReadinessView:
    """Pure: assemble a `ReadinessView` from raw gh payloads. No network.

    `now` is a parameter rather than a `datetime.now()` default precisely
    so the engine stays clock-free.
    """
    ci_checks, review_funnel = _partition_checks(meta.get("statusCheckRollup") or [])
    return ReadinessView(
        pr=core_from_node(meta, repo or _HANDBUILT_REPO),
        # Readiness-only; the shared PR core carries `merge_state`.
        mergeable=meta.get("mergeable"),
        reviews=[_review(r) for r in reviews_json],
        threads=[_thread(n) for n in thread_nodes],
        reactions=reactions,
        issue_comments=issue_comments,
        requested_logins=_requested_logins(meta.get("reviewRequests") or []),
        checks=ci_checks,
        review_funnel=review_funnel,
        now=now,
        roster=roster if roster is not None else Roster(),
        requested_at=requested_at or {},
        overrides=overrides or {},
        sightings=sightings if sightings is not None else events.Sightings(),
        emit_events=emit_events,
    )


def _partition_checks(
    rollup: list[dict],
) -> tuple[list[dict], list[ReviewFunnelCheck]]:
    """Split a head-commit status rollup into (CI checks, funnel breadcrumbs);
    left in `checks`, a failed funnel run would read as failing CI."""
    ci_checks: list[dict] = []
    funnel: list[ReviewFunnelCheck] = []
    for entry in rollup:
        name = entry.get("name") or ""
        if name.startswith(_FUNNEL_CHECK_PREFIX):
            funnel.append(
                ReviewFunnelCheck(
                    reviewer=name[len(_FUNNEL_CHECK_PREFIX) :],
                    status=entry.get("status"),
                    conclusion=entry.get("conclusion"),
                    started_at=entry.get("startedAt"),
                )
            )
        else:
            ci_checks.append(entry)
    return ci_checks, funnel


def _commit_id(oid: str | None) -> Sha | None:
    return None if oid is None else Sha(oid)


def _review(raw: dict) -> Review:
    review_id = raw["id"]
    if type(review_id) is not int:
        raise ValueError(f"malformed review payload: id must be int, got {review_id!r}")
    return Review(
        review_id=review_id,
        author=(raw.get("user") or {}).get("login", ""),
        state=raw.get("state", ""),
        commit_id=_commit_id(raw.get("commit_id")),
        body=raw.get("body") or "",
    )


def _thread(node: dict) -> Thread:
    thread_id = node["id"]
    if not isinstance(thread_id, str) or not thread_id:
        raise ValueError(
            f"malformed thread node: id must be a non-empty str, got {thread_id!r}"
        )
    is_resolved = node["isResolved"]
    if not isinstance(is_resolved, bool):
        raise ValueError(
            f"malformed thread node: isResolved must be a bool, got {is_resolved!r}"
        )
    comments = []
    for c in node["comments"]["nodes"]:
        comment_id = c["databaseId"]
        if type(comment_id) is not int:
            raise ValueError(
                f"malformed review comment node: databaseId must be int, "
                f"got {comment_id!r}"
            )
        # A comment may carry no review, but a present value must be an int.
        review_id = (c.get("pullRequestReview") or {}).get("databaseId")
        if review_id is not None and type(review_id) is not int:
            raise ValueError(
                f"malformed review comment node: pullRequestReview.databaseId "
                f"must be int, got {review_id!r}"
            )
        comments.append(
            ReviewComment(
                comment_id=comment_id,
                path=c.get("path") or "",
                line=c.get("line") or c.get("originalLine"),
                body=c.get("body") or "",
                author=(c.get("author") or {}).get("login", ""),
                review_id=review_id,
            )
        )
    return Thread(
        thread_id=thread_id, is_resolved=is_resolved, comments=tuple(comments)
    )


def _requested_logins(review_requests: list[dict]) -> list[str]:
    out = [
        (rr.get("login") or rr.get("name") or rr.get("slug") or "")
        for rr in review_requests
    ]
    return [x for x in out if x]
