"""The review path records each run — start, agent/backend, posting outcome.

OBS01-WS03: `review` is one of the three boundaries OBS02-04 need observable
(OBS03 makes these runs async + detached). The run is logged at DEBUG/INFO; the
human-facing dry-run output is unchanged; and the installation token minted to
post AS the bot NEVER reaches a log record.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from shipit.agent import backend as agent_backend
from shipit.review import post, service
from shipit.review.diff import ReviewView, review_view

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
        head_sha="deadbeef" * 5,  # a full 40-hex sha (COR02)
        base_ref="main",
        base_sha="cafe",
        diff=_DIFF,
        is_draft=False,
        changed_files=["foo.py"],
    )


_REVIEW = {
    "summary": {"status": "COMMENT", "overall_feedback": "looks ok"},
    "comments": [],
}


def test_dry_run_output_is_preserved_and_logged(capsys, caplog):
    """The user-facing dry-run stdout is exactly the payload JSON (+ the as-app
    note) — logging is additive plumbing under it, never on stdout."""
    with caplog.at_level(logging.DEBUG, logger="shipit.review"):
        payload = post.post_review(
            _REVIEW, _ctx(), backend=agent_backend.CODEX, dry_run=True, as_app=True
        )
    out = capsys.readouterr().out
    import json

    # stdout is byte-for-byte the pretty payload plus the single as-app line.
    expected = json.dumps(payload, indent=2) + "\n"
    expected += "(dry-run: would post as adr-codex-review[bot])\n"
    assert out == expected
    # And the run is recorded for the durable log.
    assert any("dry-run" in r.getMessage() for r in caplog.records)


def test_post_as_app_never_logs_the_token(monkeypatch, caplog):
    """A record produced over the secret-bearing (installation-token) review path
    must NOT contain the token value."""
    secret = "ghs_reviewInstallToken0987654321"
    monkeypatch.setattr(post.ghauth, "installation_token", lambda agent, repo: secret)
    captured = {}

    def _fake_rest(path, *, method, body, token):
        captured["token"] = token  # the token IS passed to gh (just never logged)
        return {"id": 1}

    monkeypatch.setattr(post.gh, "rest", _fake_rest)

    with caplog.at_level(logging.DEBUG, logger="shipit.review"):
        post.post_review(_REVIEW, _ctx(), backend=agent_backend.CODEX, as_app=True)

    assert captured["token"] == secret  # the seam still injects the real token
    full = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in full
    assert "posting review to owner/repo#5" in full


def test_parse_failure_full_raw_at_debug_snippet_at_warning(caplog):
    """#75: when an agent's output can't be parsed, the FULL raw stdout reaches the
    logger ONLY at DEBUG (the always-DEBUG OBS01 file sink) — the user-facing WARNING
    surface (console / CI handler) carries ONLY the head/tail snippet, never the full
    raw. The `BackendError` message — the PR-surface / terminal budget — likewise keeps
    only the snippet."""
    import pytest

    from shipit.review.backends import base

    # > 2*_SNIPPET so the message would snippet it; unparseable so it raises. The
    # MIDDLE marker lives only in the full raw — never in the head/tail snippet.
    raw = "A" * 500 + "MIDDLE-ONLY-IN-FULL-RAW" + "B" * 500
    with caplog.at_level(logging.DEBUG, logger="shipit.review"):
        with pytest.raises(base.BackendError) as excinfo:
            base.parse_review_output(raw, backend_name="agy")

    # The WARNING surface (console WARNING+, CI logs) carries only the snippet.
    warnings = "\n".join(
        r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    )
    assert "MIDDLE-ONLY-IN-FULL-RAW" not in warnings  # full raw never on the surface
    # The FULL raw reaches the logger only at DEBUG — captured by the durable file sink.
    debug = "\n".join(
        r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG
    )
    assert raw in debug
    # The error message (-> check-run summary / PR surface) keeps only the snippet.
    assert "MIDDLE-ONLY-IN-FULL-RAW" not in str(excinfo.value)
    # ...and the full raw is attached to the error so the service can salvage it (#76).
    assert excinfo.value.raw == raw


def test_parse_success_logs_full_raw_at_debug(caplog):
    """#75: a SUCCESSFUL parse still logs the full raw at DEBUG — the always-on audit
    trail of what the agent actually emitted, durable in the file sink."""
    from shipit.review.backends import base

    raw = '{"summary": {"status": "COMMENT", "overall_feedback": "ok"}, "comments": []}'
    with caplog.at_level(logging.DEBUG, logger="shipit.review"):
        review = base.parse_review_output(raw, backend_name="agy")

    assert review["summary"]["status"] == "COMMENT"
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert raw in logged


def test_generate_review_logs_start_and_outcome(monkeypatch, caplog):
    """`generate_review` delegates to the Tree-fetch producer and records start +
    outcome; the producer launch itself is faked so no Tree is cloned / model run."""
    monkeypatch.setattr(
        service.producer, "run_tree_review", lambda backend, ctx, **kw: dict(_REVIEW)
    )
    ctx = SimpleNamespace(diff=_DIFF, workdir="/tmp/wd", number=5, head_ref="b")
    with caplog.at_level(logging.DEBUG, logger="shipit.review"):
        service.generate_review(agent_backend.CODEX, ctx)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "agent=codex" in text
    assert "complete" in text
