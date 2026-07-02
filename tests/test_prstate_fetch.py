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
from shipit.identity import Sha
from shipit.prstate import fetch
from shipit.prstate.model import ReviewLifecycle
from shipit.prstate.reviewers import CopilotAdapter

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
    monkeypatch.setattr(fetch.gh, "repo_slug", lambda: ("owner", "repo"))
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
    ctx = fetch.gather(558)
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
    ctx = fetch.gather(558)
    assert ctx.requested_logins == ["platform-team"]


def test_no_pending_requests_reads_not_requested(monkeypatch):
    _wire(monkeypatch, [])
    ctx = fetch.gather(558)
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
    ctx = fetch.gather(558)
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
    monkeypatch.setattr(fetch.gh, "repo_slug", lambda: ("owner", "repo"))
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
    ctx = fetch.gather_reviews(558)
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
    monkeypatch.setattr(fetch.gh, "repo_slug", lambda: ("owner", "repo"))
    monkeypatch.setattr(fetch.gh, "rest", lambda *a, **k: [])
    from shipit.prstate import reviewers, reviewers_config

    reviewers._reset_required_cache()
    monkeypatch.setattr(
        reviewers_config, "load_override", lambda root=None: {"copilot": True}
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
                    "commit": {"oid": OLD},
                    "author": {"login": "Copilot"},
                }
            ],
            head=NEW,
        ),
    )
    ctx = fetch.gather_reviews(558)
    reviewers._reset_required_cache()
    assert ctx.reviewer_rerun.get("copilot") is True
    assert CopilotAdapter().detect(ctx) is ReviewLifecycle.REQUESTED


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
