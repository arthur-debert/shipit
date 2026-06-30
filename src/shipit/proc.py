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
    replace_env: bool = False,
    input: str | None = None,  # noqa: A002 — mirrors subprocess.run's parameter name
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run ``cmd`` (no shell), capturing text stdout/stderr.

    ``env``, when given, is MERGED over ``os.environ`` (not a replacement) — the
    common case (add/override a few keys). Set ``replace_env=True`` to use ``env``
    as the COMPLETE child environment instead (the subprocess-native semantics):
    this is the only way to *remove* an inherited variable, since a merge can add
    or override a key but never drop one. The Tree provisioner relies on it to keep
    a parent's ``PIXI_*`` project pointers from leaking into a child operating in a
    different clone. On a nonzero exit with ``check=True`` raise :class:`ProcError`.
    """
    if env is None:
        merged_env = None
    elif replace_env:
        merged_env = env
    else:
        merged_env = {**os.environ, **env}
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
