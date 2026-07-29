"""selfcert — install's staged postconditions, asserted before any commit/PR.

Four checks over what install OWNS: the stamped manifest + lint-env solve,
a scoped lint over the delivered whole-file units, live hooks, and the
launcher resolving the stamped pin. See docs/adr/0033-repo-pins-its-shipit.md.
"""

from __future__ import annotations

import io
import logging
import os
from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path

from .. import config, execrun, git, pixienv
from .reconcile import Plan
from .units import HOOK_RECOVERY_CMD, LINT_ENV, PIXI_FILE, SHIPIT_LAUNCHER_FILE

logger = logging.getLogger("shipit.install")

PIN_CHECK_ENV = "SHIPIT_PIN_CHECK"

LAUNCHER_PROBE_TIMEOUT: float = 30.0

CHECK_MANIFEST = "manifest parses + lint env solves"
CHECK_DELIVERED_LINT = "delivered files pass delivered lint configs"
CHECK_HOOKS = "hooks live"
CHECK_LAUNCHER = "launcher resolves the stamped pin"


@dataclass(frozen=True)
class CertCheck:
    """One postcondition's outcome: its name, verdict, and failure detail."""

    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class CertReport:
    """The four postconditions' outcomes — what :func:`certify` returns."""

    checks: tuple[CertCheck, ...]

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def failures(self) -> tuple[CertCheck, ...]:
        return tuple(c for c in self.checks if not c.ok)


def format_failure(report: CertReport) -> str:
    """The loud fail-closed diagnostic — every missed postcondition, named."""
    lines = [
        "install self-certification failed (ADR-0033) — refusing to "
        "commit or open a PR:"
    ]
    for check in report.failures:
        lines.append(f"  FAIL {check.name}")
        for detail_line in check.detail.strip().splitlines():
            lines.append(f"       {detail_line}")
    lines.append(
        "the managed set must never fail its own checks; the fix belongs in "
        "shipit's managed set (never in this consumer) — fix it there and re-run."
    )
    return "\n".join(lines)


def delivered_lint_paths(plan: Plan) -> list[str]:
    """The scoped lint set: every WHOLE-FILE unit this plan writes, sorted; block units are excluded."""
    return sorted({d.unit.dest for d in plan.writes if d.unit.kind == "file"})


def _lint_env_run_tool(
    root: Path, runner: Callable[..., execrun.ExecResult]
) -> Callable[[str, list[str], Path], execrun.ExecResult]:
    """A ``shipit lint`` tool runner executing each tool through the managed lint env, under a scrubbed environment."""
    scrubbed = pixienv.scrub_env(os.environ)

    def run_tool(binary: str, args: list[str], cwd: Path) -> execrun.ExecResult:
        return runner(
            pixienv.run_argv([binary, *args], root, environment=LINT_ENV),
            cwd=str(cwd),
            env=scrubbed,
            replace_env=True,
            check=False,
            timeout=pixienv.INSTALL_TIMEOUT,
        )

    return run_tool


def _scoped_lint(root: Path, paths: list[str], runner) -> tuple[int, str]:
    """Run the lint orchestrator over exactly ``paths``, returning ``(rc, captured report)``."""
    from .. import lint

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        rc = lint.run(
            str(root),
            discover=lambda _root: list(paths),
            run_tool=_lint_env_run_tool(root, runner),
        )
    return rc, buffer.getvalue()


def _check_manifest(root: Path, runner) -> CertCheck:
    """Postcondition 1: the stamped config parses and the lint env solves — UNLOCKED, so it refreshes ``pixi.lock``."""
    try:
        config.load(root / config.CONFIG_NAME)
    except config.ConfigError as exc:
        return CertCheck(CHECK_MANIFEST, False, f"stamped {config.CONFIG_NAME}: {exc}")
    if not (root / PIXI_FILE).is_file():
        return CertCheck(CHECK_MANIFEST, False, f"no {PIXI_FILE} after the writes")
    try:
        pixienv.install(
            root,
            environment=LINT_ENV,
            env=pixienv.scrub_env(os.environ),
            runner=runner,
        )
    except execrun.ExecError as exc:
        return CertCheck(
            CHECK_MANIFEST,
            False,
            f"`pixi install --environment {LINT_ENV}` failed: {exc}",
        )
    return CertCheck(CHECK_MANIFEST, True)


def _check_delivered_lint(root: Path, plan: Plan, runner) -> CertCheck:
    """Postcondition 2: the delivered files pass the delivered lint configs."""
    paths = delivered_lint_paths(plan)
    missing = [p for p in paths if not (root / p).is_file()]
    if missing:
        return CertCheck(
            CHECK_DELIVERED_LINT,
            False,
            "install did not deliver whole-file units it planned to write:\n"
            + "\n".join(f"  {p}" for p in missing),
        )
    if not paths:
        return CertCheck(CHECK_DELIVERED_LINT, True)
    try:
        rc, report = _scoped_lint(root, paths, runner)
    except (config.ConfigError, execrun.ExecError) as exc:
        return CertCheck(
            CHECK_DELIVERED_LINT, False, f"scoped lint could not run: {exc}"
        )
    if rc != 0:
        return CertCheck(CHECK_DELIVERED_LINT, False, report)
    return CertCheck(CHECK_DELIVERED_LINT, True)


def _check_hooks(root: Path, plan: Plan, hooks_activated: bool | None) -> CertCheck:
    """Postcondition 3: the checks install configured are LIVE where it ran; no claim when it never activated."""
    if not (plan.writes and plan.activates_hooks):
        return CertCheck(CHECK_HOOKS, True)
    if hooks_activated is not True:
        return CertCheck(
            CHECK_HOOKS,
            False,
            "hook activation did not succeed — a committing install ships "
            f"its checks LIVE, never dormant; re-run `{HOOK_RECOVERY_CMD}` "
            "to activate them",
        )
    hooks_dir = git.hooks_dir(cwd=str(root))
    if hooks_dir is None:
        return CertCheck(
            CHECK_HOOKS,
            False,
            "activation reported success but the git hooks directory could not "
            "be resolved",
        )
    missing = [h for h in ("pre-commit", "pre-push") if not (hooks_dir / h).is_file()]
    if missing:
        return CertCheck(
            CHECK_HOOKS,
            False,
            f"activation reported success but .git/hooks is missing: "
            f"{', '.join(missing)}",
        )
    return CertCheck(CHECK_HOOKS, True)


def _check_launcher(root: Path, plan: Plan, stamped_pin: str, runner) -> CertCheck:
    """Postcondition 4: the delivered launcher resolves the stamped pin; skipped when the unit was declined."""
    if SHIPIT_LAUNCHER_FILE in plan.declined:
        return CertCheck(CHECK_LAUNCHER, True)
    launcher = root / SHIPIT_LAUNCHER_FILE
    if not launcher.is_file():
        return CertCheck(
            CHECK_LAUNCHER, False, f"{SHIPIT_LAUNCHER_FILE} was not delivered"
        )
    env = {k: v for k, v in os.environ.items() if k != "SHIPIT_EXEC"}
    env[PIN_CHECK_ENV] = "1"
    try:
        result = runner(
            ["bash", str(launcher)],
            cwd=str(root),
            env=env,
            replace_env=True,
            check=False,
            timeout=LAUNCHER_PROBE_TIMEOUT,
        )
    except execrun.ExecError as exc:
        return CertCheck(CHECK_LAUNCHER, False, f"launcher probe could not run: {exc}")
    if result.rc != 0:
        return CertCheck(
            CHECK_LAUNCHER,
            False,
            f"launcher refused the pin (rc {result.rc}): "
            f"{(result.stderr or result.stdout).strip()}",
        )
    resolved = result.stdout.strip()
    if resolved != stamped_pin:
        return CertCheck(
            CHECK_LAUNCHER,
            False,
            f"launcher resolved {resolved!r}, install stamped {stamped_pin!r}",
        )
    return CertCheck(CHECK_LAUNCHER, True)


def certify(
    plan: Plan,
    root: Path,
    *,
    hooks_activated: bool | None,
    stamped_pin: str,
    runner=execrun.run,
) -> CertReport:
    """Assert all four postconditions — never fail-fast, so the diagnostic names every miss; ``runner`` is the Exec boundary."""
    report = CertReport(
        checks=(
            _check_manifest(root, runner),
            _check_delivered_lint(root, plan, runner),
            _check_hooks(root, plan, hooks_activated),
            _check_launcher(root, plan, stamped_pin, runner),
        )
    )
    logger.info(
        "install self-certification %s",
        "passed" if report.ok else "FAILED",
        extra={
            "root": str(root),
            "failed_checks": ", ".join(c.name for c in report.failures) or None,
        }
        if not report.ok
        else {"root": str(root)},
    )
    return report


def consumer_debt(root: Path, *, runner=execrun.run) -> int | None:
    """Best-effort whole-tree lint failure count; ``None`` when the run could not complete at all."""
    from .. import lint

    runs: list[lint.ToolRun] = []
    try:
        with redirect_stdout(io.StringIO()):
            lint.run(
                str(root),
                run_tool=_lint_env_run_tool(root, runner),
                runs_out=runs,
            )
    except Exception:  # noqa: BLE001 — best-effort by contract, never a blocker
        logger.warning(
            "whole-tree debt lint could not run — the PR body will not "
            "carry a debt count",
            exc_info=True,
            extra={"root": str(root)},
        )
        return None
    return sum(1 for r in runs if not r.ok)
