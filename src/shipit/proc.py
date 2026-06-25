"""proc — the generic subprocess runner for the review backends + diff.

The ``review`` package shells out to the ``codex`` / ``agy`` agent CLIs and to
``git`` (for PR-diff resolution). Those are NOT the GitHub boundary (``gh.py``),
so they get their own small, explicit runner here rather than threading every
call through ``gh``. Ported from release-core's ``proc.py``.

Rules: never ``shell=True``; never interpolate into a shell string. Commands are
argument lists.
"""

from __future__ import annotations

import os
import subprocess


class ProcError(RuntimeError):
    """A subprocess exited nonzero (raised by ``run(check=True)``)."""

    def __init__(self, cmd: list[str], returncode: int, stderr: str) -> None:
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"{' '.join(cmd)} failed ({returncode}): {stderr.strip()}")


def run(
    cmd: list[str],
    *,
    cwd: str | os.PathLike | None = None,
    env: dict[str, str] | None = None,
    input: str | None = None,  # noqa: A002 — mirrors subprocess.run's parameter name
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run ``cmd`` (no shell), capturing text stdout/stderr.

    ``env``, when given, is MERGED over ``os.environ`` (not a replacement). On a
    nonzero exit with ``check=True`` raise :class:`ProcError`.
    """
    merged_env = {**os.environ, **env} if env is not None else None
    proc = subprocess.run(  # noqa: S603 — cmd is a constructed list, never shell-interpolated
        cmd,
        cwd=cwd,
        env=merged_env,
        input=input,
        capture_output=capture_output,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise ProcError(cmd, proc.returncode, proc.stderr)
    return proc
