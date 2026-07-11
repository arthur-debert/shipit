"""selfcert — install's staged postconditions, asserted before any commit/PR.

ADR-0033: **install self-certifies, scoped to what it owns.** After staging the
managed set (files written, manifest stamped, hooks activated), a committing
install asserts four postconditions and fails CLOSED — no commit, no PR, a loud
diagnostic — on any miss (:class:`~shipit.install.errors.SelfCertError` at the
apply seam):

1. **manifest** — the stamped ``.shipit.toml`` parses back, and the managed
   lint environment SOLVES (``pixi install --environment lint`` against the
   consumer's reconciled ``pixi.toml`` — which also proves that manifest
   parses, pixi refuses a manifest it cannot read).
2. **delivered lint** — the files install delivered pass the lint configs
   install delivered: a SCOPED ``shipit lint`` run over exactly the written
   WHOLE-FILE units, each tool executed through the freshly-solved lint env.
   Block units (``pixi.toml``, ``AGENTS.md``, ``settings.json``) are excluded
   deliberately: install delivered a region of those files, not the file, and
   the surrounding consumer content is DEBT to report, never a blocker. This
   is what makes "the managed set never fails its own checks" executable (the
   WS09/WS10 canary class).
3. **hooks** — the activation actually happened where install ran and left
   live hook files behind (``lefthook install`` wrote ``.git/hooks``).
4. **launcher** — the delivered ``bin/shipit`` launcher, run under its
   :data:`PIN_CHECK_ENV` probe, resolves the freshly-stamped pin to exactly
   the sha install stamped — the launcher's own parse over the real file, with
   no uv resolve (the postcondition must not need the network). Skipped when
   the consumer DECLINED the launcher unit (#600): install delivered nothing
   to probe, and the consumer's own launcher is outside what install owns.

The whole-tree check is the REPO'S bar (the ADP01 checklist's lint step), not
install's: :func:`consumer_debt` counts the whole-tree failures best-effort so
the reconcile PR body can REPORT pre-existing consumer lint debt without ever
blocking on it (the WS08 canary deadlock this scoping breaks).
"""

from __future__ import annotations

import io
import logging
import os
from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import dataclass
from pathlib import Path

from .. import config, execrun, pixienv
from .reconcile import Plan
from .units import HOOK_RECOVERY_CMD, LINT_ENV, PIXI_FILE, SHIPIT_LAUNCHER_FILE

logger = logging.getLogger("shipit.install")

#: The launcher's self-certification probe (mirrored in the managed
#: ``bin/shipit``): with this env var set, the launcher prints the pin it
#: resolved and exits 0 INSTEAD of exec'ing uv — the real script's real parse
#: over the real ``.shipit.toml``, with no network and no uv requirement.
PIN_CHECK_ENV = "SHIPIT_PIN_CHECK"

#: The launcher probe's stated timeout (ADR-0028): a bash parse of one small
#: TOML file — local-tier work; a wedged probe is itself a failed postcondition.
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
    """The scoped lint set: every WHOLE-FILE unit this plan writes, sorted.

    Block units are excluded by design (see the module docstring): the consumer
    content around a managed block is reported debt, never a blocker.
    """
    return sorted({d.unit.dest for d in plan.writes if d.unit.kind == "file"})


def _lint_env_run_tool(
    root: Path, runner: Callable[..., execrun.ExecResult]
) -> Callable[[str, list[str], Path], execrun.ExecResult]:
    """A ``shipit lint`` tool runner that executes each tool through the managed
    lint env (``pixi run --environment lint``) — the exact toolchain install
    just delivered and solved, never whatever happens to be on install's PATH.

    The child runs under a SCRUBBED environment (:func:`pixienv.scrub_env` +
    ``replace_env``): a parent dev session's leaked ``PIXI_*``/Conda activation
    pointers must not bind these tool subprocesses to a different project than
    the consumer checkout install is certifying — the same leak class every
    Tree/provisioning scrub path closes. The timeout stays pixi's long-runner
    bound (:data:`pixienv.INSTALL_TIMEOUT`): a ``pixi run``'s worst case is a
    first activation re-solving the env (provisioning-shaped work), which is
    exactly why the pixi-run seam takes that bound rather than the bare-tool
    :data:`~shipit.lint.CHECK_TIMEOUT`.
    """
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
    """Run the lint orchestrator over exactly ``paths``, capturing its report.

    Returns ``(rc, report_text)`` — the report surfaces only on failure (the
    loud diagnostic); a green scoped run stays quiet on install's terminal.
    """
    # Imported at call time so install keeps its import graph light; this is the
    # service, not the CLI error shell.
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
    """Postcondition 1: the stamped config parses; the managed lint env solves."""
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
    # Self-cert runs AFTER staging: every whole-file unit in the plan's write set
    # is a file install just delivered. One missing on disk is not "nothing to
    # lint" — it is install failing to write a file it intended to (fail CLOSED,
    # ADR-0033), so name the missing paths rather than silently skipping them.
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
    """Postcondition 3: the checks install configured are LIVE where it ran."""
    if not (plan.writes and plan.activates_hooks):
        # Mirror apply's activation predicate exactly (apply.py): it only runs
        # `lefthook install` on a WRITING install that manages the hooks. A plan
        # with no lefthook unit, or one that writes nothing (a seed-only or
        # retire-delete-only committing install with the managed set already
        # current), never attempts activation — `hooks_activated` stays None and
        # this postcondition makes no claim over hooks install did not touch.
        return CertCheck(CHECK_HOOKS, True)
    if hooks_activated is not True:
        return CertCheck(
            CHECK_HOOKS,
            False,
            "hook activation did not succeed — a committing install ships "
            f"its checks LIVE, never dormant; re-run `{HOOK_RECOVERY_CMD}` "
            "to activate them",
        )
    hooks_dir = root / ".git" / "hooks"
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
    """Postcondition 4: the delivered launcher resolves the freshly-stamped pin.

    Scoped to what install owns (the module contract): a consumer that DECLINED
    the launcher unit (``[managed.decline].keep`` carrying ``bin/shipit``, #600
    — the dogfood repo's source-deferring bootstrap is the standing case) keeps
    its OWN launcher, which install neither delivered nor may make claims over
    — so the probe is skipped, a no-claim pass like :func:`_check_hooks` on an
    activation install never attempted.
    """
    if SHIPIT_LAUNCHER_FILE in plan.declined:
        return CertCheck(CHECK_LAUNCHER, True)
    launcher = root / SHIPIT_LAUNCHER_FILE
    if not launcher.is_file():
        return CertCheck(
            CHECK_LAUNCHER, False, f"{SHIPIT_LAUNCHER_FILE} was not delivered"
        )
    # The probe env: the launcher honors SHIPIT_EXEC BEFORE the pin parse, so a
    # dev session's override must be stripped or the probe would exec a build
    # instead of answering; the probe var itself turns the run into a pin print.
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
    """Assert the four staged postconditions; run ALL of them (never fail-fast),
    so the fail-closed diagnostic names every miss at once.

    ``runner`` is the injectable Exec boundary (ADR-0028) — tests assert each
    check's verdict logic without a live pixi/bash.
    """
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
    """Best-effort whole-tree lint failure count — the DEBT the reconcile PR
    body reports (never a blocker; the whole-tree gate is the repo's bar).

    ``None`` when the whole-tree run could not complete at all (no verdict is
    not zero debt); an int is the number of failing checks.
    """
    from .. import lint  # lazy — see `_scoped_lint`

    runs: list[lint.ToolRun] = []
    try:
        with redirect_stdout(io.StringIO()):
            lint.run(
                str(root),
                run_tool=_lint_env_run_tool(root, runner),
                runs_out=runs,
            )
    except Exception:  # noqa: BLE001 — best-effort by contract: debt is
        # reported when readable, never a blocker and never a crash.
        logger.warning(
            "whole-tree debt lint could not run — the PR body will not "
            "carry a debt count",
            exc_info=True,
            extra={"root": str(root)},
        )
        return None
    return sum(1 for r in runs if not r.ok)
