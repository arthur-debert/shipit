"""execrun — the one Exec seam: every external command shipit runs (ADR-0028).

One execution of an external binary is an **Exec** (CONTEXT.md): argv in, run to
completion, a normalized :class:`ExecResult` or the single transport error
:class:`ExecError` out, and exactly one structured log record of what happened.
The contract, in full:

- **Result or one error.** Success (or any completed run with ``check=False``)
  returns an :class:`ExecResult` carrying rc, both captured streams, and the
  duration. Every failure — nonzero exit under ``check=True``, timeout expiry,
  a missing binary, any OS-level launch error — raises :class:`ExecError`
  carrying argv, rc, both streams, duration, and a ``cause`` tag. No raw
  ``OSError``/``FileNotFoundError``/``TimeoutExpired`` ever escapes.
- **Nothing hangs by default.** Every Exec carries a timeout, default
  :data:`DEFAULT_TIMEOUT` (5 minutes). Legitimate long-runners override it
  per call (``None`` allowed — an explicit choice, never the default).
- **One record per Exec, structured** — the record carries the outcome as
  FLAT FIELDS (stdlib ``extra=``, adopted into flat JSONL keys by the
  ADR-0029 pipeline) alongside a human-readable ``msg`` that still states the
  command and outcome inline. The field vocabulary, defined once in
  :func:`_record_fields` and used on every record this module emits:

  - ``argv`` — the command as ONE human-readable string (``shlex.join``;
    JSONL fields are flat scalars, not arrays);
  - ``cwd`` — the working directory (``"."`` when the caller passed none);
  - ``rc`` — the exit code, an int; ABSENT (not null) when the child never
    produced one (timeout, launch failure);
  - ``duration_ms`` — wall-clock milliseconds, an int;
  - on failure only: ``cause`` (a ``CAUSE_*`` tag) and ``stdout_tail`` /
    ``stderr_tail`` (the last :data:`TAIL_CHARS` of each stream);
  - on a detached spawn: ``pid`` instead of ``rc``/``duration_ms`` (there is
    no completion — see :func:`spawn_detached`).

  So "all Execs slower than 10s" or "all nonzero exits" is a query on the raw
  log (``jq 'select(.duration_ms > 10000)'``, ``jq 'select(has("rc") and
  .rc != 0)'`` — the ``has`` guard is load-bearing: ``rc`` is ABSENT on a
  record with no exit code, and in jq ``null != 0`` is true, so the bare
  ``.rc != 0`` would wrongly match timeouts, launch failures, and detached
  spawns), never a parse of ``msg`` (glassbox PRD story 14). Success logs at DEBUG,
  failure at ERROR. A nonzero exit under ``check=False`` is the caller's
  *normal* outcome (a liveness probe of a dead pid, ``git cat-file -e``), so
  it records at DEBUG, not ERROR.
- **Everything redacted.** Every attribute of an :class:`ExecError` is masked
  at construction (:mod:`shipit.redact`) — the error object surfaces to callers
  OUTSIDE the logging chain, so it can never carry a secret anywhere. That
  guarantee extends to the exception CHAIN (#317): the raw stdlib exception a
  failure wraps stays reachable via ``__cause__``, so it is sanitized of its
  captured stream payloads before chaining (:func:`_sanitize_cause`) — the
  chain's diagnostic value (type, message) survives; the raw streams do not.
  The log records themselves carry no per-site masking (#277): the central
  ``redact.redact_event`` processor in ``logsetup._PIPELINE`` masks every
  record, on every sink, at format time.

Rules carried over from the retired proto-runner: never ``shell=True``; never
interpolate into a shell string — commands are argument lists. Stdin (ADR-0020):
with no ``input`` the child's stdin is pinned to ``DEVNULL`` so a stdin-reading
child gets a clean EOF instead of hanging on an idle inherited pipe.

One deliberate NON-Exec lives here too: :func:`spawn_detached`, the detached
fire-and-forget spawn. It has no completion to normalize, so it is outside the
Exec contract — but it stays in this module so that every ``subprocess`` import
in shipit remains in exactly one file, and it keeps the parts of the contract
that do apply (spawn-time record, redaction, launch-error normalization).

Tests inject this seam rather than spawning tools: call sites take a ``runner``
parameter defaulting to :func:`run`, and the runner's own suite fakes
``subprocess.run`` to assert the result/error/record contract.
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

#: The Exec record's logger — a child of the package ``shipit`` logger, so it
#: inherits the sinks :func:`shipit.logsetup.configure_logging` attaches.
logger = logging.getLogger("shipit.exec")

#: The default per-Exec timeout, in seconds: 5 minutes — generous enough that
#: no normal tool call trips it, tight enough that nothing hangs forever
#: (ADR-0028). Known long-runners override per call; ``None`` disables.
DEFAULT_TIMEOUT: float = 300.0

#: How much of each stream a failure record / error message carries: the TAIL,
#: where tools put their actual diagnostics. The full streams stay on the
#: :class:`ExecError` itself.
TAIL_CHARS = 2000

#: What a ``secret_stdout=True`` Exec's stdout is replaced with the moment it
#: fails. A completed run's stdout is never recorded (success logs argv only),
#: but a TIMEOUT captures whatever partial stdout the child had already written
#: and a failure record / re-logged :class:`ExecError` would carry it. For a
#: secret-bearing stdout channel (``doppler ... --plain``) that value is not yet
#: registered with the redactor, so it would ride to a sink unredacted. When the
#: caller marks the Exec ``secret_stdout``, the error carries this placeholder in
#: place of stdout instead — the failure is still surfaced, the secret never is.
SECRET_STDOUT_PLACEHOLDER = "<redacted: secret-bearing stdout>"

#: :attr:`ExecError.cause` tags — the one axis callers may branch on.
CAUSE_EXIT = "exit"  # the child completed with a nonzero rc (check=True)
CAUSE_TIMEOUT = "timeout"  # the timeout expired; the child was killed
CAUSE_MISSING_BINARY = "missing-binary"  # argv[0] not found on PATH
CAUSE_OS = "os-error"  # any other OS-level launch failure


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
        """Whether the child exited 0."""
        return self.rc == 0


class ExecError(RuntimeError):
    """The single transport error: an Exec failed (ADR-0028).

    Carries argv, rc (``None`` when the child never produced one — timeout or
    launch failure), both captured streams (partial output on a timeout),
    ``duration_ms``, and ``cause`` (one of the ``CAUSE_*`` tags — the only
    axis a caller should branch on; there is no per-tool exception hierarchy).

    Every attribute and the message are pre-redacted: an ExecError is surfaced
    and re-logged by callers, so nothing secret may ride it to a sink.
    """

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
        self.argv = tuple(redact.redact_text(arg) for arg in argv)
        self.rc = rc
        self.stdout = redact.redact_text(stdout)
        self.stderr = redact.redact_text(stderr)
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
    """The Exec record's structured fields — the ONE definition of the vocabulary.

    Every record this module emits builds its ``extra=`` dict here, so the
    field names (see the module docstring: ``argv``, ``cwd``, ``rc``,
    ``duration_ms``, ``cause``, ``stdout_tail``, ``stderr_tail``, ``pid``)
    can never drift between the success, failure, and detached-spawn records.
    ``argv`` is joined to one human-readable string (flat JSONL scalars, not
    arrays); a ``None`` outcome value (an rc that never existed — timeout,
    launch failure) is DROPPED, honouring ADR-0029's absent-not-null rule.
    No masking here (#277): the central ``redact.redact_event`` processor
    masks every field, on every sink, at format time.
    """
    fields: dict[str, int | str] = {
        "argv": shlex.join(_display_argv(argv)),
        "cwd": str(cwd or "."),
    }
    for key, value in outcome.items():
        if value is not None:
            fields[key] = value
    return fields


def _prompt_summary(text: str) -> str:
    """Stable bounded stand-in for agent prompt/developer-instruction payloads."""
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"<redacted: prompt sha256={digest} chars={len(text)}>"


def _display_argv(argv: list[str] | tuple[str, ...]) -> list[str]:
    """Return an argv safe for durable logs, preserving shape but not prompts.

    Agent CLIs carry large/sensitive prompt material as argv (`claude -p`,
    `codex exec`, `agy --print`) and Codex additionally accepts a
    `developer_instructions=...` config override. The child still receives the
    original argv; only the structured Exec record and its human message use
    this summarized view.
    """
    display = [str(arg) for arg in argv]
    if not display:
        return display

    def redact_after(flag: str) -> None:
        if flag in display:
            index = display.index(flag) + 1
            if index < len(display):
                display[index] = _prompt_summary(display[index])

    binary = os.path.basename(display[0])
    if binary == "claude":
        redact_after("-p")
    elif binary == "codex" and "exec" in display:
        for index, arg in enumerate(display):
            if arg.startswith("developer_instructions="):
                display[index] = "developer_instructions=" + _prompt_summary(
                    arg.split("=", 1)[1]
                )
        # The prompt is the final positional argument in shipit's codex adapter.
        if display:
            display[-1] = _prompt_summary(display[-1])
    elif binary in {"agy", "antigravity"}:
        redact_after("--print")
        for index, arg in enumerate(display):
            if arg.startswith("--print="):
                display[index] = "--print=" + _prompt_summary(arg.split("=", 1)[1])
    return display


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

    ``env``, when given, is MERGED over ``os.environ`` (the common case: add or
    override a few keys). ``replace_env=True`` uses ``env`` as the COMPLETE child
    environment instead — the only way to *remove* an inherited variable (the
    Tree provisioner relies on it to keep a parent's ``PIXI_*`` project pointers
    out of a child operating in a different clone).

    ``check=True`` (the default) raises :class:`ExecError` on a nonzero exit;
    ``check=False`` returns the :class:`ExecResult` whatever the rc — for call
    sites where nonzero is a normal answer, not a failure.

    ``timeout`` defaults to :data:`DEFAULT_TIMEOUT`; pass a larger value (or
    ``None``, explicitly) for a legitimate long-runner. Expiry kills the child
    and raises :class:`ExecError` with ``cause=CAUSE_TIMEOUT`` and whatever
    partial output was captured.

    Stdin (ADR-0020): when no ``input`` is supplied the child's stdin is
    redirected from ``os.devnull`` rather than inheriting the parent's — a
    stdin-reading child (notably ``agy --print``) must get a clean EOF, not
    block forever on an idle inherited pipe. When ``input`` IS given,
    ``subprocess.run`` owns the pipe (passing both is a ValueError).

    ``secret_stdout`` marks this Exec's stdout as secret-bearing (a
    ``doppler ... --plain`` fetch): the returned :class:`ExecResult` still
    carries the real stdout for the caller, but any :class:`ExecError` — most
    sharply a timeout, which captures the partial secret the child had already
    written — carries :data:`SECRET_STDOUT_PLACEHOLDER` in place of stdout, so
    neither the failure record nor a re-logged error can leak it. The value is
    not yet registered with the redactor at this point, so suppression (not
    redaction) is the only safe move.
    """
    # argv is typed list[str], but subprocess.run natively accepts Path/numeric
    # elements — which would later crash redaction (``arg.replace``) or the
    # ``" ".join`` in the record. Coerce once here so both the record and the
    # ``ExecResult.argv`` tuple are honestly strings whatever the caller passed.
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
            # ``input`` and ``stdin`` are mutually exclusive in subprocess.run:
            # pin stdin to DEVNULL only when we are NOT piping input.
            stdin=subprocess.DEVNULL if input is None else None,
            capture_output=True,
            # Decode text mode explicitly with errors="replace": a tool that
            # emits bytes undecodable in the process encoding (git on binary or
            # non-UTF-8 output) would make a bare text=True raise UnicodeDecodeError
            # — a ValueError, caught by neither handler below — bypassing the
            # one-error contract with a raw escape. Replacement keeps every Exec
            # ending in an ExecResult/ExecError, matching _stream_text's defensive
            # decode on the timeout path.
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        # A timeout is the sharp case for secret_stdout: the child was killed
        # mid-write, so exc.stdout holds a partial secret the redactor cannot
        # yet know. Suppress it before it can reach the record or a re-log.
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
        # Normalize EVERY launch-level OS failure into the transport error: a
        # missing binary (FileNotFoundError — the semantically distinct case) or
        # anything else (permissions, a bad cwd). No raw OSError escapes.
        # A missing cwd ALSO raises FileNotFoundError, but names the directory in
        # ``exc.filename``; distinguish it so a bad cwd reports as an OS error,
        # not as a missing binary (which names argv[0]).
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
    # The one record for a completed Exec (DEBUG — success, or a nonzero rc the
    # caller declared normal via check=False). The outcome rides as structured
    # fields (the _record_fields vocabulary → flat JSONL keys) AND inline in the
    # human msg — fields are additive for the machine reader (ADR-0029's "human
    # msg inside" rule). No per-site redaction (#277): the central
    # `redact.redact_event` processor masks EVERY record at format time,
    # on every sink, so masking here would only run the redactor twice. Streams
    # are deliberately absent from success records (bulk, and the secret-bearing
    # channel) — failures carry their tails via _record_failure above.
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
    """Spawn ``argv`` as a DETACHED fire-and-forget child — the seam's one non-Exec.

    A detached child has no completion to normalize — no rc, no streams, no
    duration — so it cannot be an Exec (an Exec runs to completion; ADR-0028)
    and there is no :class:`ExecResult` and no timeout (there is no wait for
    one to bound). It lives HERE anyway so that every ``subprocess`` import in
    shipit stays in this one module and "tool argv built outside its adapter"
    stays a mechanically greppable review defect: ``git grep 'subprocess\\.'
    src/`` matches only ``execrun.py``.

    Detach semantics: ``start_new_session=True`` puts the child in its own
    session/process group, so it survives the parent exiting and has no
    controlling terminal; stdio is pinned to ``/dev/null`` because a detached
    child's diagnostics go to its own durable sink (the OBS01 file sink for
    the review child), not a pipe the parent would have to drain; the handle
    is deliberately not retained and never waited on.

    What parts of the seam's contract DO still apply: one structured record at
    spawn time — argv, cwd, pid, all redacted — so the detached child stays on the
    causal record chain (glassbox PRD story 3), and launch normalization — a
    missing binary or any other OS-level spawn failure raises
    :class:`ExecError` exactly as a failed Exec launch would (``rc=None``,
    ``cause`` of ``missing-binary``/``os-error``, one ERROR record); no raw
    ``OSError`` ever escapes.

    ``env``, when given, is the child's FULL environment (the caller builds it,
    e.g. via :func:`shipit.logcontext.env_export`, so it is the parent's
    environment plus the ``SHIPIT_LOG_CTX_*`` domain keys the child rebinds at
    its logging setup — the ADR-0029 cross-process context seam). ``None``
    inherits the parent's environment unchanged.
    """
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
        # Same launch normalization as :func:`run`: FileNotFoundError naming
        # argv[0] is a missing binary; one naming a bad cwd (or any other
        # OSError) is an os-error. No raw OSError escapes the seam.
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
    # The one record for a detached spawn: what was launched, from where, as
    # what pid — as structured fields (the _record_fields vocabulary: pid in
    # place of rc/duration_ms, since there is no completion) and inline in the
    # human msg. The pid is the only handle a log reader has to correlate the
    # child's own records back to this spawn. No per-site redaction (#277): the
    # central `redact.redact_event` processor masks every record at format time.
    fields = _record_fields(argv, cwd, pid=proc.pid)
    logger.debug(
        "exec-detach %s (cwd=%s) -> pid=%d",
        fields["argv"],
        fields["cwd"],
        proc.pid,
        extra=fields,
    )


def _sanitize_cause(exc: BaseException) -> BaseException:
    """Scrub raw stream payloads off ``exc`` before chaining it as ``__cause__``.

    Every failure path raises ``ExecError from exc``, and the chained cause
    stays reachable via ``err.__cause__`` for as long as the error lives.
    :class:`subprocess.TimeoutExpired` carries the child's raw partial streams
    (``.output``/``.stderr``) — unredacted, and untouched even when
    ``secret_stdout=True`` scrubbed the wrapping :class:`ExecError` (#317).
    Traceback RENDERING happens to be safe today (the sink formatter's
    flattened exception text passes through ``redact_text``, and
    ``TimeoutExpired.__str__`` prints no output), but the seam's contract must
    not depend on how stdlib exceptions happen to stringify: null the
    payload-bearing attributes so ``err.__cause__`` is as safe as ``err``.

    The chain itself is preserved — the cause's type and message ("Command
    '...' timed out after 0.1 seconds") are diagnostic value; only its captured
    streams are the hazard. The command the message names is redacted in place,
    matching the redaction ``ExecError`` applies to its own ``argv``. OS-level
    causes (``FileNotFoundError`` & co.) carry no stream payloads and pass
    through untouched.

    Attribute rewrites alone are not enough: ``BaseException.__new__``
    snapshots the positional constructor arguments onto ``.args``, and
    ``repr(exc)`` renders THAT tuple — so ``.args`` must be rebuilt from the
    sanitized values or the raw ``cmd`` (and any positionally-passed streams)
    leaks straight through the redacted attributes.
    """
    if isinstance(exc, subprocess.TimeoutExpired):
        exc.output = None  # ``.stdout`` is a property over ``.output``
        exc.stderr = None
        # ``cmd`` is a str when the child was launched through a shell; this
        # seam's contract holds for any constructor shape, not just the list
        # argv :func:`run` itself enforces.
        exc.cmd = (
            redact.redact_text(exc.cmd)
            if isinstance(exc.cmd, str)
            else [redact.redact_text(str(arg)) for arg in exc.cmd]
        )
        exc.args = (exc.cmd, exc.timeout, None, None)[: len(exc.args)]
    return exc


def _record_failure(error: ExecError, cwd: str | os.PathLike | None) -> None:
    """The one record for a failed Exec: ERROR, with both stream tails.

    The outcome rides as structured fields (the :func:`_record_fields`
    vocabulary — here including ``cause`` and both stream tails; ``rc`` is
    absent when the child never produced one) and inline in the human msg.
    ``error``'s attributes are redacted at construction — the ERROR object
    surfaces to callers OUTSIDE the logging chain, so that redaction stays. The
    record itself needs no per-site masking (#277): the central
    ``redact.redact_event`` processor masks every record at format time.
    """
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
    """Milliseconds elapsed since ``start`` (a ``time.monotonic`` stamp)."""
    return int((time.monotonic() - start) * 1000)


def _tail(text: str) -> str:
    """The last :data:`TAIL_CHARS` of ``text``, stripped — where diagnostics live."""
    return text[-TAIL_CHARS:].strip()


def _stream_text(stream: str | bytes | None) -> str:
    """Normalize a ``TimeoutExpired`` partial stream to text.

    ``subprocess`` attaches whatever it had read when the timeout struck; the
    type is version- and platform-dependent (``None``, ``bytes`` even in text
    mode, or ``str``), so normalize defensively rather than trusting one shape.
    """
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return stream
