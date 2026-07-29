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


def test_prompt_bearing_argv_is_summarized_for_exec_records():
    display = execrun._display_argv(
        [
            "codex",
            "exec",
            "-c",
            "developer_instructions=SECRET ROLE SLICE",
            "--model",
            "gpt-5.5",
            "SECRET TASK PROMPT",
        ]
    )

    joined = " ".join(display)
    assert "SECRET ROLE SLICE" not in joined
    assert "SECRET TASK PROMPT" not in joined
    assert "developer_instructions=<redacted: prompt sha256=" in joined
    assert joined.count("<redacted: prompt sha256=") == 2


def test_every_repeated_prompt_flag_is_summarized():
    display = execrun._display_argv(
        ["agy", "--print", "FIRST PROMPT", "--print", "SECOND PROMPT"]
    )
    joined = " ".join(display)
    assert "FIRST PROMPT" not in joined
    assert "SECOND PROMPT" not in joined
    assert joined.count("<redacted: prompt sha256=") == 2


@pytest.mark.parametrize(
    "child,payloads",
    [
        (["claude", "-p", "CLAUDE TASK"], ["CLAUDE TASK"]),
        (
            [
                "codex",
                "exec",
                "-c",
                "developer_instructions=CODEX ROLE",
                "CODEX TASK",
            ],
            ["CODEX ROLE", "CODEX TASK"],
        ),
        (["agy", "--print", "AGY TASK"], ["AGY TASK"]),
    ],
)
@pytest.mark.parametrize("separator", [True, False])
def test_pixi_run_wrapper_summarizes_nested_backend_prompts(child, payloads, separator):
    argv = ["pixi", "run", "--manifest-path", "/tree/pixi.toml"]
    if separator:
        argv.append("--")
    argv.extend(child)
    display = execrun._display_argv(argv)
    joined = " ".join(display)
    assert display[:4] == argv[:4]
    for payload in payloads:
        assert payload not in joined
    assert joined.count("<redacted: prompt sha256=") == len(payloads)


@pytest.mark.parametrize(
    "pixi_options",
    [
        ["-e", "codex"],
        ["--environment", "agy"],
        ["--manifest-path=claude"],
    ],
)
def test_pixi_option_value_named_like_backend_is_not_the_child(pixi_options):
    prompt = "REAL CLAUDE PROMPT"
    argv = ["pixi", "run", *pixi_options, "claude", "-p", prompt]
    display = execrun._display_argv(argv)
    assert display[: 2 + len(pixi_options)] == argv[: 2 + len(pixi_options)]
    assert prompt not in " ".join(display)


def test_pixi_downstream_separator_does_not_hide_earlier_backend_prompt():
    prompt = "AGY PROMPT BEFORE DOWNSTREAM SEPARATOR"
    argv = ["pixi", "run", "agy", "--print", prompt, "--", "--extra"]
    display = execrun._display_argv(argv)
    assert prompt not in " ".join(display)
    assert display[-2:] == ["--", "--extra"]


@pytest.mark.parametrize(
    "argv",
    [
        ["codex", "exec"],
        ["codex", "exec", "--model", "gpt-5"],
    ],
)
def test_codex_flag_only_argv_keeps_diagnostic_shape(argv):
    assert execrun._display_argv(argv) == argv


def test_codex_boolean_flag_after_developer_instructions_is_not_a_prompt():
    argv = ["codex", "exec", "-c", "developer_instructions=ROLE", "--json"]
    display = execrun._display_argv(argv)
    assert display[-1] == "--json"
    assert "ROLE" not in " ".join(display)


def test_codex_prompt_starting_with_hyphen_is_summarized():
    argv = [
        "codex",
        "exec",
        "-c",
        "developer_instructions=ROLE SLICE",
        "- Please fix the session setup",
    ]
    joined = " ".join(execrun._display_argv(argv))
    assert "ROLE SLICE" not in joined
    assert "Please fix" not in joined
    assert joined.count("<redacted: prompt sha256=") == 2


def test_codex_failure_never_exposes_prompt_payloads(monkeypatch, caplog):
    role = "SECRET ROLE SLICE"
    task = "SECRET TASK PROMPT"
    argv = [
        "codex",
        "exec",
        "-c",
        f"developer_instructions={role}",
        "--model",
        "gpt-5.5",
        task,
    ]
    monkeypatch.setattr(
        subprocess,
        "run",
        _fake_completed(rc=1, stderr=f"launch failed: {role}; task: {task}"),
    )
    with caplog.at_level(logging.ERROR, logger="shipit.exec"):
        with pytest.raises(execrun.ExecError) as excinfo:
            execrun.run(argv)
    err = excinfo.value
    surfaced = " ".join(err.argv) + err.stderr + str(err)
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    for leak in (role, task):
        assert leak not in surfaced
        assert leak not in rendered
    assert surfaced.count("<redacted: prompt sha256=") >= 2


def test_pixi_wrapped_codex_failure_never_exposes_prompt_payloads(monkeypatch, caplog):
    role = "WRAPPED SECRET ROLE"
    task = "WRAPPED SECRET TASK"
    argv = [
        "pixi",
        "run",
        "--manifest-path",
        "/tree/pixi.toml",
        "--",
        "codex",
        "exec",
        "-c",
        f"developer_instructions={role}",
        task,
    ]
    monkeypatch.setattr(
        subprocess,
        "run",
        _fake_completed(rc=1, stderr=f"launch failed: {role}; task: {task}"),
    )
    with caplog.at_level(logging.ERROR, logger="shipit.exec"):
        with pytest.raises(execrun.ExecError) as excinfo:
            execrun.run(argv)
    surfaced = (
        " ".join(excinfo.value.argv)
        + excinfo.value.stderr
        + str(excinfo.value)
        + "\n".join(record.getMessage() for record in caplog.records)
    )
    assert role not in surfaced
    assert task not in surfaced


def test_short_prompt_echo_suppresses_ambiguous_failure_stream(monkeypatch):
    task = "fix"
    argv = [
        "codex",
        "exec",
        "-c",
        "developer_instructions=ROLE SLICE",
        task,
    ]
    diagnostic = "prefix: failed to fix process configuration"
    monkeypatch.setattr(
        subprocess,
        "run",
        _fake_completed(rc=1, stderr=diagnostic),
    )
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run(argv)
    err = excinfo.value
    assert err.stderr == execrun.PROMPT_STREAM_PLACEHOLDER
    assert diagnostic not in str(err)


def test_unrelated_print_equals_argument_does_not_suppress_failure_stream(monkeypatch):
    diagnostic = "script failed on line 1"
    monkeypatch.setattr(
        subprocess,
        "run",
        _fake_completed(rc=1, stderr=diagnostic),
    )
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run(["python", "script.py", "--print=1"])
    assert excinfo.value.stderr == diagnostic


def test_missing_binary_normalizes_into_execerror(monkeypatch):
    def fake_run(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run(["no-such-binary"])
    err = excinfo.value
    assert err.cause == execrun.CAUSE_MISSING_BINARY
    assert err.rc is None
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
    def fake_run(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", kwargs["cwd"])

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run(["tool"], cwd="/no/such/dir")
    assert excinfo.value.cause == execrun.CAUSE_OS


def test_missing_cwd_real_child():
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run(["true"], cwd="/shipit/no/such/dir/xyzzy")
    assert excinfo.value.cause == execrun.CAUSE_OS


def test_missing_binary_real_child():
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run(["shipit-no-such-binary-xyzzy"])
    assert excinfo.value.cause == execrun.CAUSE_MISSING_BINARY


def test_undecodable_output_replaced_not_raised():
    result = execrun.run(
        [sys.executable, "-c", r'import sys; sys.stdout.buffer.write(b"\xff\xfe ok")']
    )
    assert result.ok
    assert "ok" in result.stdout
    assert "�" in result.stdout


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
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 0.1, output=b"partial", stderr=None)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run(["slow-tool"], timeout=0.1)
    assert excinfo.value.stdout == "partial"
    assert excinfo.value.stderr == ""


def test_timeout_real_child_is_killed():
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.2)
    assert excinfo.value.cause == execrun.CAUSE_TIMEOUT


def test_timeout_cause_is_sanitized_of_stream_payloads(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(
            argv, 0.1, output="raw partial stdout", stderr="raw partial stderr"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run(["slow-tool"], timeout=0.1)
    err = excinfo.value
    assert err.stdout == "raw partial stdout"
    cause = err.__cause__
    assert isinstance(cause, subprocess.TimeoutExpired)
    assert cause.output is None
    assert cause.stdout is None
    assert cause.stderr is None
    assert "timed out" in str(cause)
    assert "raw partial" not in repr(vars(cause))


def test_timeout_cause_carries_no_secret_with_secret_stdout(monkeypatch):
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
    redact.register_secret("s3cret-value")

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 0.1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run(["tool", "--token", "s3cret-value"], timeout=0.1)
    cause = excinfo.value.__cause__
    assert "s3cret-value" not in " ".join(cause.cmd)
    assert "s3cret-value" not in str(cause)


def test_timeout_cause_cmd_summarizes_codex_prompts(monkeypatch):
    role = "SECRET ROLE SLICE"
    task = "SECRET TASK PROMPT"
    argv = [
        "codex",
        "exec",
        "-c",
        f"developer_instructions={role}",
        task,
    ]

    def fake_run(child_argv, **kwargs):
        raise subprocess.TimeoutExpired(child_argv, 0.1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run(argv, timeout=0.1)
    cause = excinfo.value.__cause__
    surfaced = " ".join(cause.cmd) + str(cause) + repr(cause)
    assert role not in surfaced
    assert task not in surfaced
    assert surfaced.count("<redacted: prompt sha256=") >= 2


def test_timeout_cause_args_tuple_is_sanitized(monkeypatch, _clean_registry):
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
    redact.register_secret("s3cret-value")
    exc = subprocess.TimeoutExpired("tool --token s3cret-value", 0.1, output="raw")
    sanitized = execrun._sanitize_cause(exc)
    assert isinstance(sanitized.cmd, str)
    assert sanitized.cmd.startswith("tool --token ")
    assert "s3cret-value" not in sanitized.cmd
    assert "s3cret-value" not in repr(sanitized)
    assert sanitized.output is None


def test_timeout_real_child_cause_is_sanitized():
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
    def fake_run(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run(["no-such-binary"])
    cause = excinfo.value.__cause__
    assert isinstance(cause, FileNotFoundError)
    for attr in ("output", "stdout", "stderr"):
        assert getattr(cause, attr, None) is None
    assert "No such file" in str(cause)


def test_secret_stdout_suppresses_partial_stdout_on_timeout(monkeypatch, caplog):
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
    assert err.stderr == "doppler: deadline"
    full_log = "\n".join(r.getMessage() for r in caplog.records)
    assert "s3cret-plaintext" not in full_log


def test_secret_stdout_success_still_returns_the_real_stdout(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_completed(rc=0, stdout="s3cret\n"))
    result = execrun.run(["doppler", "get"], check=False, secret_stdout=True)
    assert result.stdout == "s3cret\n"


def test_secret_stdout_suppresses_stdout_on_nonzero_under_check(monkeypatch):
    monkeypatch.setattr(
        subprocess, "run", _fake_completed(rc=1, stdout="s3cret", stderr="denied")
    )
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run(["doppler", "get"], secret_stdout=True)
    assert excinfo.value.stdout == execrun.SECRET_STDOUT_PLACEHOLDER
    assert excinfo.value.stderr == "denied"


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
    assert "the stdout diagnostics" in message
    assert "the stderr" in message


@pytest.fixture()
def _clean_registry():
    redact.clear_registered_secrets()
    yield
    redact.clear_registered_secrets()


def _render(records) -> str:
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
    for text in (str(err), err.stderr, err.stdout, " ".join(err.argv)):
        assert "s3cret-value" not in text
        assert "ghp_abc123token" not in text
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
    redact.register_secret("s3cret-dir")
    monkeypatch.setattr(subprocess, "run", _fake_completed(rc=2, stderr="boom"))
    with caplog.at_level(logging.DEBUG, logger="shipit.exec"):
        with pytest.raises(execrun.ExecError):
            execrun.run(["tool"], cwd="/work/s3cret-dir/clone")
    rendered = _render(caplog.records)
    assert "s3cret-dir" not in rendered
    assert redact.MASK in rendered


def test_argv_non_string_elements_are_coerced(monkeypatch, caplog, _clean_registry):
    import pathlib

    monkeypatch.setattr(subprocess, "run", _fake_completed(rc=0))
    with caplog.at_level(logging.DEBUG, logger="shipit.exec"):
        result = execrun.run(["tool", pathlib.Path("/some/path"), 42])
    assert result.argv == ("tool", "/some/path", "42")
    assert all(isinstance(a, str) for a in result.argv)
    message = "\n".join(r.getMessage() for r in caplog.records)
    assert "/some/path" in message


def _jsonl_records(tmp_path, emit) -> list[dict]:
    from shipit import logsetup
    from shipit.identity import repo_from_slug

    handler = logsetup.build_file_handler(repo_from_slug("o/r"), base_dir=tmp_path)
    log = logging.getLogger("shipit.exec")
    old_level, old_propagate = log.level, log.propagate
    log.setLevel(logging.DEBUG)
    log.propagate = False
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
    assert rec["argv"] == "tool arg"
    assert rec["cwd"] == "/work"
    assert rec["rc"] == 0
    assert isinstance(rec["duration_ms"], int)
    assert rec["level"] == "debug"
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
    assert "pixi install" in rec["msg"]
    assert "rc=2" in rec["msg"]


def test_timeout_record_fields_omit_rc_absent_not_null(monkeypatch, tmp_path):
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
    assert "rc" not in rec
    assert "duration_ms" not in rec


def test_jq_style_slices_work_on_the_raw_log(monkeypatch, tmp_path):
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

    slow = [r for r in records if r.get("duration_ms", 0) > 10000]
    assert [r["argv"] for r in slow] == ["slow-tool"]

    assert any("rc" not in r for r in records)
    nonzero = [r for r in records if "rc" in r and r["rc"] != 0]
    assert sorted(r["argv"] for r in nonzero) == ["gh broken", "gh probe"]

    naive = [r for r in records if r.get("rc") != 0]
    assert "no-such-binary" in [r["argv"] for r in naive]


def test_structured_fields_are_redacted_post_format(monkeypatch, tmp_path):
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
    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _capture_kwargs(captured))
    execrun.run(["true"])
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["input"] is None


def test_run_leaves_stdin_to_subprocess_when_input_given(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(subprocess, "run", _capture_kwargs(captured))
    execrun.run(["cat"], input="hello")
    assert captured["stdin"] is None
    assert captured["input"] == "hello"


def test_run_does_not_hang_on_stdin_reading_child():
    result = execrun.run([sys.executable, "-c", "import sys; sys.stdin.read()"])
    assert result.rc == 0


class _FakePopen:
    pid = 4321

    def __init__(self, captured: dict):
        self._captured = captured

    def __call__(self, argv, **kwargs):
        self._captured["argv"] = argv
        self._captured.update(kwargs)
        return self


def test_spawn_detached_semantics(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(subprocess, "Popen", _FakePopen(captured))
    assert execrun.spawn_detached(["tool", "--flag"]) is None
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

    def fake_popen(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "/no/such/dir")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.spawn_detached(["tool"], cwd="/no/such/dir")
    assert excinfo.value.cause == execrun.CAUSE_OS


def test_spawn_detached_real_child_runs_in_own_session(tmp_path):
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
    while not out.read_text() and time.monotonic() < deadline:
        time.sleep(0.05)
    child_sid = int(out.read_text())
    assert child_sid != os.getsid(0)
