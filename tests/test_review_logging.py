from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from shipit.agent import backend as agent_backend
from shipit.identity import repo_from_slug
from shipit.pr import PrId
from shipit.review import post, service
from shipit.review.diff import ReviewView, review_view

TARGET = PrId(repo=repo_from_slug("owner/repo"), number=5)

_DIFF = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,2 +1,3 @@
 import os
+x = 1
 y = 2
"""


def _ctx() -> ReviewView:
    return review_view(
        number=5,
        repo="owner/repo",
        head_sha="deadbeef" * 5,
        base_ref="main",
        base_sha="cafe" * 10,
        diff=_DIFF,
        is_draft=False,
        changed_files=["foo.py"],
    )


_REVIEW = {
    "summary": {"status": "COMMENT", "overall_feedback": "looks ok"},
    "comments": [],
}


def test_dry_run_output_is_preserved_and_logged(capsys, caplog):
    with caplog.at_level(logging.DEBUG, logger="shipit.review"):
        payload = post.post_review(
            _REVIEW, _ctx(), backend=agent_backend.CODEX, dry_run=True, as_app=True
        )
    out = capsys.readouterr().out
    import json

    expected = json.dumps(payload, indent=2) + "\n"
    expected += "(dry-run: would post as adr-codex-review[bot])\n"
    assert out == expected
    assert any("dry-run" in r.getMessage() for r in caplog.records)


def test_post_as_app_never_logs_the_token(monkeypatch, caplog):
    secret = "ghs_reviewInstallToken0987654321"
    monkeypatch.setattr(post.ghauth, "installation_token", lambda agent, repo: secret)
    captured = {}

    def _fake_rest(path, *, method, body, token):
        captured["token"] = token
        return {"id": 1}

    monkeypatch.setattr(post.gh, "rest", _fake_rest)

    with caplog.at_level(logging.DEBUG, logger="shipit.review"):
        post.post_review(_REVIEW, _ctx(), backend=agent_backend.CODEX, as_app=True)

    assert captured["token"] == secret
    full = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in full
    assert "posting to pr#5" in full


def test_parse_failure_full_raw_at_debug_snippet_at_warning(caplog):

    from shipit.review.backends import base

    raw = "A" * 500 + "MIDDLE-ONLY-IN-FULL-RAW" + "B" * 500
    with caplog.at_level(logging.DEBUG, logger="shipit.review"):
        with pytest.raises(base.BackendError) as excinfo:
            base.parse_review_output(raw, backend_name="agy")

    warnings = "\n".join(
        r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    )
    assert "MIDDLE-ONLY-IN-FULL-RAW" not in warnings
    debug = "\n".join(
        r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG
    )
    assert raw in debug
    assert "MIDDLE-ONLY-IN-FULL-RAW" not in str(excinfo.value)
    assert excinfo.value.raw == raw


def test_parse_success_logs_full_raw_at_debug(caplog):
    from shipit.review.backends import base

    raw = '{"summary": {"status": "COMMENT", "overall_feedback": "ok"}, "comments": []}'
    with caplog.at_level(logging.DEBUG, logger="shipit.review"):
        review = base.parse_review_output(raw, backend_name="agy")

    assert review["summary"]["status"] == "COMMENT"
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert raw in logged


def test_generate_review_logs_start_and_outcome(monkeypatch, caplog):
    monkeypatch.setattr(
        service.fanout,
        "run_fanout_review",
        lambda backend, ctx, **kw: service.fanout.FanoutOutcome(
            review=dict(_REVIEW), findings=(), runs=()
        ),
    )
    ctx = SimpleNamespace(diff=_DIFF, workdir="/tmp/wd", number=5, head_ref="b")
    with caplog.at_level(logging.DEBUG, logger="shipit.review"):
        service.generate_review(agent_backend.CODEX, ctx)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "agent=codex" in text
    assert "complete" in text


def _duration_records(caplog, level):
    return [
        r
        for r in caplog.records
        if r.levelno == level and getattr(r, "duration_ms", None) is not None
    ]


def test_generate_review_outcome_carries_duration_fields(monkeypatch, caplog):
    monkeypatch.setattr(
        service.fanout,
        "run_fanout_review",
        lambda backend, ctx, **kw: service.fanout.FanoutOutcome(
            review=dict(_REVIEW), findings=(), runs=()
        ),
    )
    ctx = SimpleNamespace(diff=_DIFF, workdir="/tmp/wd", number=5, head_ref="b")
    with caplog.at_level(logging.INFO, logger="shipit.review"):
        service.generate_review(agent_backend.CODEX, ctx)
    timed = _duration_records(caplog, logging.INFO)
    assert len(timed) == 1
    rec = timed[0]
    assert rec.reviewer == "codex"
    assert rec.pr == 5
    assert rec.duration_ms >= 0


def test_detached_child_settle_carries_start_to_settle_duration(monkeypatch, caplog):
    monkeypatch.setattr(
        service,
        "resolve_pr",
        lambda pr, repo=None: SimpleNamespace(changed_files=["foo.py"], diff=_DIFF),
    )
    monkeypatch.setattr(
        service, "_generate_post_and_close", lambda *a, **kw: {"post": {"id": 1}}
    )
    with caplog.at_level(logging.INFO, logger="shipit.review"):
        service.run_detached_review(agent_backend.CODEX, TARGET, run_id=9)
    settles = _duration_records(caplog, logging.INFO)
    assert len(settles) == 1
    rec = settles[0]
    assert rec.reviewer == "codex"
    assert rec.pr == 5
    assert rec.duration_ms >= 0


def test_detached_child_failure_settles_at_error_with_exception_and_duration(
    monkeypatch, caplog
):

    def boom(*a, **kw):
        raise RuntimeError("backend crashed")

    monkeypatch.setattr(
        service,
        "resolve_pr",
        lambda pr, repo=None: SimpleNamespace(changed_files=["foo.py"], diff=_DIFF),
    )
    monkeypatch.setattr(service, "_generate_post_and_close", boom)

    with caplog.at_level(logging.INFO, logger="shipit.review"):
        with pytest.raises(RuntimeError):
            service.run_detached_review(agent_backend.CODEX, TARGET, run_id=9)
    errors = _duration_records(caplog, logging.ERROR)
    assert len(errors) == 1
    rec = errors[0]
    assert rec.reviewer == "codex"
    assert rec.pr == 5
    assert rec.exc_info is not None


def test_resolve_failure_settles_at_error_with_exception_and_duration(
    monkeypatch, caplog
):

    def boom_resolve(pr, repo=None):
        raise RuntimeError("could not fetch PR")

    monkeypatch.setattr(service, "resolve_pr", boom_resolve)

    with caplog.at_level(logging.INFO, logger="shipit.review"):
        with pytest.raises(RuntimeError):
            service.run_detached_review(agent_backend.CODEX, TARGET, run_id=None)
    errors = _duration_records(caplog, logging.ERROR)
    assert len(errors) == 1
    assert errors[0].exc_info is not None


def test_post_failure_records_error_with_exception(monkeypatch, caplog):
    from shipit import execrun

    def boom_rest(path, *, method=None, body=None, token=None):
        raise execrun.ExecError(["gh", "api"], rc=1, stderr="422 sad")

    monkeypatch.setattr(post.gh, "rest", boom_rest)

    with caplog.at_level(logging.INFO, logger="shipit.review"):
        with pytest.raises(RuntimeError):
            post.post_review(_REVIEW, _ctx(), backend=agent_backend.CODEX, as_app=False)
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].pr == 5
    assert errors[0].exc_info is not None
