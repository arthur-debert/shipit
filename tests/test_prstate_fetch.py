"""The requested-reviewers fetch path — the gh-CLI Bot-omission regression.

`gh pr view --json reviewRequests` silently omits Bot-typed requested
reviewers: after `gh pr edit --add-reviewer @copilot`, REST shows
`requested_reviewers: [{login: "Copilot", type: "Bot"}]` while gh's JSON field
returns `[]`. Sourced from that field, `CopilotAdapter.detect()` could NEVER
read REQUESTED — `pr status` kept demanding "request for the current head"
even with the request already pending. Requested reviewers therefore come from
GraphQL `reviewRequests` (whose union includes Bots), riding along on the
review-threads query. These tests pin `gather()`'s assembly of that path with
the network mocked at the gh-adapter boundary.
"""

from __future__ import annotations

import pytest
from shipit.identity import Sha, repo_from_slug
from shipit.prstate import fetch
from shipit.prstate.model import ReviewLifecycle
from shipit.prstate.reviewers import CopilotAdapter
from shipit.prstate.reviewers_config import default_roster
from shipit.prstate.roster import Roster, RosterEntry

# Full, validated commit identities (COR02) for the wire fixtures.
HEAD = "abc1234" + "0" * 33
OLD = "dead" * 10
NEW = "beef" * 10


def _graphql_page(
    review_requests: list[dict],
    threads: list[dict] | None = None,
    timeline: list[dict] | None = None,
) -> dict:
    return {
        "repository": {
            "pullRequest": {
                "reviewRequests": {"nodes": review_requests},
                # The ReviewRequestedEvent timeline (WS03): the request-edge times
                # the App reviewer's wait window ages against.
                "timelineItems": {"nodes": timeline or []},
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": threads or [],
                },
            }
        }
    }


def _wire(monkeypatch, review_requests: list[dict], timeline: list[dict] | None = None):
    monkeypatch.setattr(fetch.gh, "current_repo", lambda: repo_from_slug("owner/repo"))
    monkeypatch.setattr(
        fetch.gh,
        "pr_meta",
        lambda pr: {
            # The live gh-view payload: no reviewRequests key at all (pr_meta
            # no longer asks for the field gh renders wrong for Bots).
            "number": 558,
            "headRefOid": HEAD,
            "isDraft": True,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "BLOCKED",
            "statusCheckRollup": [],
        },
    )
    monkeypatch.setattr(
        fetch.gh,
        "graphql",
        lambda query, **vars: _graphql_page(review_requests, timeline=timeline),
    )
    monkeypatch.setattr(fetch.gh, "rest", lambda *args, **kwargs: [])


def test_bot_typed_request_yields_copilot_requested(monkeypatch):
    # The regression: a Bot-typed requested reviewer (login "Copilot") must
    # surface in requested_logins and read as REQUESTED through the adapter.
    _wire(monkeypatch, [{"requestedReviewer": {"login": "Copilot"}}])
    ctx = fetch.gather(558, default_roster())
    assert ctx.requested_logins == ["Copilot"]
    assert CopilotAdapter().detect(ctx) is ReviewLifecycle.REQUESTED


def test_team_request_surfaces_by_slug(monkeypatch):
    # Team nodes carry `slug`, not `login`; a null requestedReviewer (e.g. a
    # deleted account) is skipped rather than crashing the fetch.
    _wire(
        monkeypatch,
        [
            {"requestedReviewer": {"slug": "platform-team"}},
            {"requestedReviewer": None},
        ],
    )
    ctx = fetch.gather(558, default_roster())
    assert ctx.requested_logins == ["platform-team"]


def test_no_pending_requests_reads_not_requested(monkeypatch):
    _wire(monkeypatch, [])
    ctx = fetch.gather(558, default_roster())
    assert ctx.requested_logins == []
    assert CopilotAdapter().detect(ctx) is ReviewLifecycle.NOT_REQUESTED


def test_review_requested_edge_time_carried_for_the_app_wait_window(monkeypatch):
    # WS03: the App reviewer's `review_requested` edge time comes from the timeline
    # (GraphQL `reviewRequests` has none), keyed by login. The LATEST event per login
    # wins — a re-request supersedes an earlier one — so the current edge's age is
    # what the wait window measures.
    _wire(
        monkeypatch,
        [{"requestedReviewer": {"login": "Copilot"}}],
        timeline=[
            {
                "createdAt": "2026-01-01T00:00:00Z",
                "requestedReviewer": {"login": "Copilot"},
            },
            {
                "createdAt": "2026-01-01T00:10:00Z",
                "requestedReviewer": {"login": "Copilot"},
            },
        ],
    )
    ctx = fetch.gather(558, default_roster())
    assert ctx.requested_at == {"Copilot": "2026-01-01T00:10:00Z"}


# --- the light skip-decision fetch (release#852) ----------------------------


def _reviews_page(
    review_requests: list[dict],
    reviews: list[dict],
    head: str = HEAD,
    *,
    is_draft: bool = False,
) -> dict:
    # The light query now selects the full PR core (number/isDraft/baseRefName/
    # mergeStateStatus) alongside the head sha, so the core rides on the ONE call
    # already in flight and `gather_reviews` no longer hardcodes `is_draft`.
    return {
        "repository": {
            "pullRequest": {
                "number": 558,
                "headRefOid": head,
                "baseRefName": "main",
                "isDraft": is_draft,
                "mergeStateStatus": "CLEAN",
                "reviewRequests": {"nodes": review_requests},
                "reviews": {"nodes": reviews},
            }
        }
    }


def test_gather_reviews_fetches_only_the_skip_decision_inputs(monkeypatch):
    # release#852: the bare-request skip path uses a LIGHT fetch — one GraphQL
    # call for head sha + reviews + requested reviewers + rerun policy, and NO
    # threads-cursor walk or reactions/issue-comment REST pagination. `rest` is
    # wired to blow up so any stray pagination fails the test.
    monkeypatch.setattr(fetch.gh, "current_repo", lambda: repo_from_slug("owner/repo"))
    monkeypatch.setattr(
        fetch.gh,
        "rest",
        lambda *a, **k: pytest.fail(
            "gather_reviews must not hit the REST pagination paths"
        ),
    )
    monkeypatch.setattr(
        fetch,
        "_threads_and_review_requests",
        lambda *a, **k: pytest.fail("no threads walk"),
    )
    monkeypatch.setattr(
        fetch.gh,
        "graphql",
        lambda query, **vars: _reviews_page(
            [{"requestedReviewer": {"login": "Copilot"}}],
            [
                {
                    "databaseId": 11,
                    "state": "COMMENTED",
                    "commit": {"oid": HEAD},
                    "author": {"login": "Copilot"},
                }
            ],
            is_draft=True,
        ),
    )
    ctx = fetch.gather_reviews(558, default_roster())
    assert ctx.head_sha == Sha(HEAD)
    # The core is REAL now, not hardcoded: the light path reads `is_draft` off its
    # own query (the killed `is_draft=False` trap) and composes the PR identity.
    assert ctx.is_draft is True
    assert ctx.pr.number == 558
    assert ctx.requested_logins == ["Copilot"]
    assert [(r.review_id, r.author, r.commit_id) for r in ctx.reviews] == [
        (11, "Copilot", Sha(HEAD))
    ]
    # A counting review on the head → DONE (review-once any-head); the skip
    # decision is correct off the light context.
    assert CopilotAdapter().detect(ctx) in (
        ReviewLifecycle.DONE_CLEAN,
        ReviewLifecycle.DONE_COMMENTS,
    )


def test_gather_reviews_threads_the_rerun_policy(monkeypatch):
    # The rerun policy must ride on the light context so detect() is head-strict
    # for rerun=True reviewers. With copilot rerun=True and the only review on an
    # OLD head, copilot is stale → reads back REQUESTED (still pending), not DONE.
    monkeypatch.setattr(fetch.gh, "current_repo", lambda: repo_from_slug("owner/repo"))
    monkeypatch.setattr(fetch.gh, "rest", lambda *a, **k: [])
    roster = Roster((RosterEntry(name="copilot", required=True, rerun=True),))
    monkeypatch.setattr(
        fetch.gh,
        "graphql",
        lambda query, **vars: _reviews_page(
            [{"requestedReviewer": {"login": "Copilot"}}],
            [
                {
                    "databaseId": 11,
                    "state": "COMMENTED",
                    "commit": {"oid": OLD},
                    "author": {"login": "Copilot"},
                }
            ],
            head=NEW,
        ),
    )
    ctx = fetch.gather_reviews(558, roster)
    assert ctx.roster.entry("copilot").rerun is True
    assert CopilotAdapter().detect(ctx) is ReviewLifecycle.REQUESTED


# --- identity/decision fields die loudly at the wire boundary (#330) --------


def _thread_node(**overrides) -> dict:
    node = {
        "id": "RT_kwDOq1",
        "isResolved": False,
        "comments": {
            "nodes": [
                {
                    "databaseId": 7,
                    "path": "a.py",
                    "line": 3,
                    "body": "finding",
                    "author": {"login": "codex"},
                    "pullRequestReview": {"databaseId": 11},
                }
            ]
        },
    }
    node.update(overrides)
    return node


def test_gather_reviews_rejects_malformed_review_database_id(monkeypatch):
    # The GraphQL light path: a non-int (or bool) databaseId is a malformed
    # review node and raises at the parse site, naming the wire field.
    monkeypatch.setattr(fetch.gh, "current_repo", lambda: repo_from_slug("owner/repo"))
    monkeypatch.setattr(
        fetch.gh,
        "graphql",
        lambda query, **vars: _reviews_page(
            [],
            [
                {
                    "databaseId": True,
                    "state": "COMMENTED",
                    "commit": {"oid": HEAD},
                    "author": {"login": "Copilot"},
                }
            ],
        ),
    )
    with pytest.raises(ValueError, match="databaseId must be int"):
        fetch.gather_reviews(558, default_roster())


def test_rest_review_id_happy_path_and_malformed():
    review = fetch._review({"id": 42, "state": "APPROVED", "user": {"login": "codex"}})
    assert review.review_id == 42
    for bad in (True, "42", None):
        with pytest.raises(ValueError, match="id must be int"):
            fetch._review({"id": bad, "state": "APPROVED"})


def test_thread_happy_path():
    thread = fetch._thread(_thread_node())
    assert thread.thread_id == "RT_kwDOq1"
    assert thread.is_resolved is False
    assert [c.comment_id for c in thread.comments] == [7]


def test_thread_id_must_be_non_empty_str():
    for bad in ("", None, 12):
        with pytest.raises(ValueError, match="id must be a non-empty str"):
            fetch._thread(_thread_node(id=bad))


def test_is_resolved_must_be_exact_bool():
    # The readiness gate: a truthy non-bool like "false" must never read as
    # resolved — it raises instead.
    for bad in ("false", "true", 1, None):
        with pytest.raises(ValueError, match="isResolved must be a bool"):
            fetch._thread(_thread_node(isResolved=bad))


def test_comment_database_id_must_be_int():
    for bad in (True, "7", None):
        node = _thread_node()
        node["comments"]["nodes"][0]["databaseId"] = bad
        with pytest.raises(ValueError, match="databaseId must be int"):
            fetch._thread(node)


def test_comment_review_id_none_allowed_but_present_must_be_int():
    # `review_id` associates the comment with its round in build_rounds();
    # a detached comment reads as None, but a present value must be an exact
    # int — a malformed "11" or True must never silently break the association.
    node = _thread_node()
    node["comments"]["nodes"][0]["pullRequestReview"] = None
    assert fetch._thread(node).comments[0].review_id is None
    for bad in (True, "11"):
        node = _thread_node()
        node["comments"]["nodes"][0]["pullRequestReview"] = {"databaseId": bad}
        with pytest.raises(
            ValueError, match=r"pullRequestReview\.databaseId must be int"
        ):
            fetch._thread(node)


def test_commit_id_boundary_none_stays_none_and_present_is_validated():
    """`_commit_id` distinguishes an absent oid from a present-but-malformed one.

    A review that carries no commit reads as honestly-unknown ``None``. A present
    value — including an empty string, the classic silent-falsey trap — is handed
    to :class:`Sha`, which raises loudly rather than masquerading as unknown (the
    fail-loud staleness boundary this WS exists to add).
    """
    assert fetch._commit_id(None) is None
    assert fetch._commit_id(HEAD) == Sha(HEAD)
    with pytest.raises(ValueError):
        fetch._commit_id("")
