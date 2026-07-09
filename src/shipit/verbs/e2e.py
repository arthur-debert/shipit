"""e2e — the artifact-consuming Tool verb (ADR-0039, TOL01-WS03).

Where ``shipit test`` takes the tree as input, ``shipit e2e`` takes a built
**Artifact**: for every artifact whose ``.shipit.toml`` declares an ``e2e``
table (:class:`shipit.config.E2eSpec` — DECLARING it at all is the opt-in,
PRD story 11), the verb resolves the artifact's binary through the
artifact-source seam (:mod:`shipit.tools.artifact_source` — the WF02
boundary; the one source today is the local build, PRD story 12), injects
its absolute path into the declared harness's environment as ``<NAME>_BIN``
(uppercased artifact name, ``-`` → ``_`` — the legacy fleet's contract,
kept deliberately), and runs the harness from the repo root. A repo with no
e2e declaration has NO e2e lane: the verb reports it and exits 0 — opting
out is the absence of config, never a flag.

The pure planning — which artifacts run, with what harness argv and env
var, where the binary is expected — lives in :mod:`shipit.tools.e2e`; this
module is the effectful shell, sharing its rim with ``test``/``build``
(:mod:`._tool`: arg splitting, the config read):

- ``shipit e2e [ARTIFACT] [-- ARGS…]``; passthrough forwards VERBATIM to
  the selected artifact's harness and requires exactly one selected
  artifact (ADR-0039 — never a broadcast).
- the harness runs through the one Exec runner (:mod:`shipit.execrun`,
  ADR-0028): cwd at the repo root, ``<NAME>_BIN`` merged over the parent
  env, ``check=False`` (a nonzero rc is the SUITE's verdict, not a
  transport error) and a stated :data:`E2E_TIMEOUT` — an e2e suite is a
  legitimate long-runner, so the bound is deliberately generous. A
  script-path harness (the default ``bin/check-e2e``) that is missing or
  not executable is a hard error naming the path (legacy ``bats-e2e.yml``
  parity), checked for every job BEFORE any build runs; a PATH-resolved
  harness binary that is absent hard-fails the job (127), never a skip.
- the uniform exit contract (ADR-0030), shared with ``test``/``build``:
  0 = every harness passed, 1 = any harness failed or its artifact could
  not be produced, 2 = usage. Config inconsistencies (a malformed map, an
  e2e artifact with no binary-producing build target, an orphaned or
  ambiguous build target) raise :class:`~shipit.config.ConfigError`, mapped
  by the shared :func:`~._errors.cli_errors` shell to ``error: …`` + exit 1
  — validated UP FRONT over every job (fail-fast, parity with ``shipit
  build``), so a broken declaration is refused before any build runs, never
  after the healthy jobs ahead of it have already built.

Like ``test`` and ``build``, output prints VERBATIM even on green: the
suite's report is the point of running it.

**Bespoke-lane boundary** (CONTEXT.md "e2e", pinned here on purpose):
environment-heavy test jobs — supage's Firestore-emulator run — are bespoke
**Lanes**, NOT e2e; this verb and its registries grow no environment-setup
hooks, ever. e2e's whole contract is binary-in, harness-verdict-out.
"""

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

#: The tool's name in every user-facing message.
TOOL = "e2e"


def _split_args(args: Sequence[str]) -> tuple[str | None, tuple[str, ...]]:
    """``(selector, passthrough)`` for the ARTIFACT axis — e2e's own split,
    deliberately NOT :func:`._tool.split_args` (the leg axis).

    The rule is the simple one: a leading ``-`` token means no selector (all
    passthrough — click has already stripped the first ``--``); otherwise the
    first token is the artifact selector and the rest is passthrough. Unlike
    the leg axis, e2e has NO single-unit sugar that forwards an unrecognised
    first token as passthrough: an artifact name that names no e2e-declaring
    artifact is a usage error (:func:`shipit.tools.e2e.plan_e2e` raises), never
    a silent hand-off to the harness — so validation stays wholly in the pure
    planner and the split needs no artifact map here.
    """
    if not args or args[0].startswith("-"):
        return None, tuple(args)
    return args[0], tuple(args[1:])


#: The harness Exec's stated timeout, in seconds (ADR-0028: every Exec
#: states its bound deliberately). One hour, matching
#: :data:`~.test.TEST_TIMEOUT` / :data:`~.build.BUILD_TIMEOUT`: an e2e suite
#: is a legitimate long-runner (it drives the real binary end to end), so
#: the lint checks' 5-minute bound would kill healthy suites.
E2E_TIMEOUT: float = 3600.0


@dataclass(frozen=True)
class HarnessRun:
    """The outcome of one e2e job — its harness's verdict, or the hard
    failure that kept the harness from running (build failed, binary
    missing, harness launch failure)."""

    job: e2e_mod.E2eJob
    returncode: int
    output: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def verdict(runs: Sequence[HarnessRun]) -> int:
    """``0`` when every harness passed, ``1`` otherwise — the whole exit
    contract's non-usage half (ADR-0030; usage errors exit 2 before any
    build or harness runs)."""
    return 0 if all(run.ok for run in runs) else 1


def _run_harness(
    argv: Sequence[str], cwd: Path, env: Mapping[str, str]
) -> execrun.ExecResult:
    """Run one job's harness in ``cwd`` (the repo root) through the one Exec
    runner, ``env`` — the ``<NAME>_BIN`` injection — merged over the
    parent's.

    ``check=False``: a nonzero rc is the suite's *verdict* (ADR-0028's
    check=False shape — the harness verdict is the tool's verdict, not a
    transport error). A launch failure raises
    :class:`~shipit.execrun.ExecError`, rendered by the orchestrator as the
    hard-fail 127. The Exec states :data:`E2E_TIMEOUT`; a wedged suite dies
    at that bound as the same hard fail.
    """
    return execrun.run(
        list(argv),
        cwd=str(cwd),
        check=False,
        timeout=E2E_TIMEOUT,
        env=dict(env) or None,
    )


def _check_harness_script(root: Path, harness: Sequence[str]) -> None:
    """The legacy hard error, kept: a SCRIPT-path harness head (it contains
    a path separator — the default ``bin/check-e2e``, a declared
    ``tests/run-e2e``) that is missing or not executable raises
    :class:`~shipit.config.ConfigError` naming the path. A bare-name head
    (``bats``) resolves on PATH at exec time instead, where a missing
    binary hard-fails the job (127)."""
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
    """Run the repo's declared e2e jobs from the current directory. Returns
    0/1/2 (0 also for a BARE invocation when no artifact declares e2e —
    nothing to run is a clean outcome, not a failure; but an explicit
    artifact selector that names no e2e-declaring artifact is usage, exit 2).

    ``args`` is the raw post-``--``-stripped argument tuple (see
    :func:`_split_args` — the artifact axis, not the leg axis). ``source``
    injects the artifact-source
    seam (default: the local-build source over the repo's ``[toolchains]``
    legs); ``run_harness`` injects the harness Exec boundary for tests;
    ``runs_out``, when given, receives every :class:`HarnessRun` outcome —
    the typed per-job verdicts behind the exit code.
    """
    started = time.monotonic()
    root = Path(".").resolve()
    selector, passthrough = _split_args(tuple(args))
    cfg = load_config(root)
    artifacts = config.load_artifacts(cfg)

    try:
        jobs = e2e_mod.plan_e2e(artifacts, selector=selector, passthrough=passthrough)
    except e2e_mod.E2ePlanError as exc:
        # The usage tier (ADR-0030, rc 2): the invocation — not the repo,
        # not a harness — is wrong, and the message is the whole diagnosis.
        print(f"error: {exc}", file=sys.stderr)
        logger.error("e2e invocation rejected", extra={"root": str(root)})
        return 2
    if not jobs:
        # PRD story 11: no declaration -> no e2e lane. A report, not an error.
        print(
            "e2e: no e2e declared — nothing to run "
            "(declare [artifacts.<name>].e2e in .shipit.toml to opt in)"
        )
        logger.info("e2e complete", extra={"root": str(root), "jobs": 0, "rc": 0})
        return 0

    # Legacy parity, fail-fast: every job's script-path harness is checked
    # BEFORE any (expensive) build runs.
    for job in jobs:
        _check_harness_script(root, job.harness)

    if source is None:
        # Default local-build source: validate every job's build declaration UP
        # FRONT — fail-fast, parity with `shipit build` — so an inconsistent
        # artifact is refused BEFORE any (hours-long) build runs, never after
        # the healthy jobs ahead of it have already built. These are the SAME
        # pure gates `shipit build` runs: the orphan-target gate, the
        # ambiguous-producing-path gate, and each artifact's binary location
        # (the local source re-checks them as its own precondition; here they
        # run over the whole job set at once, which the per-job resolve cannot).
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
    # Accumulate into a fresh list so the verdict is this invocation's alone; a
    # caller-supplied `runs_out` is an OUTPUT sink, extended at the end (never
    # aliased, so a non-empty one it passes can never leak stale runs into the
    # verdict or the reported job count).
    runs: list[HarnessRun] = []
    for job in jobs:
        command = shlex.join(job.harness)
        try:
            binary = source.resolve(job.artifact)
        except artifact_source.ArtifactSourceError as exc:
            # The artifact could not be produced: the job hard-fails (the
            # harness never ran — rc 1, not a verdict), the rest still run.
            runs.append(HarnessRun(job, 1, str(exc)))
            print(str(exc))
            print(f"  FAIL {job.label} ({command})")
            logger.error(
                "e2e artifact could not be resolved",
                exc_info=True,
                extra={"job": job.label, "root": str(root)},
            )
            continue
        print(f"e2e: {job.label}: {command} [{job.env_var}={binary}]")
        try:
            result = run_harness(job.harness, root, {job.env_var: str(binary)})
        except execrun.ExecError as exc:
            # A harness binary missing from PATH (or any launch failure) is
            # the HARD-fail signal: 127 + a clear note, never a silent skip.
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
            # The harness report prints VERBATIM (the suite's report is the
            # point of running e2e), exactly as the build sibling prints a
            # builder's: normalize only the trailing newline — add one when it
            # is missing so the ok/FAIL line starts on its own line, keep the
            # harness's own when present so it is never doubled.
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
        # Present only when meaningful — the absent-not-null record contract.
        summary["failed_jobs"] = ", ".join(failed)
    logger.info("e2e complete", extra=summary)
    return rc
