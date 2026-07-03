"""LOG02-WS05 (#285): the verbs/pr lifecycle actions LOG — not just print.

Before the convergence, every `pr ready` / `pr next` / `pr review request`
action was print-only: the one human hand-off signal in the whole dev cycle
(the draft→ready flip) left no durable record. These tests pin the
CONVENTION, not the prose (no per-message string assertions, per the epic):

- the flip and its `--undo` are INFO milestones carrying the ``pr`` key;
- the flip has a durable milestone at the gh-adapter boundary that performs it;
- `pr next`'s action-taken is an INFO milestone carrying the ``pr`` key;
- each review-request outcome records at its conventional level (INFO for a
  placed/in-flight request, DEBUG for a deliberate non-act, WARNING for a
  dropped request), each carrying the ``pr`` key.
"""

from __future__ import annotations

import logging

import pytest

from shipit.identity import repo_from_slug
from shipit.pr import PrId
from shipit.prstate.errors import PrStateError
from shipit.prstate.state import ChecksState, TaskState, TaskStatus
from shipit.verbs.pr import next_action as next_verb
from shipit.verbs.pr import ready as ready_verb
from shipit.verbs.pr import review as review_verb
from shipit.verbs.pr._request import RequestResult, ReviewerOutcome

# The typed PR targets (CLI01-WS02): the verbs resolve a PrId; the records carry
# its NUMBER. `repo=REPO` injects the identity half for direct `run()` calls.
REPO = repo_from_slug("owner/repo")
TARGET_42 = PrId(repo=REPO, number=42)
TARGET_7 = PrId(repo=REPO, number=7)


def _status(state: TaskState, pr: int = 42, next_action: str = "do x") -> TaskStatus:
    return TaskStatus(
        state=state,
        next_action=next_action,
        pr=pr,
        checks=ChecksState.GREEN,
        mergeable="MERGEABLE",
    )


def _pr_records(caplog, level: int):
    return [
        r
        for r in caplog.records
        if r.name == "shipit.pr" and r.levelno == level and hasattr(r, "pr")
    ]


# --- pr ready: flip / undo ----------------------------------------------------


def test_flip_is_an_info_milestone_with_the_pr_key(monkeypatch, caplog):
    monkeypatch.setattr(ready_verb, "resolve_pr", lambda pr, repo: TARGET_42)
    monkeypatch.setattr(
        ready_verb,
        "guarded_flip",
        lambda target: _status(TaskState.READY, target.number),
    )
    with caplog.at_level(logging.INFO, logger="shipit.pr"):
        assert ready_verb.run(42, repo=REPO) == 0
    milestones = _pr_records(caplog, logging.INFO)
    assert len(milestones) == 1
    assert milestones[0].pr == 42


def test_undo_is_an_info_milestone_with_the_pr_key(monkeypatch, caplog):
    monkeypatch.setattr(ready_verb, "resolve_pr", lambda pr, repo: TARGET_42)
    monkeypatch.setattr(ready_verb.gh, "pr_ready", lambda target, undo=False: None)
    with caplog.at_level(logging.INFO, logger="shipit.pr"):
        assert ready_verb.run(42, undo=True, repo=REPO) == 0
    milestones = _pr_records(caplog, logging.INFO)
    assert len(milestones) == 1
    assert milestones[0].pr == 42


def test_refused_flip_is_a_warning_with_the_pr_key(monkeypatch, caplog):
    def refuse(target):
        raise ready_verb.NotReady(_status(TaskState.VALIDATING, target.number))

    monkeypatch.setattr(ready_verb, "resolve_pr", lambda pr, repo: TARGET_42)
    monkeypatch.setattr(ready_verb, "guarded_flip", refuse)
    with caplog.at_level(logging.INFO, logger="shipit.pr"):
        assert ready_verb.run(42, repo=REPO) == 1
    assert not _pr_records(caplog, logging.INFO)  # nothing flipped, no milestone
    warnings = _pr_records(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert warnings[0].pr == 42


def test_gh_adapter_flip_leaves_a_durable_milestone(monkeypatch, caplog):
    """The boundary that PERFORMS the flip records it (before #285 its only
    record was the Exec runner's DEBUG line)."""
    from shipit import gh

    monkeypatch.setattr(gh, "_run", lambda args, **k: "")
    with caplog.at_level(logging.INFO, logger="shipit.gh"):
        gh.pr_ready(TARGET_7)
    milestones = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and getattr(r, "pr", None) == 7
    ]
    assert len(milestones) == 1
    assert milestones[0].repo == "owner/repo"


# --- pr next: the action-taken milestone ---------------------------------------


def test_next_action_taken_is_an_info_milestone_with_the_pr_key(monkeypatch, caplog):
    monkeypatch.setattr(next_verb, "resolve_pr", lambda pr, repo: TARGET_42)
    monkeypatch.setattr(next_verb, "gather", lambda target: target)
    monkeypatch.setattr(next_verb, "required_reviewers", lambda: [])
    monkeypatch.setattr(
        next_verb,
        "evaluate",
        lambda ctx, required: _status(TaskState.BLOCKED, ctx.number, "the blocker"),
    )
    with caplog.at_level(logging.INFO, logger="shipit.pr"):
        assert next_verb.run(42, repo=REPO) == 0
    milestones = _pr_records(caplog, logging.INFO)
    assert len(milestones) == 1
    assert milestones[0].pr == 42


def test_next_action_failure_is_an_error_with_the_pr_key(monkeypatch, caplog):
    """A gh/state failure AFTER the PR resolved still records the ``pr`` key, so
    the durable ERROR stays jq-sliceable by PR."""
    monkeypatch.setattr(next_verb, "resolve_pr", lambda pr, repo: TARGET_42)
    monkeypatch.setattr(next_verb, "required_reviewers", lambda: [])

    def boom(target):
        raise PrStateError("gh blew up")

    monkeypatch.setattr(next_verb, "gather", boom)
    with caplog.at_level(logging.ERROR, logger="shipit.pr"):
        assert next_verb.run(42, repo=REPO) == 1
    errors = _pr_records(caplog, logging.ERROR)
    assert len(errors) == 1
    assert errors[0].pr == 42


# --- pr review request: per-reviewer outcomes ----------------------------------


@pytest.fixture
def _request_run(monkeypatch):
    """Run the verb against an injected RequestResult; returns (run, caplog use)."""

    def runner(result: RequestResult) -> int:
        monkeypatch.setattr(review_verb, "resolve_pr", lambda pr, repo: TARGET_7)
        monkeypatch.setattr(
            review_verb, "request_reviewers", lambda pr, adapters, force: result
        )
        monkeypatch.setattr(review_verb, "required_reviewers", lambda: [object()])
        return review_verb.run(7, repo=REPO)

    return runner


def test_verified_and_in_flight_outcomes_are_info_records(_request_run, caplog):
    result = RequestResult(
        outcomes=[
            ReviewerOutcome("copilot", "verified"),
            ReviewerOutcome("codex", "in_flight"),
        ]
    )
    with caplog.at_level(logging.DEBUG, logger="shipit.pr"):
        assert _request_run(result) == 0
    infos = _pr_records(caplog, logging.INFO)
    assert {r.reviewer for r in infos} == {"copilot", "codex"}
    assert all(r.pr == 7 for r in infos)


def test_skip_and_no_op_outcomes_are_debug_mechanics(_request_run, caplog):
    result = RequestResult(
        outcomes=[
            ReviewerOutcome("copilot", "skipped"),
            ReviewerOutcome("gemini", "no_op"),
        ]
    )
    with caplog.at_level(logging.DEBUG, logger="shipit.pr"):
        assert _request_run(result) == 0
    assert not _pr_records(caplog, logging.INFO)  # nothing transitioned
    mechanics = _pr_records(caplog, logging.DEBUG)
    assert {r.reviewer for r in mechanics} == {"copilot", "gemini"}


def test_dropped_outcome_is_a_warning_record(_request_run, caplog):
    result = RequestResult(outcomes=[ReviewerOutcome("copilot", "dropped")])
    with caplog.at_level(logging.DEBUG, logger="shipit.pr"):
        assert _request_run(result) == 1
    warnings = _pr_records(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert warnings[0].reviewer == "copilot"
    assert warnings[0].pr == 7


def test_review_request_failure_is_an_error_with_the_pr_key(monkeypatch, caplog):
    """A gh/auth failure (or the local-agent guard) AFTER the PR resolved still
    records the ``pr`` key on its durable ERROR."""
    monkeypatch.setattr(review_verb, "resolve_pr", lambda pr, repo: TARGET_7)
    monkeypatch.setattr(review_verb, "required_reviewers", lambda: [object()])

    def boom(pr, adapters, force):
        raise PrStateError("gh blew up")

    monkeypatch.setattr(review_verb, "request_reviewers", boom)
    with caplog.at_level(logging.ERROR, logger="shipit.pr"):
        assert review_verb.run(7, repo=REPO) == 1
    errors = _pr_records(caplog, logging.ERROR)
    assert len(errors) == 1
    assert errors[0].pr == 7
