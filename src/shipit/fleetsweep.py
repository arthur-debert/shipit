"""The fleet verification sweep — every shipped tool verb, run locally against every portfolio repo, as one matrix report."""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import config, events, execrun, identity, pixienv, workenv
from .changelog import CHANGELOG_DIR
from .release import posture
from .tree.create import Tree, create_from_source, new_tree_id, new_tree_naming
from .tree.layout import TreeSpec

logger = logging.getLogger("shipit.fleet")


class SweepError(RuntimeError):
    """The sweep's domain refusal: a missing source checkout, an unresolvable candidate executable, an unknown selector."""


SWEEP_TOOLS: tuple[str, ...] = ("lint", "test", "build", "e2e", "changelog")

TOOL_ARGS: Mapping[str, tuple[str, ...]] = {
    "lint": ("lint",),
    "test": ("test",),
    "build": ("build",),
    "e2e": ("e2e",),
    "changelog": ("changelog", "check"),
}

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_NOT_APPLICABLE = "not-applicable"
STATUS_EXPECTED_FAIL = "expected-fail"

SWEEP_TIMEOUT: float = 3600.0

REPORT_PATH = Path("docs/reports/fleet-sweep.json")

DEFAULT_SOURCE_ROOT = Path("~/h")


@dataclass(frozen=True)
class PortfolioEntry:
    """One ``[project.portfolio]`` repo: stack, ``owner/name`` slug, source-checkout path, signing posture, and any declared ``expect_verify_fail`` reason."""

    stack: str
    repo: str
    path: str
    signing: str
    signing_reason: str | None = None
    expect_verify_fail: str | None = None


def _parse_entry(where: str, stack: str, spec: object) -> PortfolioEntry:
    """One portfolio list entry into a typed value; unknown keys pass through untouched."""
    if not isinstance(spec, dict):
        raise config.ConfigError(
            f"{where} must be an inline table, e.g. "
            f'{{ repo = "owner/name", path = "owner/name" }}; got {spec!r}'
        )
    repo = spec.get("repo")
    if not isinstance(repo, str) or not repo:
        raise config.ConfigError(f"{where} must declare `repo` (an owner/name slug)")
    try:
        identity.repo_from_slug(repo)
    except ValueError as exc:
        raise config.ConfigError(f"{where}: {exc}") from exc
    path = spec.get("path")
    if not isinstance(path, str) or not path:
        raise config.ConfigError(
            f"{where} must declare `path` (the source-checkout layout path)"
        )
    if Path(path).is_absolute() or ".." in Path(path).parts:
        raise config.ConfigError(
            f"{where}: path must be a repo-relative layout path under "
            f"--source-root — no absolute path, no `..` escape; got {path!r}"
        )
    expect = spec.get("expect_verify_fail")
    if expect is not None and (not isinstance(expect, str) or not expect):
        raise config.ConfigError(
            f"{where}: expect_verify_fail must be a non-empty reason string"
        )
    signing, reason = _parse_signing(where, spec)
    return PortfolioEntry(
        stack=stack,
        repo=repo,
        path=path,
        signing=signing,
        signing_reason=reason,
        expect_verify_fail=expect,
    )


def _parse_signing(where: str, spec: dict) -> tuple[str, str | None]:
    """The entry's posture and its reason; every repo declares one so none is accidentally divergent, and ``unsigned`` records the decision that chose it."""
    signing = spec.get("signing")
    if not isinstance(signing, str) or not signing:
        raise config.ConfigError(
            f"{where} must declare `signing` (one of: {', '.join(posture.POSTURES)}) — "
            f"the fleet's signing posture is declared per repo, never inferred"
        )
    try:
        posture.validate_posture(signing)
    except ValueError as exc:
        raise config.ConfigError(f"{where}: {exc}") from exc
    reason = spec.get("signing_reason")
    if reason is not None and (not isinstance(reason, str) or not reason):
        raise config.ConfigError(
            f"{where}: signing_reason must be a non-empty reason string"
        )
    if signing == posture.POSTURE_UNSIGNED and reason is None:
        raise config.ConfigError(
            f'{where}: signing = "{posture.POSTURE_UNSIGNED}" must declare '
            f"`signing_reason` — a repo that could sign and does not records why"
        )
    return signing, reason


def load_portfolio(cfg: dict) -> tuple[PortfolioEntry, ...]:
    """The ``[project.portfolio]`` fleet manifest as typed entries in declaration order; duplicate repos (by canonical slug) are refused."""
    section: object = None
    where = "[project.portfolio]"
    for table in ("project", "custom"):
        sub = cfg.get(table)
        if isinstance(sub, dict) and "portfolio" in sub:
            section = sub["portfolio"]
            where = f"[{table}.portfolio]"
            break
    if section is None:
        raise config.ConfigError(
            "no [project.portfolio] table — the fleet sweep iterates exactly the "
            "declared portfolio (ADR-0033), never a reconstructed repo list"
        )
    if not isinstance(section, dict):
        raise config.ConfigError(f"{where} must be a table of stack -> entry list")
    entries: list[PortfolioEntry] = []
    seen: dict[str, str] = {}
    for stack, specs in section.items():
        if not isinstance(specs, list):
            raise config.ConfigError(f"{where}.{stack} must be a list of repo entries")
        for i, spec in enumerate(specs):
            site = f"{where}.{stack}[{i}]"
            entry = _parse_entry(site, str(stack), spec)
            slug = identity.repo_from_slug(entry.repo).slug
            if slug in seen:
                raise config.ConfigError(
                    f"{site}: duplicate portfolio repo {slug!r} — also declared at "
                    f"{seen[slug]}. The sweep keys repos by canonical (lowercased) "
                    f"slug, so a duplicate (including a case-only difference) would "
                    f"silently collapse under `--repo` filtering while the full "
                    f"sweep runs both; declare each repo once."
                )
            seen[slug] = site
            entries.append(entry)
    return tuple(entries)


@dataclass(frozen=True)
class ToolPlan:
    """One tool's applicability verdict for one repo; ``reason`` is set exactly when ``applicable`` is False."""

    tool: str
    applicable: bool
    reason: str | None = None


def derive_plans(
    *, legs_declared: bool, e2e_declared: bool, changelog_dir: bool
) -> tuple[ToolPlan, ...]:
    """The pure applicability rules over a repo's parsed declarations."""

    def plan(tool: str, declared: bool, reason: str) -> ToolPlan:
        return ToolPlan(tool, True) if declared else ToolPlan(tool, False, reason)

    return (
        ToolPlan("lint", True),
        plan("test", legs_declared, "no testable leg declared (no [toolchains] map)"),
        plan("build", legs_declared, "no buildable leg declared (no [toolchains] map)"),
        plan("e2e", e2e_declared, "no e2e harness declared (no [artifacts] e2e table)"),
        plan("changelog", changelog_dir, f"no {CHANGELOG_DIR}/ fragment convention"),
    )


def plan_tools(repo_root: Path) -> tuple[ToolPlan, ...]:
    """The per-tool plans for ``repo_root``; an unreadable config proves no absence, so test/build/e2e default to applicable."""
    changelog_dir = (repo_root / CHANGELOG_DIR).is_dir()
    try:
        cfg = config.load(repo_root / config.CONFIG_NAME)
        legs = config.load_toolchains(cfg)
        artifacts = config.load_artifacts(cfg)
    except (config.ConfigError, OSError):
        return derive_plans(
            legs_declared=True, e2e_declared=True, changelog_dir=changelog_dir
        )
    return derive_plans(
        legs_declared=bool(legs),
        e2e_declared=any(artifact.e2e is not None for artifact in artifacts),
        changelog_dir=changelog_dir,
    )


def cell_status(rc: int, expect_verify_fail: str | None) -> tuple[str, str | None]:
    """A run's (status, reason): rc 0 is green, nonzero is expected-fail with the declared reason or red."""
    if rc == 0:
        return STATUS_PASS, None
    if expect_verify_fail:
        return STATUS_EXPECTED_FAIL, expect_verify_fail
    return STATUS_FAIL, None


@dataclass(frozen=True)
class Cell:
    """One matrix cell: a tool's verdict on one repo, with the exact argv and — when not green — the raw output."""

    tool: str
    status: str
    reason: str | None = None
    argv: tuple[str, ...] | None = None
    cwd: str | None = None
    rc: int | None = None
    duration_ms: int | None = None
    output: str | None = None
    work_env: Mapping[str, object] | None = None

    def to_dict(self) -> dict:
        """The cell's JSON shape, absent-not-null: a field appears exactly when it is meaningful for this status."""
        data: dict = {"status": self.status}
        if self.reason is not None:
            data["reason"] = self.reason
        if self.argv is not None:
            data["command"] = shlex.join(self.argv)
            data["argv"] = list(self.argv)
        if self.cwd is not None:
            data["cwd"] = self.cwd
        if self.rc is not None:
            data["rc"] = self.rc
        if self.duration_ms is not None:
            data["duration_ms"] = self.duration_ms
        if self.output is not None:
            data["output"] = self.output
        if self.work_env is not None:
            data.update(self.work_env)
        return data


@dataclass(frozen=True)
class RepoResult:
    """One matrix row: a portfolio repo and its per-tool cells, in :data:`SWEEP_TOOLS` order."""

    entry: PortfolioEntry
    cells: tuple[Cell, ...]

    @property
    def red(self) -> tuple[Cell, ...]:
        return tuple(cell for cell in self.cells if cell.status == STATUS_FAIL)

    @property
    def expected(self) -> tuple[Cell, ...]:
        return tuple(cell for cell in self.cells if cell.status == STATUS_EXPECTED_FAIL)

    @property
    def adoption_ready(self) -> bool:
        """Whether every applicable cell is green; an expected-fail row is not ready."""
        return not self.red and not self.expected

    def summary(self) -> str:
        """The per-repo adoption-ready line: ready, or what stands in the way."""
        if self.adoption_ready:
            return f"{self.entry.repo}: adoption-ready — every applicable cell green"
        parts = []
        if self.red:
            tools = ", ".join(cell.tool for cell in self.red)
            parts.append(f"{len(self.red)} red cell(s): {tools}")
        for cell in self.expected:
            parts.append(f"expected-fail ({cell.tool}): {cell.reason}")
        return f"{self.entry.repo}: NOT adoption-ready — {'; '.join(parts)}"

    def to_dict(self) -> dict:
        data: dict = {
            "stack": self.entry.stack,
            "repo": self.entry.repo,
            "path": self.entry.path,
            "adoption_ready": self.adoption_ready,
            "summary": self.summary(),
            "cells": {cell.tool: cell.to_dict() for cell in self.cells},
        }
        if self.entry.expect_verify_fail is not None:
            data["expect_verify_fail"] = self.entry.expect_verify_fail
        return data


ADOPTION_SEED_NOTE = (
    "ADP02 adoption-readiness seed: a repo whose row is all green "
    "(every applicable cell pass) is adoption-ready; each repo's `summary` "
    "line is its checklist entry."
)


@dataclass(frozen=True)
class SweepReport:
    """The per-tool × per-repo matrix, stamped with the candidate build it was verified against."""

    candidate_build: str | None
    generated_at: str
    tools: tuple[str, ...]
    repos: tuple[RepoResult, ...]

    @property
    def red_cells(self) -> int:
        return sum(len(row.red) for row in self.repos)

    @property
    def all_green(self) -> bool:
        return self.red_cells == 0

    def verdict(self) -> int:
        """The exit code: 0 when the matrix holds no red cell, 1 otherwise."""
        return 0 if self.all_green else 1

    def to_dict(self) -> dict:
        return {
            "kind": "fleet-sweep-report",
            "consumer": ADOPTION_SEED_NOTE,
            "candidate_build": self.candidate_build,
            "generated_at": self.generated_at,
            "tools": list(self.tools),
            "red_cells": self.red_cells,
            "adoption_ready": [
                row.entry.repo for row in self.repos if row.adoption_ready
            ],
            "repos": [row.to_dict() for row in self.repos],
        }


CreateTree = Callable[[PortfolioEntry], Tree]
RunTool = Callable[[Sequence[str], Path, Mapping[str, str]], execrun.ExecResult]
RemoveTree = Callable[[Path], None]


def resolve_candidate(explicit: str | Path | None = None) -> Path:
    """The candidate shipit executable: ``explicit`` taken literally as a path, else the running build; refuses when neither is an executable file."""
    if explicit is not None:
        resolved = Path(explicit).expanduser()
    else:
        argv0 = sys.argv[0]
        resolved = Path(shutil.which(argv0) or argv0).expanduser()
    if resolved.is_file() and os.access(resolved, os.X_OK):
        return resolved.resolve()
    hint = (
        "pass --shipit-exec /path/to/candidate"
        if explicit is None
        else "not an executable file"
    )
    raise SweepError(
        f"cannot resolve the candidate shipit executable at {resolved} — {hint}"
    )


def _create_tree(entry: PortfolioEntry, *, source_root: Path) -> Tree:
    """Cut one freeform sweep Tree for ``entry`` off its local source checkout under ``source_root``."""
    source = (source_root / entry.path).expanduser()
    if not source.is_dir():
        raise SweepError(
            f"source checkout for {entry.repo} missing at {source} — the sweep "
            "cuts each Tree off the portfolio's local checkout layout "
            "(--source-root)"
        )
    sweep_token = new_tree_id()
    spec = TreeSpec(
        repo=identity.repo_from_slug(entry.repo),
        **new_tree_naming("claude"),
        branch=f"fleet-sweep-{sweep_token}",
    )
    return create_from_source(spec, source_repo=source)


def _run_tool(
    argv: Sequence[str], cwd: Path, env: Mapping[str, str]
) -> execrun.ExecResult:
    """One cell's Exec: ``check=False`` at :data:`SWEEP_TIMEOUT`, in a scrubbed replacement environment carrying only ``env`` on top."""
    child_env = pixienv.scrub_env(os.environ)
    child_env.update(env)
    return execrun.run(
        list(argv),
        cwd=str(cwd),
        env=child_env,
        replace_env=True,
        check=False,
        timeout=SWEEP_TIMEOUT,
    )


def _remove_tree(path: Path) -> None:
    """Tear one sweep Tree down; a failed removal warns rather than failing the sweep."""
    try:
        shutil.rmtree(path)
    except OSError:
        logger.warning("fleet sweep: could not remove Tree %s", path, exc_info=True)


def _failed_row(
    entry: PortfolioEntry, tools: Sequence[str], plans: Sequence[ToolPlan], error: str
) -> RepoResult:
    """The row for a repo whose Tree could not be cut: every applicable cell red (or expected-fail) with the create error as output."""
    status, reason = cell_status(1, entry.expect_verify_fail)
    cells = []
    for plan in plans:
        if plan.tool not in tools:
            continue
        if not plan.applicable:
            cells.append(Cell(plan.tool, STATUS_NOT_APPLICABLE, reason=plan.reason))
            continue
        cells.append(Cell(plan.tool, status, reason=reason, output=error))
    return RepoResult(entry, tuple(cells))


def _run_cell(
    entry: PortfolioEntry,
    tool: str,
    tree: Tree,
    *,
    candidate: Path,
    run_tool: RunTool,
) -> Cell:
    """Execute one applicable cell under the ``SHIPIT_EXEC`` override, routed through the Tree's own pixi env when it has one."""
    tree_root = Path(tree.path)
    launcher = tree_root / "bin" / "shipit"
    argv: tuple[str, ...] = (str(launcher), *TOOL_ARGS[tool])
    pixi_provisioned = pixienv.has_default_env(tree_root)
    cell_work_env = workenv.resolve_write_run_env(
        repo=identity.repo_from_slug(entry.repo),
        tree_path=str(tree_root),
        branch=tree.branch,
        base=tree.base,
        pixi_provisioned=pixi_provisioned,
    )
    work_env_record = workenv.resolution_record(
        cell_work_env,
        boundary="fleet-sweep.cell",
        extra={"fleet_repo": entry.repo, "tool": tool},
    )
    if pixi_provisioned:
        argv = tuple(pixienv.run_argv(list(argv), tree_root))
    if not launcher.is_file():
        status, reason = cell_status(1, entry.expect_verify_fail)
        return Cell(
            tool,
            status,
            reason=reason,
            argv=argv,
            cwd=str(tree_root),
            work_env=work_env_record,
            output=(
                f"{launcher}: managed launcher missing — the repo is not "
                "bootstrapped (run shipit install there; a shipit-side gap, "
                "never a consumer patch)"
            ),
        )
    env = {"SHIPIT_EXEC": str(candidate)}
    try:
        result = run_tool(argv, tree_root, env)
    except execrun.ExecError as exc:
        status, reason = cell_status(1, entry.expect_verify_fail)
        return Cell(
            tool,
            status,
            reason=reason,
            argv=argv,
            cwd=str(tree_root),
            rc=exc.rc,
            work_env=work_env_record,
            output=str(exc),
        )
    status, reason = cell_status(result.rc, entry.expect_verify_fail)
    output = result.stdout + result.stderr if status != STATUS_PASS else None
    return Cell(
        tool,
        status,
        reason=reason,
        argv=argv,
        cwd=str(tree_root),
        rc=result.rc,
        duration_ms=result.duration_ms,
        work_env=work_env_record,
        output=output,
    )


def _sweep_repo(
    entry: PortfolioEntry,
    *,
    tools: Sequence[str],
    candidate: Path,
    source_root: Path,
    keep_trees: bool,
    create_tree: CreateTree,
    run_tool: RunTool,
    remove_tree: RemoveTree,
) -> RepoResult:
    """One matrix row: cut the Tree, derive applicability from ITS declarations, run each applicable cell, tear the Tree down."""
    try:
        tree = create_tree(entry)
    except (SweepError, ValueError, OSError, execrun.ExecError) as exc:
        logger.error(
            "fleet sweep: tree create failed",
            exc_info=True,
            extra={"sweep_repo": entry.repo},
        )
        plans = plan_tools((source_root / entry.path).expanduser())
        return _failed_row(entry, tools, plans, f"tree create failed: {exc}")
    tree_root = Path(tree.path)
    try:
        cells = []
        for plan in plan_tools(tree_root):
            if plan.tool not in tools:
                continue
            if not plan.applicable:
                cells.append(Cell(plan.tool, STATUS_NOT_APPLICABLE, reason=plan.reason))
                continue
            cells.append(
                _run_cell(
                    entry, plan.tool, tree, candidate=candidate, run_tool=run_tool
                )
            )
        return RepoResult(entry, tuple(cells))
    finally:
        if not keep_trees:
            remove_tree(tree_root)


def sweep(
    entries: Sequence[PortfolioEntry],
    *,
    candidate: Path,
    candidate_build: str | None,
    generated_at: str,
    source_root: Path,
    tools: Sequence[str] = SWEEP_TOOLS,
    keep_trees: bool = False,
    create_tree: CreateTree | None = None,
    run_tool: RunTool | None = None,
    remove_tree: RemoveTree | None = None,
) -> SweepReport:
    """Run the sweep over ``entries`` — sequentially, one Tree at a time — and assemble the report; refuses when ``tools`` selects nothing."""
    if create_tree is None:

        def create_tree(entry: PortfolioEntry) -> Tree:
            return _create_tree(entry, source_root=source_root)

    run_tool = run_tool or _run_tool
    remove_tree = remove_tree or _remove_tree
    selected = tuple(tool for tool in SWEEP_TOOLS if tool in tools)
    if not selected:
        raise SweepError(
            f"no swept tools selected from {tuple(tools)!r} — the sweep runs a "
            f"nonempty subset of {SWEEP_TOOLS}; an empty selection would emit a "
            "trivially green report (0 red cells) without running anything"
        )
    events.emit(
        logger,
        "sweep.started",
        "fleet sweep: %d repo(s) x %s under candidate %s",
        len(entries),
        "/".join(selected),
        candidate,
        extra={
            "repos": len(entries),
            "tools": "/".join(selected),
            "candidate": str(candidate),
            "candidate_build": candidate_build or "unknown",
        },
    )
    rows = []
    for entry in entries:
        row = _sweep_repo(
            entry,
            tools=selected,
            candidate=candidate,
            source_root=source_root,
            keep_trees=keep_trees,
            create_tree=create_tree,
            run_tool=run_tool,
            remove_tree=remove_tree,
        )
        rows.append(row)
        events.emit(
            logger,
            "sweep.repo.done",
            "fleet sweep: %s — %d red, adoption-ready=%s",
            entry.repo,
            len(row.red),
            row.adoption_ready,
            extra={
                "sweep_repo": entry.repo,
                "red": len(row.red),
                "adoption_ready": row.adoption_ready,
            },
        )
    report = SweepReport(
        candidate_build=candidate_build,
        generated_at=generated_at,
        tools=selected,
        repos=tuple(rows),
    )
    events.emit(
        logger,
        "sweep.completed",
        "fleet sweep: %d repo(s), %d red cell(s), %d adoption-ready",
        len(report.repos),
        report.red_cells,
        len([row for row in report.repos if row.adoption_ready]),
        extra={
            "repos": len(report.repos),
            "red_cells": report.red_cells,
            "adoption_ready": len([r for r in report.repos if r.adoption_ready]),
        },
    )
    return report
