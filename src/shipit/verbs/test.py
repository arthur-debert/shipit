"""test — the first tree-input Tool verb beyond lint (ADR-0039, TOL01-WS01).

``shipit test`` walks the repo's declared path→toolchain map (``.shipit.toml
[toolchains]``, ADR-0007) and dispatches every entry — a Leg — to its
test-producing command: the registry default per toolchain
(:mod:`shipit.tools.registry`) or the entry's per-path override. The verb is
the ONE implementation everywhere: the pixi ``test`` task, the lefthook hook,
and the CI job are all thin callers of it — the ADR-0004 lint inversion
generalized.

The pure planning — which legs run, in what order, with what argv — lives in
:func:`shipit.tools.legs.plan_legs`; this module is the effectful shell:

- argument shaping (:func:`split_args`): ``shipit test [LEG] [-- ARGS…]``.
  click consumes the first ``--`` separator, so the split is read against the
  repo's legs — a first token that names a leg is the selector; otherwise (a
  leading ``-``, or any positional token on a single-leg repo) everything is
  passthrough, forwarded VERBATIM to the selected leg's command (ADR-0039).
- execution through the one Exec runner (:mod:`shipit.execrun`, ADR-0028):
  each leg runs with cwd at its map path, ``check=False`` (a nonzero rc is
  the suite's verdict, not a transport failure) and a stated
  :data:`TEST_TIMEOUT`. A missing tool binary HARD-fails the leg (127 + a
  provision note), never a silent skip.
- the uniform exit contract (ADR-0030): 0 = every leg passed, 1 = any leg
  failed (or could not run), 2 = usage — a :class:`~shipit.tools.legs
  .LegPlanError` (unknown selector, passthrough without a single selected
  leg). A missing/malformed map raises :class:`~shipit.config.ConfigError`,
  mapped by the shared :func:`~._errors.cli_errors` shell to ``error: …`` +
  exit 1 like every config-reading verb.

Unlike lint, a leg's output prints VERBATIM even when it passes: a test run's
report is the point of running tests, so the verb replaces ``pytest`` /
``cargo nextest run`` on the console rather than swallowing a green run's
summary.
"""

from __future__ import annotations

import logging
import shlex
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import config, execrun
from ..tools import legs as legs_mod
from ._errors import cli_errors

logger = logging.getLogger("shipit.test")

#: The tool slot this verb fills — the ``[toolchains]`` override key and the
#: registry slot (:data:`shipit.tools.registry.TOOL_TEST`).
TOOL = "test"

#: Each leg Exec's stated timeout, in seconds (ADR-0028: every Exec states its
#: bound deliberately). One hour, NOT the runner's 5-minute default: a test
#: leg legitimately compiles first (cargo-nextest on a cold target dir) and
#: then runs a whole suite, so the lint checks' bound would kill healthy runs.
TEST_TIMEOUT: float = 3600.0

#: Root-level manifest basenames → the toolchain they signal, for the pointed
#: missing-map error only. This is DIAGNOSIS-side detection (what would this
#: repo probably declare?), deliberately distinct from the declared map the
#: verb dispatches on — mirrors the install catalog's provisioning-side
#: signals (:data:`shipit.install.reconcile.TOOLCHAIN_MANIFESTS`) without
#: conflating the two.
_SIGNAL_MANIFESTS: tuple[tuple[str, str], ...] = (
    ("Cargo.toml", "rust"),
    ("go.mod", "go"),
    ("pyproject.toml", "python"),
    ("package.json", "npm"),
)


def split_args(
    args: Sequence[str], entries: Sequence[config.ToolchainEntry]
) -> tuple[str | None, tuple[str, ...]]:
    """``(selector, passthrough)`` from the raw args, resolved against the repo's
    legs. Pure.

    click consumes the first ``--`` before the verb sees the args, so
    ``shipit test tests/foo.py`` and ``shipit test -- tests/foo.py`` arrive
    identically — the selector/passthrough boundary cannot be read from the
    tokens alone, so it is read from ``entries`` (the repo's legs):

    - a leading ``-`` token → no selector; everything is passthrough
      (``shipit test -- -k foo``);
    - a first token that NAMES a leg (its toolchain or map path) → the
      selector; the rest is passthrough;
    - a first token that names no leg on a SINGLE-leg repo → the no-selector
      sugar: the one leg is unambiguous, so the whole tuple is passthrough
      (``shipit test tests/foo.py`` forwards the path to pytest);
    - a first token that names no leg on a MULTI-leg repo → still taken as the
      selector, so the planner rejects it loudly naming the known legs
      (passthrough on a multi-leg repo needs an explicit selector regardless).
    """
    if not args or args[0].startswith("-"):
        return None, tuple(args)
    first = args[0]
    names = {e.toolchain for e in entries} | {e.path for e in entries}
    if first in names or len(entries) > 1:
        return first, tuple(args[1:])
    return None, tuple(args)


def missing_map_message(root: Path) -> str:
    """The pointed error for a repo with no ``[toolchains]`` map, naming the
    toolchains its root manifests signal (so the fix is a copy-paste away).
    """
    signals = [
        f'"{name}" -> {tc}' for name, tc in _SIGNAL_MANIFESTS if (root / name).is_file()
    ]
    hint = f" This repo's manifests suggest: {'; '.join(signals)}." if signals else ""
    example = next(
        (tc for name, tc in _SIGNAL_MANIFESTS if (root / name).is_file()), "rust"
    )
    return (
        f"no [toolchains] path->toolchain map in {config.CONFIG_NAME} — "
        f"`shipit {TOOL}` dispatches on that declaration (ADR-0007/0039)."
        f'{hint} Declare it under a [toolchains] table, e.g. "." = "{example}".'
    )


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
    """``0`` when every leg passed, ``1`` otherwise — the whole exit contract's
    non-usage half (ADR-0030; usage errors exit 2 before any leg runs)."""
    return 0 if all(run.ok for run in runs) else 1


def _load_entries(root: Path) -> tuple[config.ToolchainEntry, ...]:
    """The typed ``[toolchains]`` map at ``root`` — the verb's one config read.

    Raises :class:`~shipit.config.ConfigError` when ``.shipit.toml`` is
    missing/malformed OR carries no map (the pointed
    :func:`missing_map_message`) — all rendered by the shared
    :func:`~._errors.cli_errors` shell as ``error: …`` + exit 1.
    """
    cfg_path = root / config.CONFIG_NAME
    entries = (
        config.load_toolchains(config.load(cfg_path)) if cfg_path.is_file() else ()
    )
    if not entries:
        raise config.ConfigError(missing_map_message(root))
    return entries


def _run_leg(argv: Sequence[str], cwd: Path) -> execrun.ExecResult:
    """Run one leg's producing command in ``cwd`` through the one Exec runner.

    ``check=False``: a nonzero rc is the leg's *verdict*. A launch failure —
    the tool binary missing from PATH, or any OS-level error — raises
    :class:`~shipit.execrun.ExecError`, which the orchestrator renders as the
    hard-fail 127 (a missing tool NEVER skips). The Exec states
    :data:`TEST_TIMEOUT`; a wedged suite dies at that bound as the same
    hard fail.
    """
    return execrun.run(list(argv), cwd=str(cwd), check=False, timeout=TEST_TIMEOUT)


@cli_errors
def run(
    args: Sequence[str] = (),
    *,
    run_leg: Callable[[Sequence[str], Path], execrun.ExecResult] | None = None,
    runs_out: list[LegRun] | None = None,
) -> int:
    """Run the repo's test legs from the current directory. Returns 0/1/2.

    ``args`` is the raw post-``--``-stripped argument tuple (see
    :func:`split_args`). ``run_leg`` injects the Exec boundary for tests;
    ``runs_out``, when given, receives every :class:`LegRun` outcome — the
    typed per-leg verdicts behind the exit code.
    """
    started = time.monotonic()
    root = Path(".").resolve()
    entries = _load_entries(root)
    selector, passthrough = split_args(tuple(args), entries)

    try:
        planned = legs_mod.plan_legs(
            entries, tool=TOOL, selector=selector, passthrough=passthrough
        )
    except legs_mod.LegPlanError as exc:
        # The usage tier (ADR-0030, rc 2): the invocation — not the repo, not
        # a tool — is wrong, and the message is the whole diagnosis.
        print(f"error: {exc}", file=sys.stderr)
        logger.error("test invocation rejected", extra={"root": str(root)})
        return 2

    run_leg = run_leg or _run_leg
    # Accumulate into a fresh list so the verdict is this invocation's alone; a
    # caller-supplied `runs_out` is an OUTPUT sink, extended at the end (never
    # aliased, so a non-empty one it passes can never leak stale legs into the
    # verdict).
    runs: list[LegRun] = []
    for leg in planned:
        command = shlex.join(leg.argv)
        print(f"test: {leg.label}: {command}")
        try:
            result = run_leg(leg.argv, root / leg.path)
        except execrun.ExecError as exc:
            # A binary missing from PATH (or any launch failure) is the
            # HARD-fail signal: 127 + a clear note, never a silent skip.
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
            # The runner's report prints VERBATIM (unlike lint, which swallows a
            # green run): emit exactly what the leg produced, normalizing only
            # the trailing newline — add one when it's missing so the ok/FAIL
            # line starts on its own line, keep the runner's own when present so
            # it is never doubled.
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
        # Present only when meaningful — the absent-not-null record contract.
        summary["failed_legs"] = ", ".join(failed)
    logger.info("test complete", extra=summary)
    return rc
