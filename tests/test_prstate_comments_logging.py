"""LOG03-WS06 — reply/resolve lifecycle records (the last prstate mutations).

Convention-level tests, per the glassbox testing decision: the mutation
milestones EXIST and CARRY the required flat fields (``pr`` + the thread /
comment identifier) — identified by their fields, never by per-message string
assertions. Both verbs mutate a PR over the gh boundary; before LOG03 the
Exec debug transport line was their only record.
"""

from __future__ import annotations

import logging

import pytest

from shipit.identity import repo_from_slug
from shipit.pr import PrId
from shipit.prstate import comments

# The typed PR target (CLI01-WS02): the thread mutations take a PrId.
TARGET = PrId(repo=repo_from_slug("owner/repo"), number=558)

# --- reply -------------------------------------------------------------------


def test_reply_records_an_info_milestone_with_pr_and_comment_id(monkeypatch, caplog):
    calls: list[tuple] = []
    monkeypatch.setattr(
        comments.gh, "pr_review_reply", lambda pr, cid, body: calls.append((pr, cid))
    )
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        comments.reply(TARGET, 4242, "on it")
    assert calls == [(TARGET, 4242)]  # the mutation actually ran
    milestones = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and getattr(r, "comment_id", None) is not None
    ]
    assert len(milestones) == 1
    rec = milestones[0]
    assert rec.pr == 558
    assert rec.comment_id == 4242


def test_reply_failure_records_at_error_with_the_exception_attached(
    monkeypatch, caplog
):
    def boom(pr, cid, body):
        raise RuntimeError("transport died")

    monkeypatch.setattr(comments.gh, "pr_review_reply", boom)
    with caplog.at_level(logging.ERROR, logger="shipit.prstate"):
        with pytest.raises(RuntimeError):
            comments.reply(TARGET, 4242, "on it")
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    rec = errors[0]
    assert rec.pr == 558
    assert rec.comment_id == 4242
    # A real exception with a traceback rides the record, not just a flag.
    assert rec.exc_info is not None
    assert isinstance(rec.exc_info[1], RuntimeError)
    assert rec.exc_info[2] is not None
    # No success milestone alongside the failure record.
    assert not [r for r in caplog.records if r.levelno == logging.INFO]


# --- resolve -----------------------------------------------------------------


def test_resolve_records_an_info_milestone_with_pr_and_thread_id(monkeypatch, caplog):
    seen: list[dict] = []
    monkeypatch.setattr(comments.gh, "graphql", lambda query, **v: seen.append(v) or {})
    with caplog.at_level(logging.INFO, logger="shipit.prstate"):
        comments.resolve(TARGET, "PRRT_abc123")
    assert seen == [{"threadId": "PRRT_abc123"}]  # the mutation actually ran
    milestones = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and getattr(r, "thread_id", None) is not None
    ]
    assert len(milestones) == 1
    rec = milestones[0]
    assert rec.pr == 558
    assert rec.thread_id == "PRRT_abc123"


def test_resolve_failure_records_at_error_with_the_exception_attached(
    monkeypatch, caplog
):
    def boom(query, **variables):
        raise RuntimeError("graphql died")

    monkeypatch.setattr(comments.gh, "graphql", boom)
    with caplog.at_level(logging.ERROR, logger="shipit.prstate"):
        with pytest.raises(RuntimeError):
            comments.resolve(TARGET, "PRRT_abc123")
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    rec = errors[0]
    assert rec.pr == 558
    assert rec.thread_id == "PRRT_abc123"
    assert rec.exc_info is not None
    assert isinstance(rec.exc_info[1], RuntimeError)
    assert rec.exc_info[2] is not None
    assert not [r for r in caplog.records if r.levelno == logging.INFO]
