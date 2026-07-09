"""build — the second tree-input Tool verb (ADR-0039, TOL01-WS02).

``shipit build`` walks the repo's declared path→toolchain map (``.shipit.toml
[toolchains]``, ADR-0007) exactly as ``shipit test`` does — same leg planner,
same selector/passthrough rules — and dispatches each build Leg to the REAL
builder: cargo for rust, ``go build`` for go, ``uv build`` for python, the
package build script for npm. Pixi provisions the toolchains but is never the
build backend (PRD story 9): every step execs the builder directly. One local
``shipit build`` performs the single-target build a legacy CI matrix job
performed; the per-OS matrix itself is CI routing and belongs to TOL02.

The ``[artifacts]`` map (:func:`shipit.config.load_artifacts`) narrows legs
to per-artifact targets — the join lives in the pure planner,
:func:`shipit.tools.build.plan_build`, along with go's ``CGO_ENABLED=0`` env
and the ADR-0041 version injection (``--version`` supplied by the caller,
never computed; absent → the binary keeps its embedded default). This module
is the effectful shell over that plan, sharing its rim with ``shipit test``
(:mod:`._tool`: arg splitting, the map read, the pointed missing-map error):

- ``shipit build [LEG] [--version V] [-- ARGS…]``; passthrough forwards
  VERBATIM to the selected leg's builder and requires exactly one selected
  leg (ADR-0039 — never a broadcast).
- execution through the one Exec runner (:mod:`shipit.execrun`, ADR-0028):
  each step runs with cwd at its leg's map path, its extra env merged over
  the parent's, ``check=False`` (a nonzero rc is the build's verdict) and a
  stated :data:`BUILD_TIMEOUT` — builds are legitimate long-runners, so the
  bound is deliberately generous. A missing builder binary HARD-fails the
  step (127 + a provision note), never a silent skip.
- the uniform exit contract (ADR-0030): 0 = every step built, 1 = any step
  failed (or could not run), 2 = usage. A missing/malformed map raises
  :class:`~shipit.config.ConfigError`, mapped by the shared
  :func:`~._errors.cli_errors` shell to ``error: …`` + exit 1.

Like ``test`` (and unlike lint), a step's output prints VERBATIM even when it
succeeds: the builder's report (what was compiled, where the artifact landed)
is the point of running a build.
"""

from __future__ import annotations

import logging
import shlex
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import config, execrun
from ..tools import build as build_mod
from ..tools import legs as legs_mod
from ._errors import cli_errors
from ._tool import load_config, require_entries, split_args

logger = logging.getLogger("shipit.build")

#: The tool slot this verb fills — the ``[toolchains]`` override key and the
#: registry slot (:data:`shipit.tools.registry.TOOL_BUILD`).
TOOL = "build"

#: Each step Exec's stated timeout, in seconds (ADR-0028: every Exec states
#: its bound deliberately). One hour, matching :data:`~.test.TEST_TIMEOUT`
#: for the same reason: a release build legitimately compiles a whole
#: workspace cold, so the lint checks' 5-minute bound would kill healthy
#: builds.
BUILD_TIMEOUT: float = 3600.0


@dataclass(frozen=True)
class StepRun:
    """The outcome of one build step's builder command."""

    step: build_mod.BuildStep
    returncode: int
    output: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def verdict(runs: Sequence[StepRun]) -> int:
    """``0`` when every step built, ``1`` otherwise — the whole exit
    contract's non-usage half (ADR-0030; usage errors exit 2 before any step
    runs)."""
    return 0 if all(run.ok for run in runs) else 1


def _run_step(
    argv: Sequence[str], cwd: Path, env: Mapping[str, str]
) -> execrun.ExecResult:
    """Run one build step's builder command in ``cwd`` through the one Exec
    runner, ``env`` merged over the parent's (go's ``CGO_ENABLED=0``).

    ``check=False``: a nonzero rc is the step's *verdict*. A launch failure —
    the builder binary missing from PATH, or any OS-level error — raises
    :class:`~shipit.execrun.ExecError`, which the orchestrator renders as the
    hard-fail 127 (a missing builder NEVER skips). The Exec states
    :data:`BUILD_TIMEOUT`; a wedged build dies at that bound as the same
    hard fail.
    """
    return execrun.run(
        list(argv),
        cwd=str(cwd),
        check=False,
        timeout=BUILD_TIMEOUT,
        env=dict(env) or None,
    )


@cli_errors
def run(
    args: Sequence[str] = (),
    *,
    version: str | None = None,
    run_step: (
        Callable[[Sequence[str], Path, Mapping[str, str]], execrun.ExecResult] | None
    ) = None,
    runs_out: list[StepRun] | None = None,
) -> int:
    """Run the repo's build steps from the current directory. Returns 0/1/2.

    ``args`` is the raw post-``--``-stripped argument tuple (see
    :func:`~._tool.split_args`); ``version`` is the caller-supplied release
    version (ADR-0041 — never computed here). ``run_step`` injects the Exec
    boundary for tests; ``runs_out``, when given, receives every
    :class:`StepRun` outcome — the typed per-step verdicts behind the exit
    code.
    """
    started = time.monotonic()
    root = Path(".").resolve()
    selector, passthrough = split_args(tuple(args))
    cfg = load_config(root)
    entries = require_entries(cfg, root, TOOL)
    artifacts = config.load_artifacts(cfg)
    build_mod.check_targets_mapped(artifacts, entries)

    try:
        planned = legs_mod.plan_legs(
            entries, tool=TOOL, selector=selector, passthrough=passthrough
        )
    except legs_mod.LegPlanError as exc:
        # The usage tier (ADR-0030, rc 2): the invocation — not the repo, not
        # a builder — is wrong, and the message is the whole diagnosis.
        print(f"error: {exc}", file=sys.stderr)
        logger.error("build invocation rejected", extra={"root": str(root)})
        return 2

    steps = build_mod.plan_build(planned, artifacts, version=version)
    run_step = run_step or _run_step
    runs: list[StepRun] = runs_out if runs_out is not None else []
    for step in steps:
        command = shlex.join(step.argv)
        print(f"build: {step.label}: {command}")
        try:
            result = run_step(step.argv, root / step.leg.path, dict(step.env))
        except execrun.ExecError as exc:
            # A binary missing from PATH (or any launch failure) is the
            # HARD-fail signal: 127 + a clear note, never a silent skip.
            rc = 127
            if exc.cause == execrun.CAUSE_MISSING_BINARY:
                out = (
                    f"{step.argv[0]}: not found on PATH "
                    "(the check is hard — provision it)"
                )
            else:
                out = f"{step.argv[0]}: could not run: {exc}"
            logger.error(
                "build step could not run",
                exc_info=True,
                extra={
                    "step": step.label,
                    "tool_argv": command,
                    "rc": rc,
                    "cwd": step.leg.path,
                },
            )
        else:
            rc, out = result.rc, result.stdout + result.stderr
            logger.debug(
                "build step finished",
                extra={
                    "step": step.label,
                    "tool_argv": command,
                    "rc": rc,
                    "cwd": step.leg.path,
                    "duration_ms": result.duration_ms,
                },
            )
        runs.append(StepRun(step, rc, out))
        if out.strip():
            print(out.rstrip())
        print(f"  {'ok  ' if rc == 0 else 'FAIL'} {step.label} ({command})")

    rc = verdict(runs)
    failed = [r.step.label for r in runs if not r.ok]
    if rc == 0:
        print(f"BUILD: OK ({len(runs)} step{'s' if len(runs) != 1 else ''})")
    else:
        print(f"BUILD: FAILED ({', '.join(failed)})")
    summary = {
        "root": str(root),
        "steps": len(runs),
        "failed": len(failed),
        "rc": rc,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    if failed:
        # Present only when meaningful — the absent-not-null record contract.
        summary["failed_steps"] = ", ".join(failed)
    logger.info("build complete", extra=summary)
    return rc
