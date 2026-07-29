"""The one Exec seam: every external command shipit runs, normalized and recorded.

See docs/adr/0028-one-exec-seam-tool-adapters.md.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shlex
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass

from . import redact

logger = logging.getLogger("shipit.exec")

DEFAULT_TIMEOUT: float = 300.0

TAIL_CHARS = 2000

SECRET_STDOUT_PLACEHOLDER = "<redacted: secret-bearing stdout>"

SHORT_PROMPT_MAX_CHARS = 8
PROMPT_STREAM_PLACEHOLDER = "<redacted: prompt-bearing stream>"

AGENT_BINARIES = {"agy", "antigravity", "claude", "codex"}
PIXI_RUN_VALUE_FLAGS = {
    "-e",
    "--environment",
    "-p",
    "--platform",
    "--config-file",
    "--auth-file",
    "--concurrent-downloads",
    "--concurrent-solves",
    "--pinning-strategy",
    "--pypi-keyring-provider",
    "--tls-root-certs",
    "-m",
    "--manifest-path",
    "-w",
    "--workspace",
    "--color",
}
PIXI_RUN_BOOLEAN_FLAGS = {
    "-x",
    "--executable",
    "--clean-env",
    "--skip-deps",
    "--templated",
    "-n",
    "--dry-run",
    "--help",
    "-h",
    "--no-config",
    "--run-post-link-scripts",
    "--no-symbolic-links",
    "--no-hard-links",
    "--no-ref-links",
    "--tls-no-verify",
    "--use-environment-activation-cache",
    "--force-activate",
    "--no-completions",
    "--verbose",
    "--quiet",
    "--no-progress",
    "--no-install",
    "--frozen",
    "--locked",
    "--as-is",
}

CAUSE_EXIT = "exit"
CAUSE_TIMEOUT = "timeout"
CAUSE_MISSING_BINARY = "missing-binary"
CAUSE_OS = "os-error"


@dataclass(frozen=True)
class ExecResult:
    """The normalized outcome of one completed Exec."""

    argv: tuple[str, ...]
    rc: int
    stdout: str
    stderr: str
    duration_ms: int

    @property
    def ok(self) -> bool:
        return self.rc == 0


class ExecError(RuntimeError):
    """The single transport error: an Exec failed. ``rc`` is ``None`` when the child never produced one; ``cause`` is a ``CAUSE_*`` tag, the only axis a caller should branch on. Every attribute and the message are pre-redacted."""

    def __init__(
        self,
        argv: list[str] | tuple[str, ...],
        *,
        rc: int | None,
        stdout: str = "",
        stderr: str = "",
        duration_ms: int = 0,
        cause: str = CAUSE_EXIT,
    ) -> None:
        raw_argv = [str(arg) for arg in argv]
        display_argv = _display_argv(raw_argv)
        self.argv = tuple(redact.redact_text(arg) for arg in display_argv)
        self.rc = rc
        self.stdout = redact.redact_text(
            _summarize_prompt_text(stdout, raw_argv, display_argv)
        )
        self.stderr = redact.redact_text(
            _summarize_prompt_text(stderr, raw_argv, display_argv)
        )
        self.duration_ms = duration_ms
        self.cause = cause
        detail = _tail(self.stderr) or _tail(self.stdout)
        message = f"{shlex.join(self.argv)} failed ({cause}, rc={rc}, {duration_ms}ms)"
        if detail:
            message += f": {detail}"
        super().__init__(message)


def _record_fields(
    argv: list[str] | tuple[str, ...],
    cwd: str | os.PathLike | None,
    **outcome: int | str | None,
) -> dict[str, int | str]:
    """The Exec record's structured fields — the ONE definition of the vocabulary; a ``None`` outcome value is DROPPED rather than recorded null."""
    fields: dict[str, int | str] = {
        "argv": shlex.join(_display_argv(argv)),
        "cwd": str(cwd or "."),
    }
    for key, value in outcome.items():
        if value is not None:
            fields[key] = value
    return fields


def _prompt_summary(text: str) -> str:
    """Stable bounded stand-in for agent prompt payloads; idempotent over its own output."""
    if text.startswith("<redacted: prompt sha256="):
        return text
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"<redacted: prompt sha256={digest} chars={len(text)}>"


def _display_argv(argv: list[str] | tuple[str, ...]) -> list[str]:
    """An argv safe for durable logs, with prompt payloads summarized. Never changes argv LENGTH — stream sanitization zips the raw and display lists positionally."""
    display = [str(arg) for arg in argv]
    if not display:
        return display

    def redact_after(flag: str) -> None:
        for index, arg in enumerate(display):
            if arg == flag and index + 1 < len(display):
                display[index + 1] = _prompt_summary(display[index + 1])

    binary = os.path.basename(display[0])
    if binary == "pixi" and "run" in display[1:]:
        run_index = display.index("run")
        child_start = _pixi_run_child_start(display, run_index)
        if child_start is not None and child_start < len(display):
            display[child_start:] = _display_argv(display[child_start:])
        elif any(
            os.path.basename(arg) in AGENT_BINARIES for arg in display[run_index + 1 :]
        ):
            for index in range(run_index + 1, len(display)):
                display[index] = _prompt_summary(display[index])
        return display
    if binary == "claude":
        redact_after("-p")
    elif binary == "codex" and "exec" in display:
        for index, arg in enumerate(display):
            if arg.startswith("developer_instructions="):
                display[index] = "developer_instructions=" + _prompt_summary(
                    arg.split("=", 1)[1]
                )
        value_flags = {
            "-c",
            "-C",
            "-m",
            "-s",
            "--cd",
            "--color",
            "--model",
            "--output-schema",
            "--profile",
            "--sandbox",
        }
        boolean_flags = {
            "--dangerously-bypass-approvals-and-sandbox",
            "--ephemeral",
            "--json",
            "--oss",
            "--skip-git-repo-check",
        }
        if (
            len(display) > 2
            and display[-1] != "exec"
            and display[-2] not in value_flags
            and display[-1] not in boolean_flags
            and not display[-1].startswith("developer_instructions=")
        ):
            display[-1] = _prompt_summary(display[-1])
    elif binary in {"agy", "antigravity"}:
        redact_after("--print")
        for index, arg in enumerate(display):
            if arg.startswith("--print="):
                display[index] = "--print=" + _prompt_summary(arg.split("=", 1)[1])
    return display


def _pixi_run_child_start(display: list[str], run_index: int) -> int | None:
    """The index of ``pixi run``'s first positional command, skipping its options, or ``None`` when the prefix is ambiguous."""
    index = run_index + 1
    while index < len(display):
        arg = display[index]
        if arg == "--":
            return index + 1
        if arg in PIXI_RUN_VALUE_FLAGS:
            index += 2
            continue
        if any(arg.startswith(f"{flag}=") for flag in PIXI_RUN_VALUE_FLAGS):
            index += 1
            continue
        if arg in PIXI_RUN_BOOLEAN_FLAGS or (
            arg.startswith("-") and set(arg[1:]) <= {"q", "v"}
        ):
            index += 1
            continue
        return index if os.path.basename(arg) in AGENT_BINARIES else None
    return None


def _summarize_prompt_text(
    text: str,
    raw_argv: list[str],
    display_argv: list[str],
) -> str:
    """Replace argv-carried prompt payloads echoed into a failure stream; a payload short enough to match ordinary text suppresses the whole stream instead."""
    replacements: list[tuple[str, str]] = []
    for raw, display in zip(raw_argv, display_argv, strict=True):
        if raw != display:
            replacements.append((raw, display))
            for prefix in ("developer_instructions=", "--print="):
                if raw.startswith(prefix) and display.startswith(prefix):
                    replacements.append(
                        (raw.removeprefix(prefix), display.removeprefix(prefix))
                    )
    if any(
        raw and len(raw) <= SHORT_PROMPT_MAX_CHARS and raw in text
        for raw, _display in replacements
    ):
        return PROMPT_STREAM_PLACEHOLDER
    for raw, display in sorted(
        replacements, key=lambda pair: len(pair[0]), reverse=True
    ):
        if raw:
            text = text.replace(raw, display)
    return text


def run(
    argv: list[str],
    *,
    cwd: str | os.PathLike | None = None,
    env: dict[str, str] | None = None,
    replace_env: bool = False,
    input: str | None = None,  # noqa: A002 — mirrors subprocess.run's parameter name
    check: bool = True,
    timeout: float | None = DEFAULT_TIMEOUT,
    secret_stdout: bool = False,
) -> ExecResult:
    """Execute one Exec (no shell), capturing text stdout/stderr.

    ``env`` is MERGED over ``os.environ`` unless ``replace_env=True``, which makes it
    the COMPLETE child environment — the only way to REMOVE an inherited variable.
    ``check=True`` raises :class:`ExecError` on a nonzero exit; ``timeout`` expiry kills
    the child and raises with ``cause=CAUSE_TIMEOUT``. With no ``input`` the child's
    stdin is redirected from ``os.devnull``. ``secret_stdout`` keeps the real stdout on
    the result but puts :data:`SECRET_STDOUT_PLACEHOLDER` on any error.
    """
    argv = [str(arg) for arg in argv]
    if env is None:
        merged_env = None
    elif replace_env:
        merged_env = env
    else:
        merged_env = {**os.environ, **env}
    start = time.monotonic()
    try:
        proc = subprocess.run(  # noqa: S603 — argv is a constructed list, never shell-interpolated
            argv,
            cwd=cwd,
            env=merged_env,
            input=input,
            # `input` and `stdin` are mutually exclusive in subprocess.run.
            stdin=subprocess.DEVNULL if input is None else None,
            capture_output=True,
            # Explicit over `text=True`, which would raise UnicodeDecodeError on
            # undecodable tool output — caught by neither handler below.
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        timeout_stdout = (
            SECRET_STDOUT_PLACEHOLDER if secret_stdout else _stream_text(exc.stdout)
        )
        error = ExecError(
            argv,
            rc=None,
            stdout=timeout_stdout,
            stderr=_stream_text(exc.stderr),
            duration_ms=_elapsed_ms(start),
            cause=CAUSE_TIMEOUT,
        )
        _record_failure(error, cwd)
        raise error from _sanitize_cause(exc)
    except OSError as exc:
        is_missing_binary = isinstance(exc, FileNotFoundError) and (
            cwd is None or str(exc.filename) != os.fspath(cwd)
        )
        cause = CAUSE_MISSING_BINARY if is_missing_binary else CAUSE_OS
        error = ExecError(
            argv,
            rc=None,
            stderr=str(exc),
            duration_ms=_elapsed_ms(start),
            cause=cause,
        )
        _record_failure(error, cwd)
        raise error from _sanitize_cause(exc)
    duration_ms = _elapsed_ms(start)
    if check and proc.returncode != 0:
        error = ExecError(
            argv,
            rc=proc.returncode,
            stdout=SECRET_STDOUT_PLACEHOLDER if secret_stdout else proc.stdout,
            stderr=proc.stderr,
            duration_ms=duration_ms,
            cause=CAUSE_EXIT,
        )
        _record_failure(error, cwd)
        raise error
    result = ExecResult(
        argv=tuple(argv),
        rc=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        duration_ms=duration_ms,
    )
    fields = _record_fields(
        result.argv, cwd, rc=result.rc, duration_ms=result.duration_ms
    )
    logger.debug(
        "exec %s (cwd=%s) -> rc=%d in %dms",
        fields["argv"],
        fields["cwd"],
        result.rc,
        result.duration_ms,
        extra=fields,
    )
    return result


def spawn_detached(
    argv: list[str] | tuple[str, ...],
    *,
    cwd: str | os.PathLike | None = None,
    env: Mapping[str, str] | None = None,
) -> None:
    """Spawn ``argv`` as a DETACHED fire-and-forget child — the seam's one non-Exec, so no result and no timeout. One spawn-time record and launch-error normalization still apply; ``env``, when given, is the child's FULL environment."""
    argv = [str(arg) for arg in argv]
    start = time.monotonic()
    try:
        proc = subprocess.Popen(  # noqa: S603 — argv is a constructed list, never shell-interpolated
            argv,
            cwd=cwd,
            env=None if env is None else dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as exc:
        is_missing_binary = isinstance(exc, FileNotFoundError) and (
            cwd is None or str(exc.filename) != os.fspath(cwd)
        )
        error = ExecError(
            argv,
            rc=None,
            stderr=str(exc),
            duration_ms=_elapsed_ms(start),
            cause=CAUSE_MISSING_BINARY if is_missing_binary else CAUSE_OS,
        )
        _record_failure(error, cwd)
        raise error from _sanitize_cause(exc)
    fields = _record_fields(argv, cwd, pid=proc.pid)
    logger.debug(
        "exec-detach %s (cwd=%s) -> pid=%d",
        fields["argv"],
        fields["cwd"],
        proc.pid,
        extra=fields,
    )


def _sanitize_cause(exc: BaseException) -> BaseException:
    """Scrub raw stream payloads off ``exc`` before chaining it as ``__cause__``; ``.args`` must be rebuilt too, since ``repr`` renders that tuple rather than the attributes."""
    if isinstance(exc, subprocess.TimeoutExpired):
        exc.output = None
        exc.stderr = None
        exc.cmd = (
            redact.redact_text(exc.cmd)
            if isinstance(exc.cmd, str)
            else [
                redact.redact_text(arg)
                for arg in _display_argv([str(arg) for arg in exc.cmd])
            ]
        )
        exc.args = (exc.cmd, exc.timeout, None, None)[: len(exc.args)]
    return exc


def _record_failure(error: ExecError, cwd: str | os.PathLike | None) -> None:
    fields = _record_fields(
        error.argv,
        cwd,
        rc=error.rc,
        duration_ms=error.duration_ms,
        cause=error.cause,
        stdout_tail=_tail(error.stdout),
        stderr_tail=_tail(error.stderr),
    )
    logger.error(
        "exec %s (cwd=%s) -> %s (rc=%s) in %dms\nstdout tail: %s\nstderr tail: %s",
        fields["argv"],
        fields["cwd"],
        error.cause,
        error.rc,
        error.duration_ms,
        fields["stdout_tail"],
        fields["stderr_tail"],
        extra=fields,
    )


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _tail(text: str) -> str:
    return text[-TAIL_CHARS:].strip()


def _stream_text(stream: str | bytes | None) -> str:
    """Normalize a ``TimeoutExpired`` partial stream to text; the type is version- and platform-dependent (``None``, ``bytes`` even in text mode, or ``str``)."""
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return stream
