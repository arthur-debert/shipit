"""The `gh` / git boundary executes through the Exec runner — with NO secrets.

PROC01-WS02 (ADR-0028): `shipit.gh` routes every subprocess call through the
one Exec runner (`shipit.execrun`), so its record + redaction guarantees are
the runner's — one structured record per Exec on the `shipit.exec` logger, a
stated timeout on every call, argv/streams passed through the central redactor
(`shipit.redact`), and the single transport error `ExecError` carrying a
pre-redacted message. The load-bearing constraint is unchanged from the old
boundary-local logging: the auth token (and any secret) handled here NEVER
reaches a log record or an exception text — the token travels in the child
env, which is never logged.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from shipit import execrun, gh, git, redact
from shipit.execrun import ExecError


def _fake_proc(returncode=0, stdout="out", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_run_routes_through_the_runner_with_a_stated_timeout(monkeypatch, caplog):
    seen = {}

    def fake(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return _fake_proc(stdout="hi")

    monkeypatch.setattr(execrun.subprocess, "run", fake)
    with caplog.at_level(logging.DEBUG, logger="shipit.exec"):
        out = gh._run(["gh", "repo", "view"])
    assert out == "hi"
    # Every gh Exec carries a stated timeout (ADR-0028: nothing hangs by default).
    assert seen["kwargs"]["timeout"] == gh._NETWORK_TIMEOUT
    # The one record per Exec comes from the runner, not from gh.py.
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "gh repo view" in text
    assert "rc=0" in text


def test_git_helper_states_a_local_timeout(monkeypatch):
    seen = {}

    def fake(argv, **kwargs):
        seen["kwargs"] = kwargs
        return _fake_proc(stdout="")

    monkeypatch.setattr(execrun.subprocess, "run", fake)
    git._git(["status", "--porcelain"], cwd="/x")
    assert seen["kwargs"]["timeout"] == git._LOCAL_TIMEOUT


def test_run_failure_raises_execerror(monkeypatch, caplog):
    monkeypatch.setattr(
        execrun.subprocess,
        "run",
        lambda *a, **k: _fake_proc(returncode=1, stdout="", stderr="boom"),
    )
    with caplog.at_level(logging.DEBUG, logger="shipit.exec"):
        with pytest.raises(ExecError) as excinfo:
            gh._run(["gh", "pr", "view", "9"])
    assert excinfo.value.cause == execrun.CAUSE_EXIT
    assert excinfo.value.rc == 1
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "gh pr view 9" in text


def test_missing_binary_normalizes_to_execerror(monkeypatch):
    def _boom(*a, **k):
        raise FileNotFoundError

    monkeypatch.setattr(execrun.subprocess, "run", _boom)
    with pytest.raises(ExecError) as excinfo:
        gh._run(["gh", "whatever"])
    assert excinfo.value.cause == execrun.CAUSE_MISSING_BINARY


def test_token_travels_in_env_never_argv_and_github_token_is_removed(monkeypatch):
    """The token rides ``GH_TOKEN`` in the child env, with any inherited
    ``GITHUB_TOKEN`` REMOVED (not blanked) — the call authenticates as exactly
    the passed token, and the token never appears in argv."""
    secret = "ghs_SUPERSECRETtoken1234567890"
    seen = {}

    def fake(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs.get("env")
        return _fake_proc(stdout="{}")

    monkeypatch.setattr(execrun.subprocess, "run", fake)
    monkeypatch.setenv("GITHUB_TOKEN", "inherited-should-be-dropped")
    gh._run(["gh", "api", "/x"], token=secret)
    assert seen["env"]["GH_TOKEN"] == secret
    assert "GITHUB_TOKEN" not in seen["env"]
    assert all(secret not in arg for arg in seen["argv"])


def test_no_token_value_in_any_record(monkeypatch, caplog):
    """A record produced over the secret-bearing (token) path must NOT contain
    the token value: the child env is never logged, and a token-shaped string
    would be masked by the central redactor anyway."""
    secret = "ghs_SUPERSECRETtoken1234567890"
    monkeypatch.setattr(
        execrun.subprocess, "run", lambda *a, **k: _fake_proc(stdout="{}")
    )
    with caplog.at_level(logging.DEBUG, logger="shipit"):
        gh._run(["gh", "api", "/x"], token=secret)
    full = "\n".join(r.getMessage() for r in caplog.records)
    assert secret not in full


def test_token_shaped_argv_is_redacted_in_the_record(monkeypatch, caplog):
    """Defence in depth: a token-shaped argument is masked in the Exec record
    (the central redactor's GitHub-token pattern rule) even though tokens
    normally never travel in argv.

    Asserted on POST-format output (#277): the runner's records carry no
    per-site masking — the central ``redact_event`` processor masks at format
    time, so redaction is asserted on what a sink actually writes, rendered
    through the same ``ProcessorFormatter`` every sink shares (``caplog``
    captures records pre-format)."""
    from shipit import logsetup

    monkeypatch.setattr(
        execrun.subprocess, "run", lambda *a, **k: _fake_proc(stdout="{}")
    )
    leaked = "ghp_argvLeak0987654321abcDEF"
    with caplog.at_level(logging.DEBUG, logger="shipit.exec"):
        gh._run(["gh", "api", "-f", f"token={leaked}"])
    formatter = logsetup._file_formatter()
    full = "\n".join(formatter.format(r) for r in caplog.records)
    assert leaked not in full
    assert redact.MASK in full


def test_stderr_token_is_redacted_on_failure(monkeypatch, caplog):
    """A token echoed back in stderr (e.g. a gh error quoting the URL) is masked
    in the failure record AND in the raised exception text — ExecError messages
    are re-logged by callers, so the token must not ride the exception to a
    sink either."""
    leaked = "ghs_stderrLeak1234567890abcd"
    monkeypatch.setattr(
        execrun.subprocess,
        "run",
        lambda *a, **k: _fake_proc(returncode=1, stderr=f"bad token {leaked}"),
    )
    with caplog.at_level(logging.DEBUG, logger="shipit"):
        with pytest.raises(ExecError) as excinfo:
            gh._run(["gh", "api", "/x"])
    full = "\n".join(r.getMessage() for r in caplog.records)
    assert leaked not in full
    # The exception message is a sink too: it must be redacted.
    assert leaked not in str(excinfo.value)


def test_token_in_argv_is_redacted_in_execerror_message(monkeypatch):
    """A token-shaped argv argument must not survive in the raised ExecError
    text, which callers (e.g. review.post) re-log."""
    leaked = "ghp_argvErrLeak1234567890abcDE"
    monkeypatch.setattr(
        execrun.subprocess,
        "run",
        lambda *a, **k: _fake_proc(returncode=2, stderr="nope"),
    )
    with pytest.raises(ExecError) as excinfo:
        gh._run(["gh", "api", "-f", f"token={leaked}"])
    assert leaked not in str(excinfo.value)
    assert redact.MASK in str(excinfo.value)
