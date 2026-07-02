"""Unit tests for secret-source resolution — the optional-skip rule, and the
``doppler`` boundary speaking the Exec runner's result/error contract."""

import pytest

from shipit import execrun, secretsrc
from shipit.config import SecretSource


def test_resolve_doppler_injected():
    src = SecretSource("A", "doppler", "KEY", False)
    val = secretsrc.resolve(src, doppler_get=lambda k: f"val:{k}")
    assert val == "val:KEY"


def test_resolve_env():
    src = SecretSource("A", "env", "VAR", False)
    assert secretsrc.resolve(src, env={"VAR": "secret"}) == "secret"


def test_resolve_prompt():
    src = SecretSource("A", "prompt", None, False)
    assert secretsrc.resolve(src, prompt=lambda name: "typed") == "typed"


def test_required_env_missing_raises():
    src = SecretSource("A", "env", "VAR", False)
    with pytest.raises(secretsrc.SecretSourceError, match="VAR not set"):
        secretsrc.resolve(src, env={})


def test_optional_env_missing_skips():
    src = SecretSource("A", "env", "VAR", True)
    assert secretsrc.resolve(src, env={}) is None


def test_optional_doppler_failure_skips():
    def boom(_key):
        raise secretsrc.SecretSourceError("doppler down")

    src = SecretSource("A", "doppler", "KEY", True)
    assert secretsrc.resolve(src, doppler_get=boom) is None


def test_required_doppler_failure_raises():
    def boom(_key):
        raise secretsrc.SecretSourceError("doppler down")

    src = SecretSource("A", "doppler", "KEY", False)
    with pytest.raises(secretsrc.SecretSourceError):
        secretsrc.resolve(src, doppler_get=boom)


def test_prompt_without_prompt_fn_raises():
    src = SecretSource("A", "prompt", None, False)
    with pytest.raises(secretsrc.SecretSourceError, match="interactive prompt"):
        secretsrc.resolve(src, prompt=None)


# --------------------------------------------------------------------------
# The real doppler boundary — through the Exec runner (faked here)
# --------------------------------------------------------------------------


def _doppler_result(rc: int, stdout: str = "", stderr: str = "") -> execrun.ExecResult:
    return execrun.ExecResult(
        argv=("doppler",), rc=rc, stdout=stdout, stderr=stderr, duration_ms=1
    )


def test_doppler_get_runs_the_canonical_argv_check_false(monkeypatch):
    # check=False is load-bearing twice over: a nonzero rc is this layer's
    # SEMANTIC failure, and a completed run's Exec record then carries argv only
    # — never the secret riding stdout.
    captured = {}

    def fake_run(argv, *, check=True, **kw):
        captured["argv"] = argv
        captured["check"] = check
        return _doppler_result(0, stdout="s3cret\n")

    monkeypatch.setattr(secretsrc.execrun, "run", fake_run)
    assert secretsrc.doppler_get("GH_PAT") == "s3cret"
    assert captured["check"] is False
    assert captured["argv"] == [
        "doppler",
        "secrets",
        "get",
        "GH_PAT",
        "--plain",
        "--project",
        "github",
        "--config",
        "prd",
    ]


def test_doppler_get_nonzero_rc_raises_semantic_error(monkeypatch):
    monkeypatch.setattr(
        secretsrc.execrun,
        "run",
        lambda argv, **kw: _doppler_result(1, stderr="no such secret\n"),
    )
    with pytest.raises(
        secretsrc.SecretSourceError, match="doppler get KEY failed: no such secret"
    ):
        secretsrc.doppler_get("KEY")


def test_doppler_get_missing_binary_raises_semantic_error(monkeypatch):
    def boom(argv, **kw):
        raise execrun.ExecError(argv, rc=None, cause=execrun.CAUSE_MISSING_BINARY)

    monkeypatch.setattr(secretsrc.execrun, "run", boom)
    with pytest.raises(secretsrc.SecretSourceError, match="doppler not found on PATH"):
        secretsrc.doppler_get("KEY")


def test_doppler_get_other_transport_failure_raises_semantic_error(monkeypatch):
    # A timeout (or any other launch-level failure) also lands as the semantic
    # error — no raw ExecError escapes the secrets layer.
    def boom(argv, **kw):
        raise execrun.ExecError(argv, rc=None, cause=execrun.CAUSE_TIMEOUT)

    monkeypatch.setattr(secretsrc.execrun, "run", boom)
    with pytest.raises(secretsrc.SecretSourceError, match="doppler get KEY failed"):
        secretsrc.doppler_get("KEY")
