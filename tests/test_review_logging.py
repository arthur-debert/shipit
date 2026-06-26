"""The review path records each run — start, agent/backend, posting outcome.

OBS01-WS03: `review` is one of the three boundaries OBS02-04 need observable
(OBS03 makes these runs async + detached). The run is logged at DEBUG/INFO; the
human-facing dry-run output is unchanged; and the installation token minted to
post AS the bot NEVER reaches a log record.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from shipit.review import post, service
from shipit.review.diff import PRContext

_DIFF = """\
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1,2 +1,3 @@
 import os
+x = 1
 y = 2
"""


def _ctx() -> PRContext:
    return PRContext(
        number=5,
        repo="owner/repo",
        head_sha="deadbeef",
        base_ref="main",
        base_sha="cafe",
        diff=_DIFF,
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
            _REVIEW, _ctx(), agent_name="codex", dry_run=True, as_app=True
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
        post.post_review(_REVIEW, _ctx(), agent_name="codex", as_app=True)

    assert captured["token"] == secret  # the seam still injects the real token
    full = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in full
    assert "posting review to owner/repo#5" in full


def test_generate_review_logs_start_and_outcome(monkeypatch, caplog):
    class _FakeBackend:
        def preflight(self):
            pass

        def run(self, prompt, schema, *, cwd):
            return _REVIEW

    monkeypatch.setattr(service, "get_backend", lambda agent, model: _FakeBackend())
    ctx = SimpleNamespace(diff=_DIFF, workdir="/tmp/wd")
    with caplog.at_level(logging.DEBUG, logger="shipit.review"):
        service.generate_review("codex", ctx)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "agent=codex" in text
    assert "complete" in text
