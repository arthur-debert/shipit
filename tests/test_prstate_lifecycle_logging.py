"""LOG02-WS03 — the prstate subsystem's lifecycle narration (glassbox spray).

Convention-level tests, per the glassbox testing decision: key lifecycle events
EXIST and CARRY the required flat fields — identified by their fields, never by
per-message string assertions.

Covered here:

  * the fetch milestone: `gather()` records the snapshot's shape + duration at
    INFO and binds the `pr`/`repo` domain keys at the fetch seam (ADR-0029), so
    every subsequent in-process record — the gh Exec records included —
    correlates to the PR;
  * the light `gather_reviews()` fetch records as a DEBUG mechanic;
  * reviewer request/settle transitions: a placed / withdrawn request edge is
    an INFO record carrying `reviewer`/`pr`/`transition`; a local-review request
    that dies records at ERROR with the exception attached;
  * the one semantic gh failure the Exec record cannot carry: a GraphQL
    response with `errors` records at ERROR with the exception attached.
"""

from __future__ import annotations

import json
import logging

import pytest
from shipit import logcontext
from shipit import gh
from shipit.identity import repo_from_slug
from shipit.prstate import fetch
from shipit.prstate.roster import Roster
from shipit.prstate.errors import PrStateError
from shipit.prstate.reviewers import CodexAdapter, CopilotAdapter, GeminiAdapter


def _graphql_page(review_requests=None, threads=None, timeline=None) -> dict:
    return {
        "repository": {
            "pullRequest": {
                "reviewRequests": {"nodes": review_requests or []},
                "timelineItems": {"nodes": timeline or []},
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": threads or [],
                },
            }
        }
    }


def _wire_gather(monkeypatch):
    monkeypatch.setattr(fetch.gh, "current_repo", lambda: repo_from_slug("owner/repo"))
    monkeypatch.setattr(
        fetch.gh,
        "pr_meta",
        lambda pr: {
            "number": 558,
            "headRefOid": "deadbeef" * 5,  # a full 40-hex sha (COR02)
            "isDraft": True,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "BLOCKED",
            "statusCheckRollup": [],
        },
    )
    monkeypatch.setattr(fetch.gh, "graphql", lambda query, **v: _graphql_page())
    monkeypatch.setattr(fetch.gh, "rest", lambda *a, **k: [])


# --- the fetch milestone ---------------------------------------------------


def test_gather_records_an_info_milestone_with_shape_and_duration(monkeypatch, caplog):
    _wire_gather(monkeypatch)
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        ctx = fetch.gather(558, Roster())
    milestones = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and hasattr(r, "duration_ms")
    ]
    assert len(milestones) == 1
    rec = milestones[0]
    assert rec.pr == 558
    assert rec.duration_ms >= 0
    assert rec.reviews == len(ctx.reviews)
    assert rec.threads == len(ctx.threads)
    assert rec.checks_total == len(ctx.checks)


def test_gather_binds_the_pr_and_repo_domain_keys(monkeypatch):
    """The fetch seam binds pr/repo (ADR-0029): from the moment the engine
    starts working on a PR, every subsequent record correlates to it."""
    _wire_gather(monkeypatch)
    assert "pr" not in logcontext.bound()  # clean context (conftest isolation)
    fetch.gather(558, Roster())
    bound = logcontext.bound()
    assert bound["pr"] == 558
    assert bound["repo"] == "owner/repo"


def test_gather_reviews_records_a_debug_mechanic_with_fields(monkeypatch, caplog):
    monkeypatch.setattr(fetch.gh, "current_repo", lambda: repo_from_slug("owner/repo"))
    monkeypatch.setattr(
        fetch.gh,
        "graphql",
        lambda query, **v: {
            "repository": {
                "pullRequest": {
                    "number": 558,
                    "headRefOid": "deadbeef" * 5,  # a full 40-hex sha (COR02)
                    "isDraft": True,
                    "mergeStateStatus": "BLOCKED",
                    "reviewRequests": {"nodes": []},
                    "reviews": {"nodes": []},
                }
            }
        },
    )
    with caplog.at_level(logging.DEBUG, logger="shipit.prstate"):
        fetch.gather_reviews(558, Roster())
    mechanics = [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and hasattr(r, "duration_ms")
    ]
    assert len(mechanics) == 1
    rec = mechanics[0]
    assert rec.pr == 558
    assert rec.reviews == 0
    assert rec.requested == 0
    assert logcontext.bound()["pr"] == 558  # the light path binds too


# --- reviewer request/settle transitions ------------------------------------


def _transition_records(caplog):
    return [r for r in caplog.records if hasattr(r, "transition")]


def test_request_placed_is_an_info_transition_record(monkeypatch, caplog):
    monkeypatch.setattr(gh, "pr_edit_reviewer", lambda pr, handle, remove=False: None)
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        assert CopilotAdapter().request(41) is True
    transitions = _transition_records(caplog)
    assert len(transitions) == 1
    rec = transitions[0]
    assert rec.levelno == logging.INFO
    assert rec.reviewer == "copilot"
    assert rec.pr == 41
    assert rec.transition == "request placed"


def test_cancel_is_an_info_transition_record(monkeypatch, caplog):
    monkeypatch.setattr(gh, "pr_edit_reviewer", lambda pr, handle, remove=False: None)
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        assert CopilotAdapter().cancel(41) is True
    transitions = _transition_records(caplog)
    assert len(transitions) == 1
    assert transitions[0].transition == "request withdrawn"


def test_no_mechanism_request_records_only_a_debug_mechanic(caplog):
    """Gemini has no request edge: nothing transitioned, so no INFO transition
    record — a DEBUG mechanic carrying reviewer/pr is the only trace."""
    with caplog.at_level(logging.DEBUG, logger="shipit.prstate"):
        assert GeminiAdapter().request(41) is False
    assert not _transition_records(caplog)
    mechanics = [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and getattr(r, "reviewer", None) == "gemini"
    ]
    assert len(mechanics) == 1
    assert mechanics[0].pr == 41


def test_local_detach_request_is_an_info_transition_record(monkeypatch, caplog):
    from shipit.review import service

    monkeypatch.setattr(service, "start_detached_review", lambda *a, **k: True)
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        assert CodexAdapter().request(41) is True
    transitions = _transition_records(caplog)
    assert len(transitions) == 1
    rec = transitions[0]
    assert rec.reviewer == "codex-local"  # the funnel/display name
    assert rec.pr == 41


def test_local_reconcile_request_records_only_a_debug_mechanic(monkeypatch, caplog):
    """An idempotent re-request that RECONCILES against an already in-flight run
    (start_detached_review → False) detached NOTHING new: it must NOT narrate an
    INFO request transition — only a DEBUG mechanic, so the lifecycle log never
    claims a detach that didn't happen."""
    from shipit.review import service

    monkeypatch.setattr(service, "start_detached_review", lambda *a, **k: False)
    with caplog.at_level(logging.DEBUG, logger="shipit.prstate"):
        assert CodexAdapter().request(41) is True  # still reported in-flight
    assert not _transition_records(caplog)  # no INFO edge for a no-op
    mechanics = [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and getattr(r, "reviewer", None) == "codex-local"
    ]
    assert len(mechanics) == 1
    assert mechanics[0].pr == 41


def test_local_request_failure_records_error_with_exception(monkeypatch, caplog):
    """A request act that dies is a PROPAGATING failure: ERROR, with the
    exception attached (exc_info), before it normalizes to PrStateError."""
    from shipit.review import service

    def boom(*a, **k):
        raise RuntimeError("spawn exploded")

    monkeypatch.setattr(service, "start_detached_review", boom)
    with caplog.at_level(logging.ERROR, logger="shipit.prstate"):
        with pytest.raises(PrStateError):
            CodexAdapter().request(41)
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    rec = errors[0]
    assert rec.reviewer == "codex-local"
    assert rec.pr == 41
    # A real exception rides the record — value AND traceback, not just a truthy
    # exc_info. exc_info=True inside the except captures the ORIGINAL failure
    # (the RuntimeError), not the PrStateError it is later normalized to for the
    # CLI — the original cause is what a debugger wants.
    assert rec.exc_info is not None
    assert isinstance(rec.exc_info[1], RuntimeError)
    assert rec.exc_info[2] is not None


# --- the semantic gh failure the Exec record cannot carry --------------------


def test_graphql_semantic_errors_record_error_with_exception(monkeypatch, caplog):
    """The Exec succeeded (rc 0) but the GraphQL answer carries `errors`: the
    boundary records the propagating semantic failure at ERROR with the
    exception attached, then raises it."""
    payload = {"data": None, "errors": [{"message": "Could not resolve PR"}]}
    monkeypatch.setattr(gh, "_run", lambda args, **k: json.dumps(payload))
    with caplog.at_level(logging.ERROR, logger="shipit.gh"):
        with pytest.raises(PrStateError):
            gh.graphql("query {}", owner="o")
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    # Not just a truthy exc_info: a real PrStateError with a traceback rides the
    # record (raise-then-log attaches the live frame, not an unraised instance
    # whose __traceback__ is still None).
    exc_info = errors[0].exc_info
    assert exc_info is not None
    assert exc_info[0] is PrStateError
    assert isinstance(exc_info[1], PrStateError)
    assert exc_info[2] is not None
