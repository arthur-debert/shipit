from __future__ import annotations

import pytest

from shipit.identity import Sha, repo_from_slug
from shipit.pr import PrId
from shipit.prstate import fetch
from shipit.prstate.model import ReviewLifecycle
from shipit.prstate.reviewers import CopilotAdapter
from shipit.prstate.reviewers_config import default_roster
from shipit.prstate.roster import Roster, RosterEntry

HEAD = "abc1234" + "0" * 33
OLD = "dead" * 10
NEW = "beef" * 10

REPO = repo_from_slug("owner/repo")
TARGET = PrId(repo=REPO, number=558)


def _graphql_page(
    review_requests: list[dict],
    threads: list[dict] | None = None,
    timeline: list[dict] | None = None,
) -> dict:
    return {
        "repository": {
            "pullRequest": {
                "reviewRequests": {"nodes": review_requests},
                "timelineItems": {"nodes": timeline or []},
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": threads or [],
                },
            }
        }
    }


def _wire(
    monkeypatch,
    review_requests: list[dict],
    timeline: list[dict] | None = None,
    head_ref: str = "issues/558/work",
):
    monkeypatch.setattr(
        fetch.gh,
        "current_repo",
        lambda *a, **k: pytest.fail(
            "gather must not resolve the ambient repo — it rides in on the PrId"
        ),
    )
    monkeypatch.setattr(
        fetch.gh,
        "pr_meta",
        lambda pr: {
            "number": 558,
            "headRefOid": HEAD,
            "headRefName": head_ref,
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
    _wire(monkeypatch, [{"requestedReviewer": {"login": "Copilot"}}])
    ctx = fetch.gather(TARGET, default_roster())
    assert ctx.requested_logins == ["Copilot"]
    assert CopilotAdapter().detect(ctx) is ReviewLifecycle.REQUESTED


def test_team_request_surfaces_by_slug(monkeypatch):
    _wire(
        monkeypatch,
        [
            {"requestedReviewer": {"slug": "platform-team"}},
            {"requestedReviewer": None},
        ],
    )
    ctx = fetch.gather(TARGET, default_roster())
    assert ctx.requested_logins == ["platform-team"]


def test_no_pending_requests_reads_not_requested(monkeypatch):
    _wire(monkeypatch, [])
    ctx = fetch.gather(TARGET, default_roster())
    assert ctx.requested_logins == []
    assert CopilotAdapter().detect(ctx) is ReviewLifecycle.NOT_REQUESTED


def test_gather_threads_the_prid_identity_not_an_ambient_resolution(monkeypatch):
    _wire(monkeypatch, [])
    seen: dict = {}

    def graphql(query, **variables):
        seen.update(variables)
        return _graphql_page([])

    monkeypatch.setattr(fetch.gh, "graphql", graphql)
    meta_targets: list = []

    def pr_meta(pr):
        meta_targets.append(pr)
        return {
            "number": 558,
            "headRefOid": HEAD,
            "isDraft": True,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "BLOCKED",
            "statusCheckRollup": [],
        }

    monkeypatch.setattr(fetch.gh, "pr_meta", pr_meta)
    ctx = fetch.gather(TARGET, default_roster())
    assert ctx.pr.id == TARGET
    assert ctx.pr.repo == REPO
    assert meta_targets == [TARGET]
    assert (seen["owner"], seen["name"], seen["pr"]) == ("owner", "repo", 558)


def test_review_requested_edge_time_carried_for_the_app_wait_window(monkeypatch):
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
    ctx = fetch.gather(TARGET, default_roster())
    assert ctx.requested_at == {"Copilot": "2026-01-01T00:10:00Z"}


def _reviews_page(
    review_requests: list[dict],
    reviews: list[dict],
    head: str = HEAD,
    *,
    is_draft: bool = False,
    head_ref: str = "issues/558/work",
) -> dict:
    return {
        "repository": {
            "pullRequest": {
                "number": 558,
                "headRefOid": head,
                "headRefName": head_ref,
                "baseRefName": "main",
                "isDraft": is_draft,
                "mergeStateStatus": "CLEAN",
                "reviewRequests": {"nodes": review_requests},
                "reviews": {"nodes": reviews},
            }
        }
    }


def test_gather_reviews_fetches_only_the_skip_decision_inputs(monkeypatch):
    monkeypatch.setattr(
        fetch.gh,
        "current_repo",
        lambda *a, **k: pytest.fail("no ambient repo resolution on the light path"),
    )
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
    ctx = fetch.gather_reviews(TARGET, default_roster())
    assert ctx.head_sha == Sha(HEAD)
    assert ctx.is_draft is True
    assert ctx.pr.number == 558
    assert ctx.requested_logins == ["Copilot"]
    assert [(r.review_id, r.author, r.commit_id) for r in ctx.reviews] == [
        (11, "Copilot", Sha(HEAD))
    ]
    assert CopilotAdapter().detect(ctx) in (
        ReviewLifecycle.DONE_CLEAN,
        ReviewLifecycle.DONE_COMMENTS,
    )


def test_gather_reviews_threads_the_rerun_policy(monkeypatch):
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
    ctx = fetch.gather_reviews(TARGET, roster)
    assert ctx.roster.entry("copilot").rerun is True
    assert CopilotAdapter().detect(ctx) is ReviewLifecycle.REQUESTED


def test_gather_reviews_binds_epic_ws_from_a_namespaced_head_branch(monkeypatch):
    from shipit import logcontext

    monkeypatch.setattr(
        fetch.gh,
        "graphql",
        lambda query, **vars: _reviews_page([], [], head_ref="RVW01/WS02"),
    )
    fetch.gather_reviews(TARGET, default_roster())
    bound = logcontext.bound()
    assert bound["pr"] == TARGET.number
    assert bound["epic"] == "RVW01"
    assert bound["ws"] == 2


def test_gather_reviews_binds_nothing_for_a_non_namespaced_head(monkeypatch):
    from shipit import logcontext

    monkeypatch.setattr(
        fetch.gh,
        "graphql",
        lambda query, **vars: _reviews_page([], [], head_ref="issues/375/work"),
    )
    fetch.gather_reviews(TARGET, default_roster())
    bound = logcontext.bound()
    assert "epic" not in bound
    assert "ws" not in bound


def test_gather_binds_epic_ws_from_the_meta_head_branch(monkeypatch):
    from shipit import logcontext

    _wire(monkeypatch, [], head_ref="LOG04/umbrella")
    fetch.gather(TARGET, default_roster())
    bound = logcontext.bound()
    assert bound["epic"] == "LOG04"
    assert "ws" not in bound


def test_fetch_seam_head_branch_is_authoritative_over_stale_identity(monkeypatch):
    from shipit import logcontext

    logcontext.bind(epic="RVW01", ws=2)

    monkeypatch.setattr(
        fetch.gh,
        "graphql",
        lambda query, **vars: _reviews_page([], [], head_ref="LOG04/umbrella"),
    )
    fetch.gather_reviews(TARGET, default_roster())
    bound = logcontext.bound()
    assert bound["epic"] == "LOG04"
    assert "ws" not in bound

    monkeypatch.setattr(
        fetch.gh,
        "graphql",
        lambda query, **vars: _reviews_page([], [], head_ref="issues/375/work"),
    )
    fetch.gather_reviews(TARGET, default_roster())
    bound = logcontext.bound()
    assert "epic" not in bound
    assert "ws" not in bound


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
        fetch.gather_reviews(TARGET, default_roster())


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
    assert fetch._commit_id(None) is None
    assert fetch._commit_id(HEAD) == Sha(HEAD)
    with pytest.raises(ValueError):
        fetch._commit_id("")


def _wire_with_reviews(monkeypatch, reviews_json: list[dict]) -> None:
    _wire(monkeypatch, [])
    monkeypatch.setattr(
        fetch.gh,
        "rest",
        lambda path, **kwargs: reviews_json if path.endswith("/reviews") else [],
    )


def _received_records(caplog):
    import logging as _logging

    from shipit import events

    return [
        r
        for r in caplog.records
        if getattr(r, events.EXTRA_KEY, None) == "review.received"
        and r.levelno == _logging.INFO
    ]


def test_gather_tags_each_landed_review_once_per_invocation(monkeypatch, caplog):
    import logging as _logging

    from shipit import events

    _wire_with_reviews(
        monkeypatch,
        [
            {"id": 11, "user": {"login": "Copilot"}, "state": "COMMENTED"},
            {"id": 12, "user": {"login": "codex-bot"}, "state": "APPROVED"},
        ],
    )
    sightings = events.Sightings()
    with caplog.at_level(_logging.INFO, logger="shipit.prstate"):
        fetch.gather(TARGET, default_roster(), sightings=sightings)
    tagged = _received_records(caplog)
    assert {(r.reviewer, r.review_id, r.review_state) for r in tagged} == {
        ("Copilot", 11, "COMMENTED"),
        ("codex-bot", 12, "APPROVED"),
    }
    assert all(r.pr == TARGET.number for r in tagged)

    caplog.clear()
    with caplog.at_level(_logging.INFO, logger="shipit.prstate"):
        fetch.gather(TARGET, default_roster(), sightings=sightings)
    assert not _received_records(caplog)

    caplog.clear()
    with caplog.at_level(_logging.INFO, logger="shipit.prstate"):
        fetch.gather(TARGET, default_roster())
    assert len(_received_records(caplog)) == 2


def test_gather_does_not_sight_a_pending_review(monkeypatch, caplog):
    import logging as _logging

    _wire_with_reviews(
        monkeypatch,
        [{"id": 13, "user": {"login": "human"}, "state": "PENDING"}],
    )
    with caplog.at_level(_logging.INFO, logger="shipit.prstate"):
        fetch.gather(TARGET, default_roster())
    assert not _received_records(caplog)
