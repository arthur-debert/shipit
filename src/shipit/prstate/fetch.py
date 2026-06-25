"""Gather all raw GitHub state for one PR into a `PullContext`.

The only module that calls `ghapi` on read paths. The raw-JSON -> model parsing
is split out (`context_from_raw`) so tests can build a context from recorded
fixtures without the network, exercising the exact code `gather()` runs live.
"""

from __future__ import annotations

from . import ghapi
from .model import PullContext, Review, ReviewComment, Thread

# `comments(first: 100)` is deliberately un-paginated: the engine gates on a
# thread's existence + `isResolved` + its root author, all of which live in the
# thread node and its first comment, so truncating a >100-comment thread's tail
# can't flip a gating decision. Thread COUNT is the real risk (a missed thread
# is a missed unresolved blocker), so reviewThreads IS paginated via the cursor.
#
# `pullRequestReview { databaseId }` ties each comment back to the review that
# produced it — that is how the stopping rule groups findings into review
# rounds now that the REST `/pulls/{n}/comments` fetch is gone (it surfaced only
# a subset of inline comments and missed second-bot reviews; release#515).
#
# `reviewRequests` lives HERE, not in `gh pr view --json reviewRequests`: the gh
# CLI silently omits Bot-typed requested reviewers from that field (REST shows
# `{login: "Copilot", type: "Bot"}`; gh returns `[]`), so a requested Copilot
# could never read as REQUESTED through the adapter. The GraphQL union includes
# Bots. Un-paginated (first: 100): no PR has 100 pending reviewer requests.
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
    owner: str, name: str, pr: int
) -> tuple[list[dict], list[dict]]:
    """Every review-thread node for the PR plus its pending review requests.

    Threads follow the cursor to the end: without pagination a PR with >100
    threads would silently truncate, and a dropped unresolved thread reads as
    READY when it isn't. Review requests ride along on the first page only
    (the connection is identical on every page).
    """
    nodes: list[dict] = []
    requests: list[dict] = []
    cursor: str | None = None
    while True:
        data = ghapi.graphql(
            _THREADS_QUERY, owner=owner, name=name, pr=pr, cursor=cursor
        )
        pull = data["repository"]["pullRequest"]
        if cursor is None:
            requests = [
                rr["requestedReviewer"]
                for rr in pull["reviewRequests"]["nodes"]
                if rr.get("requestedReviewer")
            ]
        conn = pull["reviewThreads"]
        nodes.extend(conn["nodes"])
        page = conn["pageInfo"]
        if not page["hasNextPage"]:
            return nodes, requests
        cursor = page["endCursor"]


# The attach-verification read (release#614). One light GraphQL call: the
# pending review requests (the same Bot-inclusive union as _THREADS_QUERY —
# gh's `pr view --json reviewRequests` omits Bots) plus the NEWEST submitted
# reviews. `reviews(last: 50)` is deliberate: verification only diffs against
# a baseline taken seconds earlier, so a fresh review is always in the tail —
# an old review can only leave the window if 50+ reviews land mid-poll.
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


def attach_state(pr: int) -> tuple[list[str], list[tuple[int, str]]]:
    """Pending review-request logins + (review_id, author) of the newest reviews.

    The read side of request-attach verification (release#614): GitHub can
    accept a review-request call yet silently drop the edge, so after placing
    a request the verb polls this until the reviewer shows up in the pending
    requests — or has already submitted a fresh review that consumed it.
    """
    owner, name = ghapi.repo_slug()
    data = ghapi.graphql(_ATTACH_QUERY, owner=owner, name=name, pr=pr)
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


# The skip-decision read (release#852). A `pr review request` on the bare path
# runs frequently and only needs to know who is already DONE — which the
# rerun-aware `detect` decides from the head SHA, the submitted reviews (with the
# commit each was made against), the pending review-request logins, and the
# per-reviewer rerun policy. It does NOT need the review THREADS, issue-comment,
# or reaction pagination the full `gather` pulls (those only refine DONE_CLEAN vs
# DONE_COMMENTS — both already DONE — or feed the non-requestable Gemini adapter,
# which is never in the required/skip set). One light GraphQL call replaces the
# threads-cursor walk + three paginated REST fetches. `reviews(last: 100)` is
# deliberately the recent tail: the skip decision only cares whether a reviewer
# has a counting review on this PR, and a reviewer's own latest review is always
# in the tail unless 100+ reviews have landed.
_REVIEWS_QUERY = """
query($owner: String!, $name: String!, $pr: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $pr) {
      headRefOid
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


def gather_reviews(pr: int) -> PullContext:
    """A LIGHT context sufficient for `detect()` — head SHA + reviews + pending
    review requests + the rerun policy, nothing else.

    The read side of the bare `pr review request` skip decision (release#852):
    `detect()` reads only `reviews_on_head()`/`reviews_any_head()` (head SHA +
    reviews), `requested_logins`, and `reviewer_rerun`. This fetches exactly
    those in one GraphQL call, dropping the threads-cursor walk and the
    reactions/issue-comments REST pagination that the full `gather` runs. The
    returned context has empty `threads`/`reactions`/`issue_comments`, so the
    DONE_CLEAN vs DONE_COMMENTS refinement collapses to DONE_CLEAN — irrelevant
    to the skip decision (both are DONE) — and the Gemini adapter (which is not
    requestable, never in the required/skip set) is the only adapter that would
    read the omitted fields. The full `gather` is unchanged for every other path.
    """
    from .reviewers import reviewer_rerun

    owner, name = ghapi.repo_slug()
    data = ghapi.graphql(_REVIEWS_QUERY, owner=owner, name=name, pr=pr)
    pull = data["repository"]["pullRequest"]
    requested = _requested_logins(
        [
            rr["requestedReviewer"]
            for rr in pull["reviewRequests"]["nodes"]
            if rr.get("requestedReviewer")
        ]
    )
    reviews = [
        Review(
            review_id=n["databaseId"],
            author=(n.get("author") or {}).get("login", ""),
            state=n.get("state", ""),
            commit_id=(n.get("commit") or {}).get("oid", ""),
            body="",
        )
        for n in pull["reviews"]["nodes"]
    ]
    return PullContext(
        number=pr,
        head_sha=pull["headRefOid"],
        is_draft=False,
        reviews=reviews,
        requested_logins=requested,
        reviewer_rerun=reviewer_rerun(),
    )


def gather(pr: int) -> PullContext:
    """Fetch every raw input the engine needs for `pr`, live, via `gh`."""
    # Resolved from config (cached) at the build edge — the per-reviewer rerun
    # policy rides on the context so adapter detection stays pure (it reads the
    # policy off `ctx`, never the config). Imported here, not at module top, to
    # keep the import edge one-way (reviewers -> fetch is not a cycle, but the
    # config read is genuinely a build-site concern).
    from .reviewers import reviewer_rerun

    owner, name = ghapi.repo_slug()
    base = f"repos/{owner}/{name}"
    meta = ghapi.pr_meta(pr)
    thread_nodes, review_requests = _threads_and_review_requests(owner, name, pr)
    # Bot-typed requests only surface through GraphQL (see _THREADS_QUERY);
    # the node shape ({login} / {slug}) is what _requested_logins consumes.
    meta["reviewRequests"] = review_requests
    return context_from_raw(
        meta=meta,
        reviews_json=ghapi.rest(f"{base}/pulls/{pr}/reviews", paginate=True) or [],
        thread_nodes=thread_nodes,
        reactions=ghapi.rest(f"{base}/issues/{pr}/reactions", paginate=True) or [],
        issue_comments=ghapi.rest(f"{base}/issues/{pr}/comments", paginate=True) or [],
        reviewer_rerun=reviewer_rerun(),
    )


def context_from_raw(
    *,
    meta: dict,
    reviews_json: list[dict],
    thread_nodes: list[dict],
    reactions: list[dict],
    issue_comments: list[dict],
    reviewer_rerun: dict[str, bool] | None = None,
) -> PullContext:
    """Pure: assemble a `PullContext` from raw gh payloads. No network.

    `reviewer_rerun` is the per-reviewer rerun policy (name -> bool) resolved
    from config at the build site; it defaults to empty (every reviewer
    review-once) so a test/fixture context that omits it gets the shipped
    default behaviour."""
    return PullContext(
        number=meta["number"],
        head_sha=meta["headRefOid"],
        is_draft=bool(meta.get("isDraft")),
        base_ref=meta.get("baseRefName"),
        mergeable=meta.get("mergeable"),
        merge_state=meta.get("mergeStateStatus"),
        reviews=[_review(r) for r in reviews_json],
        threads=[_thread(n) for n in thread_nodes],
        reactions=reactions,
        issue_comments=issue_comments,
        requested_logins=_requested_logins(meta.get("reviewRequests") or []),
        checks=meta.get("statusCheckRollup") or [],
        reviewer_rerun=reviewer_rerun or {},
    )


def _review(raw: dict) -> Review:
    return Review(
        review_id=raw["id"],
        author=(raw.get("user") or {}).get("login", ""),
        state=raw.get("state", ""),
        commit_id=raw.get("commit_id", ""),
        body=raw.get("body") or "",
    )


def _thread(node: dict) -> Thread:
    comments = tuple(
        ReviewComment(
            comment_id=c["databaseId"],
            path=c.get("path") or "",
            line=c.get("line") or c.get("originalLine"),
            body=c.get("body") or "",
            author=(c.get("author") or {}).get("login", ""),
            review_id=(c.get("pullRequestReview") or {}).get("databaseId"),
        )
        for c in node["comments"]["nodes"]
    )
    return Thread(
        thread_id=node["id"], is_resolved=node["isResolved"], comments=comments
    )


def _requested_logins(review_requests: list[dict]) -> list[str]:
    # User/Bot requests carry `login`; team requests carry `name`/`slug`.
    out = [
        (rr.get("login") or rr.get("name") or rr.get("slug") or "")
        for rr in review_requests
    ]
    return [x for x in out if x]
