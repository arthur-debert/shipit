"""``shipit hook sessionstart`` — the coordinator-activation boundary (ADR-0027).

THIN by design (mirrors ``hook pretooluse``); three independent, additive checks
per session start — two writes and one advisory emit:

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
3. **Source-clone warning** (REL01 #348) — when the session's ``cwd`` is a shipit
   *source clone* (has ``.shipit.toml``, is a git repo) rather than a Tree (any
   dir under :func:`shipit.tree.layout.central_root`), print a one-line warning
   on stdout. A SessionStart hook's stdout is added to the session's context, so
   the coordinator sees it and can relay it; a WARNING log record rides along as
   the durable trail. The direct launch stays fully supported (``claude -w
   <name>`` without the launcher is an explicit path, per the ``claude-start``
   header) — this is a nudge, never a block. The discriminator is the PATH, not
   the branch: session Trees are *ephemeral-by-path, work-by-branch* (ADR-0027),
   so their branch moves off ``ephemeral/*`` mid-session and would false-positive,
   while every Tree kind (ephemeral, write, review) lives under the central root
   by construction.

**Fail-open is the contract** — the same posture as ``hook pretooluse``, the
OPPOSITE of ``hook worktreecreate``. Both writes are ADDITIVE, never load-bearing:
the committed ``pixi run shipit hook …`` lines keep their prefix, and the gc
ladder's liveness-independent rungs (the dirty/unpushed floor, the grace window,
the hard cap) carry teardown safety even with no pidfile. ANY failure in either
step (no ``CLAUDE_ENV_FILE``, bad payload, no toolchain, a pixi error, an
unwritable file, no claude ancestor) must therefore cost the session NOTHING:
skip that write and exit 0 — and the steps fail open INDEPENDENTLY, so a broken
activation never costs the session its liveness record or vice versa. The
source-clone warning is fail-open too, with one deliberate calibration exception
(#348): a detection error skips at DEBUG, not the canon's WARNING — the check
writes nothing durable, so there is no degraded state to flag, and a broken
detection environment would otherwise WARN on every session start for a purely
advisory nudge. Levels
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

from ... import config, execrun
from ...harness import activation
from ...pixienv import shell_hook
from ...session import liveness
from ...tree import layout

logger = logging.getLogger("shipit.hook")

#: The env var Claude Code sets to the file it sources before each Bash call.
ENV_FILE_VAR = "CLAUDE_ENV_FILE"

#: The advisory printed (stdout → session context) when the session lands in a
#: source clone instead of a Tree (REL01 #348). One line, actionable: the fix is
#: a relaunch through the launcher (or the equivalent bare ``claude -w``, which
#: fires the same WorktreeCreate isolation path).
SOURCE_CLONE_WARNING = (
    "shipit: you launched claude directly in the source clone — this session has "
    "no isolated Tree. Restart via ./claude-start (or claude -w <name>)."
)


@click.command(name="sessionstart")
def cmd() -> None:
    """Write the repo's toolchain activation into ``CLAUDE_ENV_FILE`` + the pidfile.

    Reads the ``SessionStart`` payload as JSON on stdin. Always exits 0; each of
    the three checks (activation, liveness pidfile, source-clone warning) fails
    OPEN independently on any error, and a repo with no activatable toolchain /
    no claude ancestor / a cwd that is not a source clone is a clean no-op for
    that check.
    """
    raise SystemExit(run())


def run(
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    environ: dict[str, str] | None = None,
    runner=execrun.run,
    probe: liveness.Probe | None = None,
    self_pid: int | None = None,
) -> int:
    """Parse stdin → warn on a source-clone cwd → write activation → write the
    liveness pidfile. Returns 0 always.

    ``stdout``, ``environ``, ``runner``, ``probe``, and ``self_pid`` are the
    injectable boundaries (defaults: the real ``sys.stdout`` / ``os.environ`` /
    :func:`shipit.execrun.run` / :func:`shipit.session.liveness.os_probe` /
    ``os.getpid()``) so tests assert all three checks without a live pixi or a
    real claude process tree. Each check is wrapped fail-open on its own, so a
    bad payload, a pixi failure, an unwritable env file, a probe error, or a
    detection error can never crash the session — and a failure in one check
    never suppresses the others.
    """
    env = environ if environ is not None else os.environ
    out = stdout if stdout is not None else sys.stdout
    try:
        raw = (stdin if stdin is not None else sys.stdin).read()
    except Exception:  # noqa: BLE001 — fail-open: no payload, nothing to do.
        logger.warning("sessionstart: could not read the payload", exc_info=True)
        return 0
    _warn_source_clone(raw, out)
    _write_activation(raw, env, runner)
    _write_liveness(raw, probe=probe, self_pid=self_pid)
    return 0


def _warn_source_clone(raw: str, out: TextIO) -> None:
    """The advisory third check: warn when the session landed in a source clone.

    A SessionStart hook's stdout is added to the session's context, so the line
    reaches the coordinator (and the transcript); the WARNING log record is the
    durable trail. Fail-open in isolation — but unlike the two writes, a
    detection error here skips at DEBUG, not WARNING (#348's explicit
    calibration): the check writes nothing durable, so there is no degraded
    state to flag, and a broken detection environment (e.g. a bad
    ``SHIPIT_TREES_ROOT``) would otherwise WARN on every session start for a
    purely advisory nudge.
    """
    try:
        cwd = _payload_cwd(raw)
        if not _is_source_clone(cwd):
            return
        out.write(SOURCE_CLONE_WARNING + "\n")
        logger.warning(
            "sessionstart: session launched directly in the source clone %s — "
            "no isolated Tree (restart via claude-start)",
            cwd,
        )
    except Exception:  # noqa: BLE001 — fail-open, DEBUG by design: advisory-only,
        # nothing durable degrades when the detection itself breaks (see docstring).
        logger.debug(
            "sessionstart: source-clone detection failed open — no warning emitted",
            exc_info=True,
        )


def _is_source_clone(cwd: Path) -> bool:
    """Whether ``cwd`` is a shipit SOURCE CLONE rather than a Tree (or neither).

    A source clone has ``.shipit.toml`` at its root and is a git repo (``.git``
    dir or worktree file). What separates it from a Tree — which, being a clone
    of the same repo, carries both markers too — is the PATH: every Tree kind
    (ephemeral, write, review) lives under :func:`shipit.tree.layout.central_root`
    by construction ("the path IS the signal", ADR-0018/0027). The branch is NOT
    consulted: session Trees are *ephemeral-by-path, work-by-branch*, so their
    branch moves off ``ephemeral/*`` mid-session and would false-positive, and a
    git call would cost a subprocess where two stats do.

    Both sides are resolved before comparing so a symlinked home or central root
    (macOS ``/tmp`` → ``/private/tmp`` and friends) cannot split one dir into
    "inside" and "outside" spellings. Only the session root itself is checked —
    a launch from a SUBDIR of the clone is not detected; the payload ``cwd`` is
    the session's root, and a fail-open advisory prefers a false negative over a
    directory walk.
    """
    if not (cwd / config.CONFIG_NAME).is_file():
        return False
    if not (cwd / ".git").exists():
        return False
    return not cwd.resolve().is_relative_to(layout.central_root().resolve())


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
        logger.warning(
            "sessionstart: unparseable payload — no session id recorded",
            exc_info=True,
        )
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
        logger.warning(
            "sessionstart: unparseable payload — falling back to cwd",
            exc_info=True,
        )
    return Path.cwd()
