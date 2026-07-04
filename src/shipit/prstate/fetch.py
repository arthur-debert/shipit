"""Gather all raw GitHub state for one PR into a `ReadinessView`.

The only module that calls the gh adapter (`shipit.gh`) on the engine's read
paths. The raw-JSON -> model parsing is split out (`context_from_raw`) so tests
can build a view from recorded fixtures without the network, exercising the
exact code `gather()` runs live.

The target arrives as a `PrId` (ADR-0030): the repo identity rides in on it —
minted once at the verb boundary from the root context, per ADR-0024's offline
identity source — so this path performs ZERO ambient-repo API resolutions (the
former per-gather `gh repo view` shellouts, paid twice per `pr next`, are gone).

The view's cheap CORE (`head_sha`, `base_ref`, `is_draft`, `merge_state`) is read
off the fetched GitHub `pullRequest` node through the ONE `pr.core_from_node`
boundary (ADR-0024) — the SAME builder the review path uses — so `head_sha` is
fetched exactly one way and the light `gather_reviews` path can no longer hardcode
`is_draft`.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from .. import branchid, events, gh, logcontext
from ..identity import Repo, Sha
from ..pr import PrId, core_from_node
from .model import (
    ReadinessView,
    Review,
    ReviewComment,
    ReviewFunnelCheck,
    Thread,
    _HANDBUILT_REPO,
)
from .roster import Roster

#: The engine's logger — a child of the package ``shipit`` logger, shared with
#: :mod:`shipit.prstate.state` so the fetch milestones and the evaluation
#: decision they feed read as one story under ``shipit.prstate``.
logger = logging.getLogger("shipit.prstate")

# The OBS02/ADR-0005 funnel check runs are named `review: <reviewer>` (see
# `shipit.review.checkrun`). They arrive on the head commit's `statusCheckRollup`
# alongside the real CI checks, so the build site recognizes this RESERVED name
# prefix to split them OUT of the CI rollup — keeping the funnel breadcrumbs and
# the CI-checks verdict (`classify_checks`) from crossing. Matching a naming
# convention is NOT branching on a reviewer's name: every funnel run, for any
# reviewer, shares this one prefix.
_FUNNEL_CHECK_PREFIX = "review: "

# `comments(first: 100)` is deliberately un-paginated: the engine blocks on a
# thread's existence + `isResolved` + its root author, all of which live in the
# thread node and its first comment, so truncating a >100-comment thread's tail
# can't flip a blocking decision. Thread COUNT is the real risk (a missed thread
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
#
# `timelineItems(REVIEW_REQUESTED_EVENT)` carries what `reviewRequests` does NOT:
# the TIME each reviewer was requested (`createdAt`). The pending-request union
# above has no timestamp, so the App reviewer's request time — which OBS04-WS03
# ages its wait window against — is sourced from the timeline here. `last: 100` is
# the recent tail in ascending chronological order, so the LATEST event per login
# (a re-request after a push supersedes an earlier one) is the current edge; rides
# the first page only (read when the cursor is None). A LOCAL reviewer has no
# requested edge and ages its check run's `started_at` instead, so it never appears.
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
    """Every review-thread node for the PR, its pending review requests, and the
    per-login `review_requested` edge time.

    Threads follow the cursor to the end: without pagination a PR with >100
    threads would silently truncate, and a dropped unresolved thread reads as
    READY when it isn't. Review requests and the timeline request-times ride along
    on the first page only (those connections are identical on every page).
    """
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
    """Map each requested reviewer login -> the time of its LATEST
    ReviewRequestedEvent (ISO-8601 tz-aware `createdAt`).

    `timelineItems(last: 100)` returns events oldest-first, so iterating in order
    and overwriting keeps the MOST RECENT request per login — the current pending
    edge, whose age WS03 measures (a re-request after a push supersedes the earlier
    one). A non-reviewer timeline node (the union member that isn't a
    ReviewRequestedEvent) has no `requestedReviewer` and is skipped; team requests
    (a `slug`, no `login`) are skipped too — only User/Bot reviewers age."""
    out: dict[str, str] = {}
    for ev in events:
        reviewer = ev.get("requestedReviewer") or {}
        login = reviewer.get("login")
        created = ev.get("createdAt")
        if login and created:
            out[login] = created
    return out


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


def attach_state(pr: PrId) -> tuple[list[str], list[tuple[int, str]]]:
    """Pending review-request logins + (review_id, author) of the newest reviews.

    The read side of request-attach verification (release#614): GitHub can
    accept a review-request call yet silently drop the edge, so after placing
    a request the verb polls this until the reviewer shows up in the pending
    requests — or has already submitted a fresh review that consumed it. The
    repo rides in on the ``PrId`` (ADR-0030) — no ambient resolution per poll.
    """
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
    """Bind the ``epic``/``ws`` a slash-namespaced head branch carries (ADR-0032).

    The PR verbs' per-operation binding site: the fetch is the moment the engine
    learns the PR's head branch, so the dev-cycle identity binds HERE — the same
    seam that binds ``pr``/``repo`` — through the ONE branch-identity parser
    (:func:`shipit.branchid.derive`). This head branch is AUTHORITATIVE for
    ``epic``/``ws``: a prior operation in the same process may have bound a
    different PR's identity, and ``logcontext.bind`` drops ``None`` (so it can
    never CLEAR a key), so the stale halves are unbound first. A non-namespaced
    head (a standalone-issue or freeform branch) derives to nothing and thus
    leaves both keys absent; an umbrella head binds ``epic`` and clears ``ws``.
    Absent keys stay absent, never a placeholder (present-when-bound, ADR-0029).
    """
    identity = branchid.derive(head_ref)
    logcontext.unbind("epic", "ws")
    logcontext.bind(epic=identity.epic, ws=identity.ws)


def bind_pr_identity(pr: PrId) -> None:
    """Bind ``pr``/``repo`` + the head branch's ``epic``/``ws`` WITHOUT a gather.

    The per-operation ADR-0032 binding for a PR verb that mutates without ever
    building a snapshot (today: the ready→draft undo). ``gather`` /
    ``gather_reviews`` bind as a side effect of the fetch they already run; this
    helper is the same seam for a verb with no fetch of its own — one light
    ``headRefName`` read, then the ONE branch-identity parser. A non-namespaced
    head leaves ``epic``/``ws`` absent, exactly as at the gather seams.
    """
    logcontext.bind(pr=pr.number, repo=pr.repo.slug)
    meta = gh.pr_view(str(pr.number), repo=pr.slug, json_fields=["headRefName"])
    _bind_branch_identity(meta.get("headRefName"))


def gather_reviews(pr: PrId, roster: Roster) -> ReadinessView:
    """A LIGHT context sufficient for `detect()` — head SHA + reviews + pending
    review requests + the reviewer Roster, nothing else.

    The read side of the bare `pr review request` skip decision (release#852):
    `detect()` reads only `reviews_on_head()`/`reviews_any_head()` (head SHA +
    reviews), `requested_logins`, and the rerun flag off the Roster. This fetches
    exactly those in one GraphQL call, dropping the threads-cursor walk and the
    reactions/issue-comments REST pagination that the full `gather` runs. The
    returned context has empty `threads`/`reactions`/`issue_comments`, so the
    DONE_CLEAN vs DONE_COMMENTS refinement collapses to DONE_CLEAN — irrelevant
    to the skip decision (both are DONE) — and the Gemini adapter (which is not
    requestable, never in the required/skip set) is the only adapter that would
    read the omitted fields. The full `gather` is unchanged for every other path.

    `roster` is the reviewer configuration the CALLER loaded once at its verb
    boundary (`reviewers_config.load_roster`, CLI01-WS04) — threaded onto the
    view here so adapter detection reads settings off the snapshot, never the
    config.
    """
    start = time.monotonic()
    # The typed repo (ADR-0030/PROC03): the identity rides in on the PrId —
    # minted once at the verb boundary — so the slug/owner/name are read off it
    # rather than re-resolved ambiently per fetch.
    repo = pr.repo
    # Bind the domain keys at the fetch seam (ADR-0029): from the moment the
    # engine starts working on this PR, every subsequent record in-process —
    # including the gh Exec records the fetch itself produces — carries pr/repo.
    logcontext.bind(pr=pr.number, repo=repo.slug)
    data = gh.graphql(
        _REVIEWS_QUERY, owner=repo.owner.login, name=repo.name, pr=pr.number
    )
    pull = data["repository"]["pullRequest"]
    # The PR-verb half of ADR-0032's per-operation binding: epic/ws derived from
    # the slash-namespaced head branch (ADR-0016) the query already carries —
    # nothing hard-coded, absent halves stay absent (None drops at bind).
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
    # The core is read off the SAME `pullRequest` node through the one
    # `core_from_node` boundary — so this light path fetches `is_draft` (and the
    # rest of the core) for real off its GraphQL query, never hardcoding it. The
    # threads/reactions/issue-comments pagination the full `gather` runs is still
    # skipped; only the cheap core rides along on the query already in flight.
    ctx = ReadinessView(
        pr=core_from_node(pull, repo),
        reviews=reviews,
        requested_logins=requested,
        roster=roster,
    )
    # The light fetch is a mechanic of the request verb's skip decision, not a
    # lifecycle milestone — record it at DEBUG (the full `gather` is the info one).
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


def gather(pr: PrId, roster: Roster) -> ReadinessView:
    """Fetch every raw input the engine needs for `pr`, live, via `gh`.

    `roster` is the reviewer configuration as ONE value (CLI01-WS04), loaded by
    the CALLER once at its verb boundary (`reviewers_config.load_roster`) and
    threaded onto the snapshot here — so the engine/adapters read every
    per-reviewer setting off `ctx.roster`, never the config, and no call path
    resolves reviewer settings twice per verb invocation."""
    start = time.monotonic()
    # The typed repo (ADR-0030/PROC03): one Repo value object — riding in on the
    # PrId, minted once at the verb boundary — feeds the log context, the REST
    # base path, the GraphQL variables, and the PR identity below. No per-gather
    # ambient resolution.
    repo = pr.repo
    # Bind the domain keys at the fetch seam (ADR-0029): from the moment the
    # engine starts working on this PR, every subsequent record in-process —
    # including the gh Exec records the fetch itself produces — carries pr/repo.
    logcontext.bind(pr=pr.number, repo=repo.slug)
    base = f"repos/{repo.slug}"
    meta = gh.pr_meta(pr)
    # The PR-verb half of ADR-0032's per-operation binding: epic/ws derived from
    # the slash-namespaced head branch (ADR-0016) the meta read already carries.
    _bind_branch_identity(meta.get("headRefName"))
    thread_nodes, review_requests, requested_at = _threads_and_review_requests(pr)
    # Bot-typed requests only surface through GraphQL (see _THREADS_QUERY);
    # the node shape ({login} / {slug}) is what _requested_logins consumes.
    meta["reviewRequests"] = review_requests
    ctx = context_from_raw(
        # The PR identity's repo — the typed adapter read above (ADR-0024/PROC03).
        repo=repo,
        meta=meta,
        reviews_json=gh.rest(f"{base}/pulls/{pr.number}/reviews", paginate=True) or [],
        thread_nodes=thread_nodes,
        reactions=gh.rest(f"{base}/issues/{pr.number}/reactions", paginate=True) or [],
        issue_comments=gh.rest(f"{base}/issues/{pr.number}/comments", paginate=True)
        or [],
        roster=roster,
        # The App `review_requested` edge times — resolved at the build edge and
        # threaded on so the engine ages the wait window off the snapshot, never
        # the clock (OBS04-WS03).
        requested_at=requested_at,
        # Stamp "now" once, at fetch time. The engine NEVER calls a clock — it
        # reads this off the snapshot — so the wall-clock read lives here, at the
        # build edge, the same place every other impurity (config, network) does.
        now=datetime.now(timezone.utc),
    )
    # The `review.received` dev-cycle event (ADR-0032 / LOG04-WS02): the gather
    # is the engine's first sight of a LANDED review — nothing in shipit posts
    # a remote reviewer's review, so the fetch seam is the strongest witness
    # there is. `emit_once` scopes "first" to the process (one `pr next`
    # invocation gathers up to three times; ADR-0029/0032 reject a cross-run
    # store), keyed by the review's own identity so a reader can dedupe on
    # data. A PENDING review has not landed (it is an unsubmitted draft) and is
    # not sighted.
    for review in ctx.reviews:
        if review.state == "PENDING":
            continue
        events.emit_once(
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
    # The fetch milestone (glassbox spray): the full snapshot is the input every
    # `pr status` / `pr next` decision reads, so its shape + duration are the
    # lifecycle record — at info, with the pr key bound above.
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
    now: datetime | None = None,
) -> ReadinessView:
    """Pure: assemble a `ReadinessView` from raw gh payloads. No network.

    The cheap CORE (`head_sha`, `base_ref`, `is_draft`, `merge_state`) is read off
    `meta` through the one `pr.core_from_node` boundary and packed into the composed
    `PR` — the identical extraction the review path uses. `repo` is the PR identity's
    repo; it defaults to a placeholder because the readiness engine keys on `number`,
    never repo identity (a fixture may omit it), while `gather()` passes the real,
    origin-derived one.

    `roster` is the reviewer configuration as ONE value (CLI01-WS04), loaded at
    the verb boundary and threaded onto the view; it defaults to the EMPTY
    Roster (every reviewer at its shipped defaults: review-once, 20m window) so
    a test/fixture context that omits it gets the default behaviour.

    `requested_at` is the App `review_requested` edge times (login -> ISO-8601);
    it defaults to empty so a fixture that omits it gets no App-side ageing (a
    local reviewer ages its own check-run `started_at`).

    `now` is the injected wall-clock the snapshot carries (a tz-aware UTC
    datetime); `gather()` stamps it at fetch time and a test/fixture passes a
    FIXED value so a recorded snapshot is deterministic. It is a parameter — not
    a default `datetime.now()` — precisely so the engine stays clock-free: the
    only "now" the engine ever sees is the one handed in here.
    """
    ci_checks, review_funnel = _partition_checks(meta.get("statusCheckRollup") or [])
    return ReadinessView(
        pr=core_from_node(meta, repo or _HANDBUILT_REPO),
        # `mergeable` is readiness-only (the async-stale merge fallback), so it stays
        # on the view — the shared PR core carries the authoritative `merge_state`.
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
    )


def _partition_checks(
    rollup: list[dict],
) -> tuple[list[dict], list[ReviewFunnelCheck]]:
    """Split a head-commit status rollup into (CI checks, funnel breadcrumbs).

    The OBS02/ADR-0005 funnel check runs (`review: <reviewer>`) ride the SAME
    `statusCheckRollup` as the real CI checks. Left in `checks`, a failed
    `review: codex-local` run (conclusion FAILURE) would make `classify_checks`
    read the whole CI verdict as FAILING — a degraded local review must never block
    CI. So the funnel runs are lifted out HERE, at the build site: anything whose
    `name` starts with the reserved `review: ` prefix becomes a
    `ReviewFunnelCheck`; everything else stays a CI check. Entries without a
    `name` (legacy StatusContext, keyed by `context`) are CI checks by definition.
    """
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
    """Mint a review's raw ``oid`` into a :class:`Sha` — ``None`` stays ``None``.

    The one wire-read for a review's commit identity (COR02): a review node that
    carries no commit reads as honestly-unknown ``None`` (never a fake empty
    string), while a present-but-malformed oid raises :class:`ValueError` loudly
    at the boundary instead of flowing on to silently fail the staleness compare.
    """
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
        # `review_id` ties the comment back to its review round in
        # `breakers.build_rounds()` — an identity field too. A comment may
        # carry no review (None), but a present value must be an exact int.
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
    # User/Bot requests carry `login`; team requests carry `name`/`slug`.
    out = [
        (rr.get("login") or rr.get("name") or rr.get("slug") or "")
        for rr in review_requests
    ]
    return [x for x in out if x]
