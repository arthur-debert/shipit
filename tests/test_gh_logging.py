"""The `gh` / git boundary logs every call and its outcome — with NO secrets.

OBS01-WS03: the single `gh` boundary is the surface OBS02-04 need observable, so
`_run` records each invocation (argv + cwd + auth mode) and its result at DEBUG.
The load-bearing constraint is that the auth token (and any secret) handled here
NEVER reaches a log record: the token travels in the child env, and the only
thing logged about it is the boolean fact that a token override is in play.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from shipit import gh


def _fake_proc(returncode=0, stdout="out", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_run_logs_call_and_success(monkeypatch, caplog):
    monkeypatch.setattr(gh.subprocess, "run", lambda *a, **k: _fake_proc(stdout="hi"))
    with caplog.at_level(logging.DEBUG, logger="shipit.gh"):
        out = gh._run(["gh", "repo", "view"])
    assert out == "hi"
    text = "\n".join(r.getMessage() for r in caplog.records)
    # The call is recorded before it runs, and its outcome after.
    assert "gh repo view" in text
    assert "ok" in text


def test_run_logs_gherror_outcome(monkeypatch, caplog):
    monkeypatch.setattr(
        gh.subprocess,
        "run",
        lambda *a, **k: _fake_proc(returncode=1, stdout="", stderr="boom"),
    )
    with caplog.at_level(logging.DEBUG, logger="shipit.gh"):
        with pytest.raises(gh.GhError):
            gh._run(["gh", "pr", "view", "9"])
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "gh pr view 9" in text
    assert "exit 1" in text


def test_run_logs_missing_binary(monkeypatch, caplog):
    def _boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(gh.subprocess, "run", _boom)
    with caplog.at_level(logging.DEBUG, logger="shipit.gh"):
        with pytest.raises(gh.GhError):
            gh._run(["gh", "whatever"])
    assert any("not found on PATH" in r.getMessage() for r in caplog.records)


def test_no_token_value_in_any_record(monkeypatch, caplog):
    """A record produced over the secret-bearing (token) path must NOT contain
    the token value — only the boolean auth fact."""
    secret = "ghs_SUPERSECRETtoken1234567890"
    monkeypatch.setattr(gh.subprocess, "run", lambda *a, **k: _fake_proc(stdout="{}"))
    with caplog.at_level(logging.DEBUG, logger="shipit.gh"):
        gh._run(["gh", "api", "/x"], token=secret)
    full = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in full
    # The fact that we authenticated as a token IS recorded (just not its value).
    assert "auth=token" in full


def test_token_shaped_argv_is_redacted(monkeypatch, caplog):
    """Defence in depth: a token-shaped argument is masked in the record even
    though tokens normally never travel in argv."""
    monkeypatch.setattr(gh.subprocess, "run", lambda *a, **k: _fake_proc(stdout="{}"))
    leaked = "ghp_argvLeak0987654321abcDEF"
    with caplog.at_level(logging.DEBUG, logger="shipit.gh"):
        gh._run(["gh", "api", "-f", f"token={leaked}"])
    full = "\n".join(r.getMessage() for r in caplog.records)
    assert leaked not in full
    assert gh._REDACTED in full


def test_stderr_token_is_redacted_on_failure(monkeypatch, caplog):
    """A token echoed back in stderr (e.g. a gh error quoting the URL) is masked
    in the failure record AND in the raised exception text — GhError messages are
    re-logged by callers, so the token must not ride the exception to a sink."""
    leaked = "ghs_stderrLeak1234567890abcd"
    monkeypatch.setattr(
        gh.subprocess,
        "run",
        lambda *a, **k: _fake_proc(returncode=1, stderr=f"bad token {leaked}"),
    )
    with caplog.at_level(logging.DEBUG, logger="shipit.gh"):
        with pytest.raises(gh.GhError) as excinfo:
            gh._run(["gh", "api", "/x"])
    full = "\n".join(r.getMessage() for r in caplog.records)
    assert leaked not in full
    # The exception message is a sink too: it must be redacted.
    assert leaked not in str(excinfo.value)


def test_token_in_argv_is_redacted_in_gherror_message(monkeypatch):
    """A token-shaped argv argument must not survive in the raised GhError text,
    which callers (e.g. review.post) re-log."""
    leaked = "ghp_argvErrLeak1234567890abcDE"
    monkeypatch.setattr(
        gh.subprocess, "run", lambda *a, **k: _fake_proc(returncode=2, stderr="nope")
    )
    with pytest.raises(gh.GhError) as excinfo:
        gh._run(["gh", "api", "-f", f"token={leaked}"])
    assert leaked not in str(excinfo.value)
    assert gh._REDACTED in str(excinfo.value)
