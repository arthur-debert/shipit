"""``shipit hook sessionstart`` — the coordinator-activation boundary (ADR-0027).

THIN by design (mirrors ``hook pretooluse``): read the ``SessionStart`` payload on
stdin → detect the toolchain governing the session's ``cwd`` → capture pixi's
activation (``pixi shell-hook --json`` via :func:`shipit.pixienv.shell_hook`) →
render it (pure core: :mod:`shipit.harness.activation`) → APPEND the export lines
to the file named by ``CLAUDE_ENV_FILE``, which Claude Code sources as a preamble
before every Bash tool call. Result: the coordinator's environment is active for
every Bash call with no wrapper — ``shipit``/``python`` resolve without a
``pixi run`` prefix.

**Fail-open is the contract** — the same posture as ``hook pretooluse``, the
OPPOSITE of ``hook worktreecreate``. Activation is ADDITIVE, never load-bearing:
the committed ``pixi run shipit hook …`` lines keep their prefix, so nothing
depends on this hook having succeeded. ANY failure (no ``CLAUDE_ENV_FILE``, bad
payload, no toolchain, a pixi error, an unwritable env file) must therefore cost
the session NOTHING: log at DEBUG, write nothing, exit 0. A repo with no
activatable toolchain is a clean no-op by design, not an error.

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

from ... import proc
from ...harness import activation
from ...pixienv import shell_hook

logger = logging.getLogger("shipit.hook")

#: The env var Claude Code sets to the file it sources before each Bash call.
ENV_FILE_VAR = "CLAUDE_ENV_FILE"


@click.command(name="sessionstart")
def cmd() -> None:
    """Write the repo's toolchain activation into ``CLAUDE_ENV_FILE``.

    Reads the ``SessionStart`` payload as JSON on stdin. Always exits 0; fails
    OPEN (writes nothing) on any error, and is a clean no-op in a repo with no
    activatable toolchain.
    """
    raise SystemExit(run())


def run(
    stdin: TextIO | None = None,
    environ: dict[str, str] | None = None,
    runner=proc.run,
) -> int:
    """Parse stdin → detect toolchain → capture activation → append. Returns 0 always.

    ``environ`` and ``runner`` are the injectable boundaries (default the real
    ``os.environ`` / :func:`shipit.proc.run`) so tests assert the written lines
    without a live pixi. Wraps the whole path so a bad payload, a pixi failure, or
    an unwritable env file can never crash the session — fail-open, nothing written.
    """
    env = environ if environ is not None else os.environ
    try:
        env_file = env.get(ENV_FILE_VAR)
        if not env_file:
            logger.debug("sessionstart: no %s in env — nothing to write", ENV_FILE_VAR)
            return 0
        raw = (stdin if stdin is not None else sys.stdin).read()
        toolchain = activation.detect_toolchain(_payload_cwd(raw))
        if toolchain is None:
            logger.debug("sessionstart: no activatable toolchain — clean no-op")
            return 0
        captured = shell_hook(toolchain.manifest, runner=runner)
        script = activation.activation_script(toolchain, captured)
        if not script:
            return 0
        with open(env_file, "a", encoding="utf-8") as handle:
            handle.write(script + "\n")
        logger.debug(
            "sessionstart: wrote %s activation for %s into %s",
            toolchain.kind,
            toolchain.manifest,
            env_file,
        )
    except Exception:  # noqa: BLE001 — fail-open: activation is additive, never load-bearing.
        logger.debug(
            "sessionstart hook failed open (no activation written)", exc_info=True
        )
    return 0


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
        logger.debug("sessionstart: unparseable payload — falling back to cwd")
    return Path.cwd()
