"""``shipit hook sessionstart`` — the coordinator-activation boundary (ADR-0027).

THIN by design (mirrors ``hook pretooluse``); two independent, additive writes per
session start:

1. **Activation** — detect the toolchain governing the session's ``cwd`` → capture
   pixi's activation (``pixi shell-hook --json`` via
   :func:`shipit.pixienv.shell_hook`) → render it (pure core:
   :mod:`shipit.harness.activation`) → APPEND the export lines to the file named
   by ``CLAUDE_ENV_FILE``, which Claude Code sources as a preamble before every
   Bash tool call. Result: the coordinator's environment is active for every Bash
   call with no wrapper — ``shipit``/``python`` resolve without a ``pixi run``
   prefix.
2. **Liveness** (SES02) — record which Claude session owns this Tree: walk the
   hook's own ancestry to the ``claude`` process (the hook runs as its
   great-grandchild: claude → shell → ``pixi run`` → ``shipit``) and write the
   :mod:`shipit.session.liveness` pidfile — PID, payload ``session_id``, and the
   PID's OS create-time, read NOW, at write time — into the Tree's ``.git`` dir.
   This is the signal the ephemeral-Tree gc ladder consults so an idle-but-live
   session's Tree is never reclaimed out from under it.

**Fail-open is the contract** — the same posture as ``hook pretooluse``, the
OPPOSITE of ``hook worktreecreate``. Both writes are ADDITIVE, never load-bearing:
the committed ``pixi run shipit hook …`` lines keep their prefix, and the gc
ladder's liveness-independent rungs (the dirty/unpushed floor, the grace window,
the hard cap) carry teardown safety even with no pidfile. ANY failure in either
step (no ``CLAUDE_ENV_FILE``, bad payload, no toolchain, a pixi error, an
unwritable file, no claude ancestor) must therefore cost the session NOTHING:
skip that write and exit 0 — and the steps fail open INDEPENDENTLY, so a broken
activation never costs the session its liveness record or vice versa. Levels
follow the fail-open canon in :mod:`shipit.verbs.hook`: a swallowed exception is
a degraded-but-continuing outcome and logs at WARNING; a clean no-op (no
``CLAUDE_ENV_FILE``, no toolchain, no claude ancestor, not a clone) is mechanics
and stays at DEBUG.

The env file is opened in APPEND mode: ``CLAUDE_ENV_FILE`` is a shared seam other
SessionStart hooks may also write to, and this boundary owns only its own lines —
never the whole file.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import TextIO

import click

from ... import execrun
from ...harness import activation
from ...pixienv import shell_hook
from ...session import liveness

logger = logging.getLogger("shipit.hook")

#: The env var Claude Code sets to the file it sources before each Bash call.
ENV_FILE_VAR = "CLAUDE_ENV_FILE"


@click.command(name="sessionstart")
def cmd() -> None:
    """Write the repo's toolchain activation into ``CLAUDE_ENV_FILE`` + the pidfile.

    Reads the ``SessionStart`` payload as JSON on stdin. Always exits 0; each of
    the two writes (activation, liveness pidfile) fails OPEN independently on any
    error, and a repo with no activatable toolchain / no claude ancestor is a
    clean no-op for that write.
    """
    raise SystemExit(run())


def run(
    stdin: TextIO | None = None,
    environ: dict[str, str] | None = None,
    runner=execrun.run,
    probe: liveness.Probe | None = None,
    self_pid: int | None = None,
) -> int:
    """Parse stdin → write activation → write the liveness pidfile. Returns 0 always.

    ``environ``, ``runner``, ``probe``, and ``self_pid`` are the injectable
    boundaries (defaults: the real ``os.environ`` / :func:`shipit.execrun.run` /
    :func:`shipit.session.liveness.os_probe` / ``os.getpid()``) so tests assert
    both writes without a live pixi or a real claude process tree. Each write is
    wrapped fail-open on its own, so a bad payload, a pixi failure, an unwritable
    env file, or a probe error can never crash the session — and a failure in one
    write never suppresses the other.
    """
    env = environ if environ is not None else os.environ
    try:
        raw = (stdin if stdin is not None else sys.stdin).read()
    except Exception:  # noqa: BLE001 — fail-open: no payload, nothing to do.
        logger.warning("sessionstart: could not read the payload", exc_info=True)
        return 0
    _write_activation(raw, env, runner)
    _write_liveness(raw, probe=probe, self_pid=self_pid)
    return 0


def _write_activation(raw: str, env, runner) -> None:
    """The activation half: toolchain → captured env → append to CLAUDE_ENV_FILE.

    Fail-open in isolation: any error logs at WARNING (the swallow is a degraded
    outcome) and writes nothing, without touching the liveness half.
    """
    try:
        env_file = env.get(ENV_FILE_VAR)
        if not env_file:
            logger.debug("sessionstart: no %s in env — nothing to write", ENV_FILE_VAR)
            return
        toolchain = activation.detect_toolchain(_payload_cwd(raw))
        if toolchain is None:
            logger.debug("sessionstart: no activatable toolchain — clean no-op")
            return
        captured = shell_hook(toolchain.manifest, runner=runner)
        script = activation.activation_script(toolchain, captured)
        if not script:
            return
        _append(Path(env_file), script + "\n")
        logger.debug(
            "sessionstart: wrote %s activation for %s into %s",
            toolchain.kind,
            toolchain.manifest,
            env_file,
        )
    except Exception:  # noqa: BLE001 — fail-open: activation is additive, never load-bearing.
        logger.warning(
            "sessionstart hook failed open (no activation written)", exc_info=True
        )


def _write_liveness(
    raw: str, *, probe: liveness.Probe | None, self_pid: int | None
) -> None:
    """The liveness half: find the claude ancestor, write the pidfile into the Tree.

    The recorded PID is NOT this hook's own — the hook runs as a great-grandchild
    of the session (claude → shell → ``pixi run`` → ``shipit``) — but the nearest
    ancestor whose command line looks like Claude Code
    (:func:`~shipit.session.liveness.find_claude_process`); its create-time is
    read from the OS here, at write time, exactly as ADR-0027 specifies. Skipped
    cleanly (DEBUG log, no pidfile) when the session's cwd is not a git clone
    (nowhere durable to record), no claude ancestor is found (launched outside a
    session), or the ancestor's create-time is unreadable (a record ``is_live``
    could never verify would only ever read as dead). Fail-open in isolation.
    """
    try:
        tree = _payload_cwd(raw)
        if not (tree / ".git").is_dir():
            logger.debug("sessionstart: %s is not a clone — no pidfile written", tree)
            return
        info = liveness.find_claude_process(
            self_pid if self_pid is not None else os.getpid(),
            probe if probe is not None else liveness.os_probe,
        )
        if info is None or info.create_time is None:
            logger.debug(
                "sessionstart: no claude ancestor with a readable create-time — "
                "no pidfile written"
            )
            return
        record = liveness.LivenessRecord(
            pid=info.pid,
            session_id=_payload_session_id(raw),
            create_time=info.create_time,
        )
        liveness.write_pidfile(tree, record)
        logger.debug(
            "sessionstart: recorded session pid %s in %s",
            info.pid,
            liveness.pidfile_path(tree),
        )
    except Exception:  # noqa: BLE001 — fail-open: liveness is additive; the gc ladder's
        # liveness-independent rungs carry teardown safety without it.
        logger.warning(
            "sessionstart hook failed open (no pidfile written)", exc_info=True
        )


def _append(env_file: Path, text: str) -> None:
    """Append ``text``, rolling the env file back to its prior state on failure.

    The env file is sourced before EVERY subsequent Bash call, so a torn append
    (disk full, transient I/O error) is WORSE than none: a truncated ``export``
    line — an unterminated quote — would corrupt the whole session's preamble.
    "Write nothing" on failure therefore means exactly that: on any write error,
    best-effort restore the file to its pre-hook bytes (truncate back, or remove
    it if this hook created it), then re-raise into the fail-open boundary.
    """
    # One stat() answers existence AND size atomically — an exists()/stat() pair
    # would race a concurrent delete between the two calls (TOCTOU).
    try:
        original_size: int | None = env_file.stat().st_size
    except FileNotFoundError:
        original_size = None
    try:
        with open(env_file, "a", encoding="utf-8") as handle:
            handle.write(text)
    except Exception:
        try:
            if original_size is not None:
                os.truncate(env_file, original_size)
            else:
                env_file.unlink(missing_ok=True)
        except OSError:
            # A torn append that could not be rolled back may leave a corrupt
            # preamble — degraded-but-continuing, so WARNING per the canon.
            logger.warning(
                "sessionstart: could not roll back partial append to %s",
                env_file,
                exc_info=True,
            )
        raise


def _payload_session_id(raw: str) -> str:
    """The payload's ``session_id``, or ``""`` when missing/malformed.

    The id is recorded for a human joining a Tree back to its transcript — the
    liveness decision never consults it (the OS cannot be asked for it), so a
    missing id degrades to an empty string rather than blocking the pidfile.
    """
    try:
        payload = json.loads(raw)
        sid = payload.get("session_id") if isinstance(payload, dict) else None
        if isinstance(sid, str):
            return sid
    except ValueError:
        logger.warning("sessionstart: unparseable payload — no session id recorded")
    return ""


def _payload_cwd(raw: str) -> Path:
    """The session's working dir from the payload, else the hook process's own cwd.

    Claude Code's ``SessionStart`` payload carries ``cwd`` (the session's root —
    the adopted session Tree once ADR-0027's ``--worktree`` launch lands). Hooks
    also RUN in the project dir, so a missing/malformed payload degrades to
    ``Path.cwd()`` rather than aborting — the manifest still resolves.
    """
    try:
        payload = json.loads(raw)
        cwd = payload.get("cwd") if isinstance(payload, dict) else None
        if isinstance(cwd, str) and cwd:
            return Path(cwd)
    except ValueError:
        logger.warning("sessionstart: unparseable payload — falling back to cwd")
    return Path.cwd()
