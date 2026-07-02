"""Tests for :mod:`shipit.execrun` — the one Exec seam (ADR-0028).

The contract under test, via an injected fake process (monkeypatched
``subprocess.run``) plus a handful of real, fast children:

- success/failure: an :class:`~shipit.execrun.ExecResult` carrying
  rc/stdout/stderr/duration, or the single transport error
  :class:`~shipit.execrun.ExecError` (argv, rc, both streams, duration, cause);
- timeout: the 5-minute default is enforced, per-call override and ``None``
  honored, expiry raises ``ExecError`` with a timeout cause and partial output;
- missing binary / OS launch failures normalize into ``ExecError`` — no raw
  ``OSError``/``FileNotFoundError`` escapes;
- chained causes (#317): the raw exception a failure wraps stays reachable via
  ``__cause__``, so it is sanitized of its stream payloads before chaining —
  the chain (type, message) survives, the raw streams do not;
- record emission: exactly one record per Exec (success DEBUG, failure ERROR
  with both stream tails), redacted at format time by the central
  ``redact_event`` processor (#277 — no per-site masking);
- the stdin contract (ADR-0020, carried over from the retired proto-runner):
  no ``input`` → the child's stdin pinned to ``DEVNULL``;
- :func:`~shipit.execrun.spawn_detached`, the one deliberate non-Exec: detach
  semantics (own session, stdio to ``/dev/null``, no handle), the spawn-time
  record (argv/cwd/pid, redacted at format time), and launch normalization into
  ``ExecError``;
- structured fields (#310, glassbox PRD story 14): every record carries the
  ``_record_fields`` vocabulary (``argv``/``cwd``/``rc``/``duration_ms``, on
  failure ``cause`` + both stream tails, on a detached spawn ``pid``) as FLAT
  JSONL keys — asserted by parsing what the real file sink writes, never the
  record object — so durations and outcomes are a query, not a msg parse.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys

import pytest

from shipit import execrun, redact


def _fake_completed(rc: int = 0, stdout: str = "", stderr: str = ""):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr=stderr)

    return fake_run


def _capture_kwargs(captured: dict):
    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    return fake_run


# ---------------------------------------------------------------------------
# Result contract
# ---------------------------------------------------------------------------


def test_success_returns_result_with_rc_streams_and_duration(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", _fake_completed(rc=0, stdout="out", stderr="err")
    )
    result = execrun.run(["tool", "arg"])
    assert result.argv == ("tool", "arg")
    assert result.rc == 0
    assert result.ok
    assert result.stdout == "out"
    assert result.stderr == "err"
    assert result.duration_ms >= 0


def test_nonzero_with_check_raises_execerror_with_full_contract(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", _fake_completed(rc=3, stdout="partial out", stderr="boom")
    )
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run(["tool", "arg"])
    err = excinfo.value
    assert err.argv == ("tool", "arg")
    assert err.rc == 3
    assert err.stdout == "partial out"
    assert err.stderr == "boom"
    assert err.duration_ms >= 0
    assert err.cause == execrun.CAUSE_EXIT
    assert "boom" in str(err)


def test_nonzero_with_check_false_returns_result(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_completed(rc=1, stderr="dead"))
    result = execrun.run(["ps", "-p", "999999"], check=False)
    assert result.rc == 1
    assert not result.ok


# ---------------------------------------------------------------------------
# Missing binary / OS normalization
# ---------------------------------------------------------------------------


def test_missing_binary_normalizes_into_execerror(monkeypatch):
    def fake_run(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run(["no-such-binary"])
    err = excinfo.value
    assert err.cause == execrun.CAUSE_MISSING_BINARY
    assert err.rc is None
    # The OS exception rides as the chained cause, never as the raised type.
    assert isinstance(err.__cause__, FileNotFoundError)


def test_other_oserror_normalizes_into_execerror(monkeypatch):
    def fake_run(argv, **kwargs):
        raise PermissionError(13, "Permission denied", argv[0])

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run(["locked-down"])
    assert excinfo.value.cause == execrun.CAUSE_OS
    assert excinfo.value.rc is None


def test_missing_cwd_normalizes_to_os_error_not_missing_binary(monkeypatch):
    # A missing cwd raises FileNotFoundError too, but names the DIRECTORY, not
    # argv[0]: it must classify as an OS error, not masquerade as a missing
    # binary (which would mislead callers branching on the cause).
    def fake_run(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", kwargs["cwd"])

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run(["tool"], cwd="/no/such/dir")
    assert excinfo.value.cause == execrun.CAUSE_OS


def test_missing_cwd_real_child():
    # End-to-end: a genuinely absent cwd surfaces as the transport error with
    # the OS cause, not the missing-binary cause.
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run(["true"], cwd="/shipit/no/such/dir/xyzzy")
    assert excinfo.value.cause == execrun.CAUSE_OS


def test_missing_binary_real_child():
    # End-to-end: a genuinely absent binary raises the transport error, not a
    # raw FileNotFoundError.
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run(["shipit-no-such-binary-xyzzy"])
    assert excinfo.value.cause == execrun.CAUSE_MISSING_BINARY


def test_undecodable_output_replaced_not_raised():
    # End-to-end: a real child that writes bytes undecodable in the runner's
    # encoding must still yield an ExecResult (with the bad bytes replaced), not
    # let a raw UnicodeDecodeError escape and bypass the one-error contract.
    result = execrun.run(
        [sys.executable, "-c", r'import sys; sys.stdout.buffer.write(b"\xff\xfe ok")']
    )
    assert result.ok
    assert "ok" in result.stdout
    assert "�" in result.stdout  # the U+FFFD replacement char


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


def test_default_timeout_is_five_minutes(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _capture_kwargs(captured))
    execrun.run(["tool"])
    assert captured["timeout"] == execrun.DEFAULT_TIMEOUT == 300.0


def test_timeout_override_and_none_are_honored(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _capture_kwargs(captured))
    execrun.run(["tool"], timeout=1800.0)
    assert captured["timeout"] == 1800.0
    execrun.run(["tool"], timeout=None)
    assert captured["timeout"] is None


def test_timeout_expiry_raises_execerror_with_partial_output(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(
            argv, 0.1, output="partial stdout", stderr="partial stderr"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run(["slow-tool"], timeout=0.1)
    err = excinfo.value
    assert err.cause == execrun.CAUSE_TIMEOUT
    assert err.rc is None
    assert err.stdout == "partial stdout"
    assert err.stderr == "partial stderr"


def test_timeout_partial_bytes_output_normalized(monkeypatch):
    # subprocess attaches partial streams as BYTES on some paths even in text
    # mode; the runner must normalize rather than crash on the type.
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 0.1, output=b"partial", stderr=None)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run(["slow-tool"], timeout=0.1)
    assert excinfo.value.stdout == "partial"
    assert excinfo.value.stderr == ""


def test_timeout_real_child_is_killed():
    # End-to-end: a real hanging child dies at the timeout and surfaces as the
    # transport error with the timeout cause — nothing hangs by default.
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.2)
    assert excinfo.value.cause == execrun.CAUSE_TIMEOUT


# ---------------------------------------------------------------------------
# Chained causes — __cause__ carries no raw stream payloads (#317)
# ---------------------------------------------------------------------------


def test_timeout_cause_is_sanitized_of_stream_payloads(monkeypatch):
    # `raise ExecError from exc` keeps the raw TimeoutExpired reachable via
    # __cause__ — its .stdout/.stderr held the unredacted partial streams. The
    # runner must null them before chaining, while keeping the chain itself
    # (type + message) for diagnostics.
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(
            argv, 0.1, output="raw partial stdout", stderr="raw partial stderr"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run(["slow-tool"], timeout=0.1)
    err = excinfo.value
    # The wrapper still hands the caller the (redacted) streams...
    assert err.stdout == "raw partial stdout"
    # ...but the chained raw exception has been stripped of them.
    cause = err.__cause__
    assert isinstance(cause, subprocess.TimeoutExpired)
    assert cause.output is None
    assert cause.stdout is None  # the property reads .output
    assert cause.stderr is None
    # The chain's diagnostic value survives: type and message intact.
    assert "timed out" in str(cause)
    assert "raw partial" not in repr(vars(cause))


def test_timeout_cause_carries_no_secret_with_secret_stdout(monkeypatch):
    # The sharp case: secret_stdout=True suppresses the wrapper's stdout, but
    # before #317 the cause kept a back-door copy of the partial secret.
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(
            argv, 0.1, output="s3cret-partial", stderr="doppler: deadline"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run(["doppler", "get"], timeout=0.1, secret_stdout=True)
    err = excinfo.value
    assert err.stdout == execrun.SECRET_STDOUT_PLACEHOLDER
    cause = err.__cause__
    assert cause.output is None
    assert cause.stdout is None
    assert cause.stderr is None
    assert "s3cret-partial" not in repr(vars(cause))
    assert "s3cret-partial" not in str(cause)


def test_timeout_cause_cmd_is_redacted(monkeypatch, _clean_registry):
    # TimeoutExpired.__str__ names the command; a registered secret riding argv
    # must be masked on the cause exactly as ExecError masks its own argv.
    redact.register_secret("s3cret-value")

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 0.1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run(["tool", "--token", "s3cret-value"], timeout=0.1)
    cause = excinfo.value.__cause__
    assert "s3cret-value" not in " ".join(cause.cmd)
    assert "s3cret-value" not in str(cause)


def test_timeout_cause_args_tuple_is_sanitized(monkeypatch, _clean_registry):
    # BaseException.__new__ snapshots the positional constructor arguments onto
    # .args, and repr(exc) renders THAT tuple — rewriting .cmd/.output/.stderr
    # alone leaves the raw values reachable via repr(cause) / cause.args. Pass
    # the streams POSITIONALLY (the worst constructor shape) to pin that .args
    # is rebuilt from the sanitized values.
    redact.register_secret("s3cret-value")

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 0.1, "raw-stdout-payload", "raw-stderr")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run(["tool", "--token", "s3cret-value"], timeout=0.1)
    cause = excinfo.value.__cause__
    assert cause.args == (cause.cmd, 0.1, None, None)
    for leak in ("s3cret-value", "raw-stdout-payload", "raw-stderr"):
        assert leak not in repr(cause)
        assert leak not in repr(cause.args)


def test_timeout_cause_string_cmd_survives_sanitization(_clean_registry):
    # A TimeoutExpired built with a STRING cmd (shell=True upstream, or any
    # caller outside run()'s list-argv enforcement) must not be exploded into a
    # list of single characters by the per-arg redaction — the string is
    # redacted whole and keeps its diagnostic value.
    redact.register_secret("s3cret-value")
    exc = subprocess.TimeoutExpired("tool --token s3cret-value", 0.1, output="raw")
    sanitized = execrun._sanitize_cause(exc)
    assert isinstance(sanitized.cmd, str)
    assert sanitized.cmd.startswith("tool --token ")
    assert "s3cret-value" not in sanitized.cmd
    assert "s3cret-value" not in repr(sanitized)
    assert sanitized.output is None


def test_timeout_real_child_cause_is_sanitized():
    # End-to-end: a real killed child's chained TimeoutExpired carries no
    # stream payloads either.
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run(
            [sys.executable, "-c", "import time; print('partial'); time.sleep(30)"],
            timeout=0.2,
        )
    cause = excinfo.value.__cause__
    assert isinstance(cause, subprocess.TimeoutExpired)
    assert cause.output is None
    assert cause.stdout is None
    assert cause.stderr is None


def test_os_error_causes_carry_no_stream_payloads(monkeypatch):
    # OS-level causes (missing binary / launch failure) never had stream
    # attributes — pinned so the contract holds for every cause the runner
    # chains, not just the timeout.
    def fake_run(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run(["no-such-binary"])
    cause = excinfo.value.__cause__
    assert isinstance(cause, FileNotFoundError)
    for attr in ("output", "stdout", "stderr"):
        assert getattr(cause, attr, None) is None
    # The chain's diagnostics survive.
    assert "No such file" in str(cause)


# ---------------------------------------------------------------------------
# secret_stdout — a secret-bearing stdout channel never rides a failure
# ---------------------------------------------------------------------------


def test_secret_stdout_suppresses_partial_stdout_on_timeout(monkeypatch, caplog):
    # A killed secret fetch: subprocess attaches the partial secret it had
    # written to stdout. With secret_stdout the runner must swap that for the
    # placeholder on BOTH the raised error and the one ERROR record — the secret
    # is not yet registered with the redactor, so suppression is the only guard.
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(
            argv, 0.1, output="s3cret-plaintext", stderr="doppler: deadline"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with caplog.at_level(logging.DEBUG, logger="shipit.exec"):
        with pytest.raises(execrun.ExecError) as excinfo:
            execrun.run(["doppler", "secrets", "get"], timeout=0.1, secret_stdout=True)
    err = excinfo.value
    assert err.cause == execrun.CAUSE_TIMEOUT
    assert err.stdout == execrun.SECRET_STDOUT_PLACEHOLDER
    assert "s3cret-plaintext" not in err.stdout
    # stderr diagnostics survive — only the secret-bearing channel is dropped.
    assert err.stderr == "doppler: deadline"
    full_log = "\n".join(r.getMessage() for r in caplog.records)
    assert "s3cret-plaintext" not in full_log


def test_secret_stdout_success_still_returns_the_real_stdout(monkeypatch):
    # The suppression is failure-only: a completed fetch must hand the caller the
    # real secret (and a completed check=False run records argv only anyway).
    monkeypatch.setattr(subprocess, "run", _fake_completed(rc=0, stdout="s3cret\n"))
    result = execrun.run(["doppler", "get"], check=False, secret_stdout=True)
    assert result.stdout == "s3cret\n"


def test_secret_stdout_suppresses_stdout_on_nonzero_under_check(monkeypatch):
    # If a secret call is run under check=True, a nonzero exit still scrubs the
    # secret-bearing stdout from the raised error.
    monkeypatch.setattr(
        subprocess, "run", _fake_completed(rc=1, stdout="s3cret", stderr="denied")
    )
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run(["doppler", "get"], secret_stdout=True)
    assert excinfo.value.stdout == execrun.SECRET_STDOUT_PLACEHOLDER
    assert excinfo.value.stderr == "denied"


# ---------------------------------------------------------------------------
# Record emission — exactly one record per Exec
# ---------------------------------------------------------------------------


def test_success_emits_exactly_one_debug_record(monkeypatch, caplog):
    monkeypatch.setattr(subprocess, "run", _fake_completed(rc=0, stdout="ok"))
    with caplog.at_level(logging.DEBUG, logger="shipit.exec"):
        execrun.run(["tool", "arg"], cwd="/work")
    records = [r for r in caplog.records if r.name == "shipit.exec"]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
    message = records[0].getMessage()
    assert "tool arg" in message
    assert "/work" in message
    assert "rc=0" in message
    assert "ms" in message


def test_check_false_nonzero_records_at_debug_not_error(monkeypatch, caplog):
    # A nonzero rc the caller declared normal (check=False) is not a failure:
    # probing a dead pid must not spam the WARNING+ console sink.
    monkeypatch.setattr(subprocess, "run", _fake_completed(rc=1))
    with caplog.at_level(logging.DEBUG, logger="shipit.exec"):
        execrun.run(["ps", "-p", "1"], check=False)
    records = [r for r in caplog.records if r.name == "shipit.exec"]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG


def test_failure_emits_exactly_one_error_record_with_both_tails(monkeypatch, caplog):
    monkeypatch.setattr(
        subprocess,
        "run",
        _fake_completed(rc=2, stdout="the stdout diagnostics", stderr="the stderr"),
    )
    with caplog.at_level(logging.DEBUG, logger="shipit.exec"):
        with pytest.raises(execrun.ExecError):
            execrun.run(["pixi", "install"], cwd="/tree")
    records = [r for r in caplog.records if r.name == "shipit.exec"]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR
    message = records[0].getMessage()
    assert "pixi install" in message
    assert "/tree" in message
    assert "rc=2" in message
    assert "the stdout diagnostics" in message  # stdout tail preserved (PRD gap)
    assert "the stderr" in message


# ---------------------------------------------------------------------------
# Redaction — everything logged or raised passes through the central redactor
# ---------------------------------------------------------------------------


@pytest.fixture()
def _clean_registry():
    redact.clear_registered_secrets()
    yield
    redact.clear_registered_secrets()


def _render(records) -> str:
    """Render records through the shared sink pipeline — POST-format output.

    The runner's records carry no per-site masking (#277): the central
    ``redact.redact_event`` processor masks every record at FORMAT time, inside
    ``logsetup._PIPELINE``. ``caplog`` captures records PRE-format, so redaction
    must be asserted on what a sink actually writes — a record rendered through
    the same :class:`~structlog.stdlib.ProcessorFormatter` every sink shares —
    never on ``record.getMessage()``.
    """
    from shipit import logsetup

    formatter = logsetup._file_formatter()
    return "\n".join(formatter.format(r) for r in records)


def test_error_and_record_are_redacted(monkeypatch, caplog, _clean_registry):
    redact.register_secret("s3cret-value")
    monkeypatch.setattr(
        subprocess,
        "run",
        _fake_completed(rc=1, stdout="ghp_abc123token", stderr="leaked s3cret-value"),
    )
    with caplog.at_level(logging.DEBUG, logger="shipit.exec"):
        with pytest.raises(execrun.ExecError) as excinfo:
            execrun.run(["tool", "--token", "s3cret-value"])
    err = excinfo.value
    # Raised channel: message and every attribute are masked.
    for text in (str(err), err.stderr, err.stdout, " ".join(err.argv)):
        assert "s3cret-value" not in text
        assert "ghp_abc123token" not in text
    # Logged channel: the one record is masked too.
    full_log = "\n".join(r.getMessage() for r in caplog.records)
    assert "s3cret-value" not in full_log
    assert "ghp_abc123token" not in full_log


def test_success_record_argv_is_redacted_at_format_time(
    monkeypatch, caplog, _clean_registry
):
    monkeypatch.setattr(subprocess, "run", _fake_completed(rc=0))
    with caplog.at_level(logging.DEBUG, logger="shipit.exec"):
        execrun.run(["curl", "-H", "Authorization: ghp_tok3nvalue"])
    rendered = _render(caplog.records)
    assert "ghp_tok3nvalue" not in rendered
    assert redact.MASK in rendered


def test_success_record_cwd_is_redacted_at_format_time(
    monkeypatch, caplog, _clean_registry
):
    # cwd is a logged field, so it passes through the central redactor too: a
    # secret in the working-directory path must not leak via the success record.
    redact.register_secret("s3cret-dir")
    monkeypatch.setattr(subprocess, "run", _fake_completed(rc=0))
    with caplog.at_level(logging.DEBUG, logger="shipit.exec"):
        execrun.run(["tool"], cwd="/work/s3cret-dir/clone")
    rendered = _render(caplog.records)
    assert "s3cret-dir" not in rendered
    assert redact.MASK in rendered


def test_failure_record_cwd_is_redacted_at_format_time(
    monkeypatch, caplog, _clean_registry
):
    # Same contract on the failure record, which logs cwd via _record_failure.
    redact.register_secret("s3cret-dir")
    monkeypatch.setattr(subprocess, "run", _fake_completed(rc=2, stderr="boom"))
    with caplog.at_level(logging.DEBUG, logger="shipit.exec"):
        with pytest.raises(execrun.ExecError):
            execrun.run(["tool"], cwd="/work/s3cret-dir/clone")
    rendered = _render(caplog.records)
    assert "s3cret-dir" not in rendered
    assert redact.MASK in rendered


def test_argv_non_string_elements_are_coerced(monkeypatch, caplog, _clean_registry):
    # subprocess.run natively accepts Path/numeric argv elements; the seam
    # coerces them to str so the record and ExecResult.argv are honest strings
    # and redaction never crashes on a non-str element.
    import pathlib

    monkeypatch.setattr(subprocess, "run", _fake_completed(rc=0))
    with caplog.at_level(logging.DEBUG, logger="shipit.exec"):
        result = execrun.run(["tool", pathlib.Path("/some/path"), 42])
    assert result.argv == ("tool", "/some/path", "42")
    assert all(isinstance(a, str) for a in result.argv)
    message = "\n".join(r.getMessage() for r in caplog.records)
    assert "/some/path" in message


# ---------------------------------------------------------------------------
# Structured fields (#310) — the Exec record's data is a query, not a parse
# ---------------------------------------------------------------------------


def _jsonl_records(tmp_path, emit) -> list[dict]:
    """Run ``emit`` with the REAL JSONL file sink attached; return parsed records.

    The structured-field contract is about what lands in the raw log, so these
    tests parse what :func:`shipit.logsetup.build_file_handler`'s sink actually
    writes — never the in-memory record object (``caplog``), which would pass
    even if the pipeline dropped the extras.
    """
    from shipit import logsetup
    from shipit.identity import repo_from_slug

    handler = logsetup.build_file_handler(repo_from_slug("o/r"), base_dir=tmp_path)
    log = logging.getLogger("shipit.exec")
    old_level, old_propagate = log.level, log.propagate
    log.setLevel(logging.DEBUG)
    log.propagate = False  # keep pytest's root handlers out of the picture
    log.addHandler(handler)
    try:
        emit()
    finally:
        log.removeHandler(handler)
        handler.close()
        log.setLevel(old_level)
        log.propagate = old_propagate
    raw = (tmp_path / "o" / "r" / "shipit.log").read_text()
    return [json.loads(line) for line in raw.splitlines()]


def test_success_record_carries_flat_fields_and_human_msg(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", _fake_completed(rc=0))

    records = _jsonl_records(
        tmp_path, lambda: execrun.run(["tool", "arg"], cwd="/work")
    )

    assert len(records) == 1
    rec = records[0]
    # The outcome as flat, typed JSONL keys — no msg parsing needed.
    assert rec["argv"] == "tool arg"
    assert rec["cwd"] == "/work"
    assert rec["rc"] == 0
    assert isinstance(rec["duration_ms"], int)
    assert rec["level"] == "debug"
    # The msg stays human-readable and self-sufficient: command and outcome
    # inline (fields are additive, ADR-0029's "human msg inside" rule).
    assert "tool arg" in rec["msg"]
    assert "rc=0" in rec["msg"]


def test_check_false_nonzero_record_carries_rc_field(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "run", _fake_completed(rc=1))

    records = _jsonl_records(
        tmp_path, lambda: execrun.run(["ps", "-p", "1"], check=False)
    )

    assert records[0]["rc"] == 1
    assert records[0]["level"] == "debug"


def test_failure_record_carries_cause_and_stream_tail_fields(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess,
        "run",
        _fake_completed(rc=2, stdout="the stdout diagnostics", stderr="the stderr"),
    )

    def emit():
        with pytest.raises(execrun.ExecError):
            execrun.run(["pixi", "install"], cwd="/tree")

    records = _jsonl_records(tmp_path, emit)
    assert len(records) == 1
    rec = records[0]
    assert rec["level"] == "error"
    assert rec["argv"] == "pixi install"
    assert rec["cwd"] == "/tree"
    assert rec["rc"] == 2
    assert rec["cause"] == execrun.CAUSE_EXIT
    assert rec["stdout_tail"] == "the stdout diagnostics"
    assert rec["stderr_tail"] == "the stderr"
    assert isinstance(rec["duration_ms"], int)
    # The msg still tells a human the whole story inline.
    assert "pixi install" in rec["msg"]
    assert "rc=2" in rec["msg"]


def test_timeout_record_fields_omit_rc_absent_not_null(monkeypatch, tmp_path):
    # A timeout has no exit code: the rc field is ABSENT (ADR-0029's
    # absent-not-null rule), never null — and the cause + partial tails ride
    # as fields.
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(
            argv, 0.1, output="partial stdout", stderr="partial stderr"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    def emit():
        with pytest.raises(execrun.ExecError):
            execrun.run(["slow-tool"], timeout=0.1)

    records = _jsonl_records(tmp_path, emit)
    rec = records[0]
    assert rec["cause"] == execrun.CAUSE_TIMEOUT
    assert "rc" not in rec
    assert rec["stdout_tail"] == "partial stdout"
    assert rec["stderr_tail"] == "partial stderr"
    assert isinstance(rec["duration_ms"], int)


def test_missing_binary_record_fields(monkeypatch, tmp_path):
    def fake_run(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(subprocess, "run", fake_run)

    def emit():
        with pytest.raises(execrun.ExecError):
            execrun.run(["no-such-binary"])

    records = _jsonl_records(tmp_path, emit)
    rec = records[0]
    assert rec["cause"] == execrun.CAUSE_MISSING_BINARY
    assert "rc" not in rec
    assert rec["argv"] == "no-such-binary"


def test_spawn_detached_record_carries_pid_not_rc(monkeypatch, tmp_path):
    captured: dict = {}
    monkeypatch.setattr(subprocess, "Popen", _FakePopen(captured))

    records = _jsonl_records(
        tmp_path,
        lambda: execrun.spawn_detached(["tool", "--flag"], cwd="/work/tree"),
    )

    assert len(records) == 1
    rec = records[0]
    assert rec["argv"] == "tool --flag"
    assert rec["cwd"] == "/work/tree"
    assert rec["pid"] == 4321
    # No completion → no completion fields on the detached-spawn record.
    assert "rc" not in rec
    assert "duration_ms" not in rec


def test_jq_style_slices_work_on_the_raw_log(monkeypatch, tmp_path):
    # The acceptance query shapes from the PRD: "all Execs slower than 10s"
    # and "all nonzero exits" as field selections over the raw JSONL — the
    # exact slices that used to require regexing the msg. The log includes a
    # launch failure (rc ABSENT, not null) so the nonzero-exit query's
    # has("rc") guard is genuinely exercised.
    def emit():
        monkeypatch.setattr(subprocess, "run", _fake_completed(rc=0))
        execrun.run(["fast-tool"])
        with monkeypatch.context() as m:
            m.setattr(execrun, "_elapsed_ms", lambda start: 12500)
            execrun.run(["slow-tool"])
        monkeypatch.setattr(subprocess, "run", _fake_completed(rc=1))
        execrun.run(["gh", "probe"], check=False)
        monkeypatch.setattr(subprocess, "run", _fake_completed(rc=2, stderr="boom"))
        with pytest.raises(execrun.ExecError):
            execrun.run(["gh", "broken"])

        def missing(argv, **kwargs):
            raise FileNotFoundError(2, "No such file or directory", argv[0])

        monkeypatch.setattr(subprocess, "run", missing)
        with pytest.raises(execrun.ExecError):
            execrun.run(["no-such-binary"])

    records = _jsonl_records(tmp_path, emit)
    assert len(records) == 5

    # jq 'select(.duration_ms > 10000)'
    slow = [r for r in records if r.get("duration_ms", 0) > 10000]
    assert [r["argv"] for r in slow] == ["slow-tool"]

    # jq 'select(has("rc") and .rc != 0)' — the has("rc") guard is
    # load-bearing: the launch-failure record carries no rc at all, and in jq
    # a missing field reads as null, so the bare `select(.rc != 0)` would
    # wrongly match it (null != 0 is true).
    assert any("rc" not in r for r in records)
    nonzero = [r for r in records if "rc" in r and r["rc"] != 0]
    assert sorted(r["argv"] for r in nonzero) == ["gh broken", "gh probe"]

    # The bare query really is wrong on this log — the documented guard is
    # not decorative.
    naive = [r for r in records if r.get("rc") != 0]
    assert "no-such-binary" in [r["argv"] for r in naive]


def test_structured_fields_are_redacted_post_format(monkeypatch, tmp_path):
    # argv and the stream tails are the secret-bearing fields: a registered
    # secret riding either must never reach the sink — asserted on the raw
    # bytes the real file sink writes (post-format, where redact_event runs).
    redact.clear_registered_secrets()
    redact.register_secret("s3cret-value")
    try:
        monkeypatch.setattr(
            subprocess,
            "run",
            _fake_completed(rc=1, stderr="leaked s3cret-value in the tail"),
        )

        def emit():
            with pytest.raises(execrun.ExecError):
                execrun.run(["tool", "--token", "s3cret-value"])

        records = _jsonl_records(tmp_path, emit)
        rec = records[0]
        assert "s3cret-value" not in json.dumps(rec)
        assert redact.MASK in rec["argv"]
        assert redact.MASK in rec["stderr_tail"]
    finally:
        redact.clear_registered_secrets()


# ---------------------------------------------------------------------------
# Env and stdin plumbing (semantics carried over from the proto-runner)
# ---------------------------------------------------------------------------


def test_env_merges_over_environ_by_default(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _capture_kwargs(captured))
    monkeypatch.setenv("KEEP_ME", "yes")
    execrun.run(["tool"], env={"LC_ALL": "C"})
    assert captured["env"]["LC_ALL"] == "C"
    assert captured["env"]["KEEP_ME"] == "yes"


def test_replace_env_uses_env_verbatim(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _capture_kwargs(captured))
    monkeypatch.setenv("PIXI_PROJECT_MANIFEST", "/parent/pixi.toml")
    execrun.run(["tool"], env={"ONLY": "this"}, replace_env=True)
    assert captured["env"] == {"ONLY": "this"}


def test_no_env_passes_none(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _capture_kwargs(captured))
    execrun.run(["tool"])
    assert captured["env"] is None


def test_run_redirects_stdin_from_devnull_when_no_input(monkeypatch):
    """With no ``input``, the child's stdin is pinned to ``DEVNULL`` (ADR-0020).

    Inheriting the parent's stdin is the root cause of the intermittent agy
    hang: a child that reads an idle inherited pipe blocks forever.
    """
    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _capture_kwargs(captured))
    execrun.run(["true"])
    assert captured["stdin"] is subprocess.DEVNULL
    # input must be None (not piped) so the DEVNULL redirect is the one in
    # effect — passing both input and stdin to subprocess.run is a ValueError.
    assert captured["input"] is None


def test_run_leaves_stdin_to_subprocess_when_input_given(monkeypatch):
    """When ``input`` IS supplied, ``stdin`` is left as ``None`` for subprocess."""
    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _capture_kwargs(captured))
    execrun.run(["cat"], input="hello")
    assert captured["stdin"] is None
    assert captured["input"] == "hello"


def test_run_does_not_hang_on_stdin_reading_child():
    """End-to-end: a child that reads ALL of stdin returns promptly, not hangs."""
    result = execrun.run([sys.executable, "-c", "import sys; sys.stdin.read()"])
    assert result.rc == 0


# ---------------------------------------------------------------------------
# spawn_detached — the seam's one deliberate non-Exec (fire-and-forget)
# ---------------------------------------------------------------------------


class _FakePopen:
    pid = 4321

    def __init__(self, captured: dict):
        self._captured = captured

    def __call__(self, argv, **kwargs):
        self._captured["argv"] = argv
        self._captured.update(kwargs)
        return self


def test_spawn_detached_semantics(monkeypatch):
    """Own session, all three stdio streams to /dev/null, fds closed, no wait:
    the exact Popen semantics the review path's original detach carried."""
    captured: dict = {}
    monkeypatch.setattr(subprocess, "Popen", _FakePopen(captured))
    assert execrun.spawn_detached(["tool", "--flag"]) is None  # no handle retained
    assert captured["argv"] == ["tool", "--flag"]
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["stdout"] is subprocess.DEVNULL
    assert captured["stderr"] is subprocess.DEVNULL
    assert captured["start_new_session"] is True
    assert captured["close_fds"] is True


def test_spawn_detached_coerces_argv_to_str(monkeypatch):
    import pathlib

    captured: dict = {}
    monkeypatch.setattr(subprocess, "Popen", _FakePopen(captured))
    execrun.spawn_detached(["tool", pathlib.Path("/some/path"), 42])
    assert captured["argv"] == ["tool", "/some/path", "42"]
    assert all(isinstance(a, str) for a in captured["argv"])


def test_spawn_detached_emits_one_debug_record_with_argv_cwd_pid(monkeypatch, caplog):
    """One structured record at spawn time (glassbox story 3): the detached
    child stays on the causal record chain — argv, cwd, and the pid a reader
    correlates the child's own records back to."""
    captured: dict = {}
    monkeypatch.setattr(subprocess, "Popen", _FakePopen(captured))
    with caplog.at_level(logging.DEBUG, logger="shipit.exec"):
        execrun.spawn_detached(["tool", "--flag"], cwd="/work/tree")
    records = [r for r in caplog.records if r.name == "shipit.exec"]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
    message = records[0].getMessage()
    assert "tool --flag" in message
    assert "/work/tree" in message
    assert "pid=4321" in message


def test_spawn_detached_record_is_redacted_at_format_time(
    monkeypatch, caplog, _clean_registry
):
    redact.register_secret("s3cret-value")
    captured: dict = {}
    monkeypatch.setattr(subprocess, "Popen", _FakePopen(captured))
    with caplog.at_level(logging.DEBUG, logger="shipit.exec"):
        execrun.spawn_detached(
            ["tool", "--token", "s3cret-value"], cwd="/work/s3cret-value/clone"
        )
    rendered = _render(caplog.records)
    assert "s3cret-value" not in rendered
    assert redact.MASK in rendered


def test_spawn_detached_missing_binary_normalizes_into_execerror(monkeypatch, caplog):
    """Launch normalization still applies to the non-Exec: no raw OSError
    escapes the seam, and the failure leaves its one ERROR record."""

    def fake_popen(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    with caplog.at_level(logging.DEBUG, logger="shipit.exec"):
        with pytest.raises(execrun.ExecError) as excinfo:
            execrun.spawn_detached(["no-such-tool-xyz"])
    err = excinfo.value
    assert err.cause == execrun.CAUSE_MISSING_BINARY
    assert err.rc is None
    records = [r for r in caplog.records if r.name == "shipit.exec"]
    assert len(records) == 1
    assert records[0].levelno == logging.ERROR


def test_spawn_detached_missing_binary_real_child():
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.spawn_detached(["definitely-not-a-binary-xyz"])
    assert excinfo.value.cause == execrun.CAUSE_MISSING_BINARY


def test_spawn_detached_bad_cwd_normalizes_to_os_error(monkeypatch):
    """A missing cwd also raises FileNotFoundError, but naming the directory —
    it must report as an os-error, not as a missing binary."""

    def fake_popen(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "/no/such/dir")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.spawn_detached(["tool"], cwd="/no/such/dir")
    assert excinfo.value.cause == execrun.CAUSE_OS


def test_spawn_detached_real_child_runs_in_own_session(tmp_path):
    """End-to-end detach: a real child lands in its OWN session (survives the
    parent exiting, no controlling terminal) and actually runs — observed via
    a file it writes, since its stdio is pinned to /dev/null."""
    import os
    import time

    out = tmp_path / "sid"
    execrun.spawn_detached(
        [
            sys.executable,
            "-c",
            "import os, sys; open(sys.argv[1], 'w').write(str(os.getsid(0)))",
            str(out),
        ]
    )
    deadline = time.monotonic() + 10
    while not out.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert out.exists(), "detached child never ran"
    # Read may race the child's write+close; poll until non-empty.
    while not out.read_text() and time.monotonic() < deadline:
        time.sleep(0.05)
    child_sid = int(out.read_text())
    assert child_sid != os.getsid(0)  # own session, not the parent's
