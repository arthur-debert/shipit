"""e2e — run each artifact's declared harness against its built binary."""

from __future__ import annotations

import logging
import os
import shlex
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import config, execrun
from ..tools import artifact_source
from ..tools import build as build_mod
from ..tools import e2e as e2e_mod
from ..tools import legs as legs_mod
from . import build as build_verb
from ._errors import cli_errors
from ._tool import load_config

logger = logging.getLogger("shipit.e2e")

TOOL = "e2e"


def _split_args(args: Sequence[str]) -> tuple[str | None, tuple[str, ...]]:
    """``(selector, passthrough)`` for the ARTIFACT axis: a leading ``-`` token means no selector."""
    if not args or args[0].startswith("-"):
        return None, tuple(args)
    return args[0], tuple(args[1:])


E2E_TIMEOUT: float = 3600.0


@dataclass(frozen=True)
class HarnessRun:
    """The outcome of one e2e job — its harness's verdict, or the hard failure that kept the harness from running."""

    job: e2e_mod.E2eJob
    returncode: int
    output: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def verdict(runs: Sequence[HarnessRun]) -> int:
    """0 when every harness passed, 1 otherwise."""
    return 0 if all(run.ok for run in runs) else 1


def _run_harness(
    argv: Sequence[str], cwd: Path, env: Mapping[str, str]
) -> execrun.ExecResult:
    """Run one job's harness in ``cwd`` with ``env`` merged over the parent's; a nonzero rc is the suite's verdict, a launch failure raises ExecError."""
    return execrun.run(
        list(argv),
        cwd=str(cwd),
        check=False,
        timeout=E2E_TIMEOUT,
        env=dict(env) or None,
    )


def _check_harness_script(root: Path, harness: Sequence[str]) -> None:
    """Raise ConfigError when a script-path harness head is missing or not executable; a bare-name head resolves on PATH at exec time instead."""
    head = harness[0]
    if os.sep not in head and "/" not in head:
        return
    script = Path(head) if os.path.isabs(head) else root / head
    if not script.is_file():
        raise config.ConfigError(
            f"e2e harness script {script} does not exist — declare the real "
            f"harness in [artifacts.<name>].e2e.harness, or add the script"
        )
    if not os.access(script, os.X_OK):
        raise config.ConfigError(
            f"e2e harness script {script} is not executable (chmod +x it)"
        )


@cli_errors
def run(
    args: Sequence[str] = (),
    *,
    source: artifact_source.ArtifactSource | None = None,
    run_harness: (
        Callable[[Sequence[str], Path, Mapping[str, str]], execrun.ExecResult] | None
    ) = None,
    runs_out: list[HarnessRun] | None = None,
) -> int:
    """Run the repo's declared e2e jobs from the current directory; returns 0/1/2. ``runs_out``, when given, receives every HarnessRun outcome."""
    started = time.monotonic()
    root = Path(".").resolve()
    selector, passthrough = _split_args(tuple(args))
    cfg = load_config(root)
    artifacts = config.load_artifacts(cfg)

    try:
        jobs = e2e_mod.plan_e2e(artifacts, selector=selector, passthrough=passthrough)
    except e2e_mod.E2ePlanError as exc:
        print(f"error: {exc}", file=sys.stderr)
        logger.error("e2e invocation rejected", extra={"root": str(root)})
        return 2
    if not jobs:
        print(
            "e2e: no e2e declared — nothing to run "
            "(declare [artifacts.<name>].e2e in .shipit.toml to opt in)"
        )
        logger.info("e2e complete", extra={"root": str(root), "jobs": 0, "rc": 0})
        return 0

    for job in jobs:
        _check_harness_script(root, job.harness)

    if source is None:
        entries = config.load_toolchains(cfg)
        job_artifacts = [job.artifact for job in jobs]
        build_mod.check_targets_mapped(job_artifacts, entries)
        build_mod.check_targets_unambiguous(
            job_artifacts, legs_mod.plan_legs(entries, tool="build")
        )
        for job in jobs:
            e2e_mod.binary_location(job.artifact, entries)
        source = artifact_source.LocalBuildSource(
            root=root,
            entries=entries,
            run_step=build_verb._run_step,
        )
    run_harness = run_harness or _run_harness
    runs: list[HarnessRun] = []
    for job in jobs:
        command = shlex.join(job.harness)
        try:
            binary = source.resolve(job.artifact)
        except artifact_source.ArtifactSourceError as exc:
            runs.append(HarnessRun(job, 1, str(exc)))
            print(str(exc))
            print(f"  FAIL {job.label} ({command})")
            logger.error(
                "e2e artifact could not be resolved",
                exc_info=True,
                extra={"job": job.label, "root": str(root)},
            )
            continue
        injected = {**dict(job.env), job.env_var: str(binary)}
        shown = " ".join(f"{key}={value}" for key, value in injected.items())
        print(f"e2e: {job.label}: {command} [{shown}]")
        try:
            result = run_harness(job.harness, root, injected)
        except execrun.ExecError as exc:
            rc = 127
            if exc.cause == execrun.CAUSE_MISSING_BINARY:
                out = (
                    f"{job.harness[0]}: not found on PATH "
                    "(the check is hard — provision it)"
                )
            else:
                out = f"{job.harness[0]}: could not run: {exc}"
            logger.error(
                "e2e harness could not run",
                exc_info=True,
                extra={"job": job.label, "harness": command, "rc": rc},
            )
        else:
            rc, out = result.rc, result.stdout + result.stderr
            logger.debug(
                "e2e harness finished",
                extra={
                    "job": job.label,
                    "harness": command,
                    "rc": rc,
                    "duration_ms": result.duration_ms,
                },
            )
        runs.append(HarnessRun(job, rc, out))
        if out:
            print(out, end="" if out.endswith("\n") else "\n")
        print(f"  {'ok  ' if rc == 0 else 'FAIL'} {job.label} ({command})")

    if runs_out is not None:
        runs_out.extend(runs)
    rc = verdict(runs)
    failed = [r.job.label for r in runs if not r.ok]
    if rc == 0:
        print(f"E2E: OK ({len(runs)} harness{'es' if len(runs) != 1 else ''})")
    else:
        print(f"E2E: FAILED ({', '.join(failed)})")
    summary = {
        "root": str(root),
        "jobs": len(runs),
        "failed": len(failed),
        "rc": rc,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    if failed:
        summary["failed_jobs"] = ", ".join(failed)
    logger.info("e2e complete", extra=summary)
    return rc
