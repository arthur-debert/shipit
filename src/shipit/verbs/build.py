"""``shipit build`` — run the repository's declared build legs."""

from __future__ import annotations

import logging
import shlex
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import config, execrun
from ..release import provisioning as provisioning_mod
from ..tools import build as build_mod
from ..tools import legs as legs_mod
from ._errors import cli_errors
from ._tool import load_config, require_entries, split_args

logger = logging.getLogger("shipit.build")

TOOL = "build"

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
    """``0`` when every step built, ``1`` otherwise."""
    return 0 if all(run.ok for run in runs) else 1


def _run_step(
    argv: Sequence[str], cwd: Path, env: Mapping[str, str]
) -> execrun.ExecResult:
    """Run one step's builder in ``cwd`` with ``env`` merged over the parent's; a nonzero rc is the step's verdict, not an error."""
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
    target: str | None = None,
    run_step: (
        Callable[[Sequence[str], Path, Mapping[str, str]], execrun.ExecResult] | None
    ) = None,
    runs_out: list[StepRun] | None = None,
) -> int:
    """Run the repo's build steps from the current directory; returns 0, 1 or 2."""
    started = time.monotonic()
    root = Path(".").resolve()
    if version is not None and any(ch.isspace() for ch in version):
        print(
            f"error: --version {version!r} contains whitespace; a release "
            "version must be a single token (it rides go's -ldflags -X value, "
            "ADR-0041)",
            file=sys.stderr,
        )
        logger.error("build invocation rejected", extra={"root": str(root)})
        return 2
    cfg = load_config(root)
    entries = require_entries(cfg, root, TOOL)
    selector, passthrough = split_args(tuple(args), entries)
    artifacts = config.load_artifacts(cfg)
    build_mod.check_targets_mapped(artifacts, entries)

    try:
        planned = legs_mod.plan_legs(
            entries, tool=TOOL, selector=selector, passthrough=passthrough
        )
    except legs_mod.LegPlanError as exc:
        print(f"error: {exc}", file=sys.stderr)
        logger.error("build invocation rejected", extra={"root": str(root)})
        return 2

    build_mod.check_targets_unambiguous(artifacts, planned)
    steps = build_mod.plan_build(planned, artifacts, version=version, target=target)
    run_step = run_step or _run_step
    runs: list[StepRun] = []
    for step in steps:
        command = shlex.join(step.argv)
        print(f"build: {step.label}: {command}")
        try:
            result = run_step(step.argv, root / step.leg.path, dict(step.env))
        except execrun.ExecError as exc:
            rc = 127
            if exc.cause == execrun.CAUSE_MISSING_BINARY:
                out = provisioning_mod.missing_tool_remedy(step.argv, exc.cause) or (
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
        if out:
            print(out, end="" if out.endswith("\n") else "\n")
        print(f"  {'ok  ' if rc == 0 else 'FAIL'} {step.label} ({command})")

    if runs_out is not None:
        runs_out.extend(runs)
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
        summary["failed_steps"] = ", ".join(failed)
    logger.info("build complete", extra=summary)
    return rc
