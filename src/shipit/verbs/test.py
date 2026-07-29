"""``shipit test`` — run the repository's declared test legs."""

from __future__ import annotations

import logging
import shlex
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import execrun
from ..tools import legs as legs_mod
from ._errors import cli_errors
from ._tool import load_config, require_entries, split_args

logger = logging.getLogger("shipit.test")

TOOL = "test"

TEST_TIMEOUT: float = 3600.0


@dataclass(frozen=True)
class LegRun:
    """The outcome of one leg's producing command."""

    leg: legs_mod.Leg
    returncode: int
    output: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def verdict(runs: Sequence[LegRun]) -> int:
    """``0`` when every leg passed, ``1`` otherwise."""
    return 0 if all(run.ok for run in runs) else 1


def _run_leg(argv: Sequence[str], cwd: Path) -> execrun.ExecResult:
    """Run one leg's producing command in ``cwd``; a nonzero rc is the leg's verdict, not an error."""
    return execrun.run(list(argv), cwd=str(cwd), check=False, timeout=TEST_TIMEOUT)


@cli_errors
def run(
    args: Sequence[str] = (),
    *,
    run_leg: Callable[[Sequence[str], Path], execrun.ExecResult] | None = None,
    runs_out: list[LegRun] | None = None,
) -> int:
    """Run the repo's test legs from the current directory; returns 0, 1 or 2."""
    started = time.monotonic()
    root = Path(".").resolve()
    entries = require_entries(load_config(root), root, TOOL)
    selector, passthrough = split_args(tuple(args), entries)

    try:
        planned = legs_mod.plan_legs(
            entries, tool=TOOL, selector=selector, passthrough=passthrough
        )
    except legs_mod.LegPlanError as exc:
        print(f"error: {exc}", file=sys.stderr)
        logger.error("test invocation rejected", extra={"root": str(root)})
        return 2

    run_leg = run_leg or _run_leg
    runs: list[LegRun] = []
    for leg in planned:
        command = shlex.join(leg.argv)
        print(f"test: {leg.label}: {command}")
        try:
            result = run_leg(leg.argv, root / leg.path)
        except execrun.ExecError as exc:
            rc = 127
            if exc.cause == execrun.CAUSE_MISSING_BINARY:
                out = (
                    f"{leg.argv[0]}: not found on PATH "
                    "(the check is hard — provision it)"
                )
            else:
                out = f"{leg.argv[0]}: could not run: {exc}"
            logger.error(
                "test leg could not run",
                exc_info=True,
                extra={
                    "leg": leg.label,
                    "tool_argv": command,
                    "rc": rc,
                    "cwd": leg.path,
                },
            )
        else:
            rc, out = result.rc, result.stdout + result.stderr
            logger.debug(
                "test leg finished",
                extra={
                    "leg": leg.label,
                    "tool_argv": command,
                    "rc": rc,
                    "cwd": leg.path,
                    "duration_ms": result.duration_ms,
                },
            )
        runs.append(LegRun(leg, rc, out))
        if out:
            print(out, end="" if out.endswith("\n") else "\n")
        print(f"  {'ok  ' if rc == 0 else 'FAIL'} {leg.label} ({command})")

    if runs_out is not None:
        runs_out.extend(runs)
    rc = verdict(runs)
    failed = [r.leg.label for r in runs if not r.ok]
    if rc == 0:
        print(f"TEST: OK ({len(runs)} leg{'s' if len(runs) != 1 else ''})")
    else:
        print(f"TEST: FAILED ({', '.join(failed)})")
    summary = {
        "root": str(root),
        "legs": len(runs),
        "failed": len(failed),
        "rc": rc,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    if failed:
        summary["failed_legs"] = ", ".join(failed)
    logger.info("test complete", extra=summary)
    return rc
