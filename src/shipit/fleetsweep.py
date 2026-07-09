"""The fleet verification sweep (TOL01-WS07) — TOL01's exit gate.

``shipit fleet sweep`` runs every shipped tool verb locally against every
``[project.portfolio]`` repo it applies to and assembles ONE per-tool ×
per-repo matrix report — the evidence artifact the workstream is verified by
(PRD docs/prd/tol01-ci-tools.md, stories 47/49; evidence over unit tests) and
ADP02's adoption-readiness seed (a repo whose row is all green is
adoption-ready).

The moving parts, in the house shape (ADR-0030 — typed frozen values; the
effectful orchestrator holds injectable boundaries so every decision is
fixture-testable):

- **The portfolio is the iteration source** (:func:`load_portfolio`,
  ADR-0033): the sweep iterates exactly the ``[project.portfolio]`` table of
  the checkout it runs from — never a reconstructed repo list. ``[project]``
  is the consumer-owned escape hatch :mod:`shipit.config` deliberately never
  polices, so the typed read lives HERE, in the one consumer of the
  ``portfolio`` subtree, with loud errors naming the offending entry.
- **A Tree per repo** (the existing dissociated-clone machinery,
  :mod:`shipit.tree.create`): each portfolio repo gets a hermetic freeform
  Tree — checkout, pixi provisioning, hook activation — cut from the repo's
  local source checkout under ``--source-root`` (the portfolio ``path``
  layout), removed after its row unless ``--keep-trees``.
- **The candidate build, never the pin** (ADR-0033): inside a Tree
  ``bin/shipit`` resolves the repo's pin, so every tool invocation runs
  THROUGH the Tree's managed launcher with the sanctioned ``SHIPIT_EXEC``
  override pointing at the candidate build under test — announced on stderr
  by the launcher and durably by the exec'd build's ``launcher.overridden``
  event. The candidate build, never the candidate ENVIRONMENT: a provisioned
  Tree's invocation is routed through the Tree's own pixi env (see
  :func:`_run_cell`) so the tools the candidate dispatches (``pytest``,
  ``cargo nextest``, …) resolve the Tree's provisioning, hermetic to the
  Tree — not the coordinator env the sweep was launched from.
- **Applicability derives from the repo's OWN declarations**
  (:func:`derive_plans`): lint everywhere; test + build where the
  path→toolchain map declares a leg (ADR-0007 — every map entry is both;
  a repo with no ``[toolchains]`` map, a no-code repo, gets n/a cells rather
  than the verbs' missing-map error); e2e where an ``[artifacts]`` entry declares an ``e2e`` table (no
  declaration = no e2e lane, by design); the changelog check where the
  ``CHANGELOG/`` fragment convention exists. A non-applicable cell is
  RECORDED as such, never silently skipped; an unreadable Tree config proves
  nothing absent, so every tool runs and fails with its own diagnosis.
- **The report is the deliverable** (:class:`SweepReport`): machine-readable,
  every red cell carrying the exact command line and raw output — sufficient
  for a fix agent to reproduce without re-running the sweep. A declared
  ``expect_verify_fail`` renders as *expected-fail* with its reason, distinct
  from both green and red. Every red-cell fix lands in shipit — a registry
  default, a toolchain dispatch entry, an adapter — never a consumer-repo
  patch (story 49); the sweep re-runs (``--repo``/``--tool`` narrow a re-run)
  until the matrix is green.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import config, events, execrun, identity, pixienv
from .changelog import CHANGELOG_DIR
from .tree.create import Tree, create_from_source, new_agent_hash
from .tree.layout import TreeSpec

logger = logging.getLogger("shipit.fleet")


class SweepError(RuntimeError):
    """The sweep's domain refusal — a missing source checkout, an unresolvable
    candidate executable, an unknown ``--repo`` selector. Mapped to
    ``error: …`` + exit 1 by the shared :func:`~shipit.verbs._errors.cli_errors`
    shell."""


#: The swept tool verbs, in matrix column order — the closed set this WS
#: orchestrates (the tools themselves landed in earlier TOL01 WSes).
SWEEP_TOOLS: tuple[str, ...] = ("lint", "test", "build", "e2e", "changelog")

#: Per-tool subcommand argv appended to the Tree's launcher — the EXACT
#: invocation a laptop or CI runs (ADR-0039: the verb is the one
#: implementation), so a red cell's recorded command reproduces verbatim.
TOOL_ARGS: Mapping[str, tuple[str, ...]] = {
    "lint": ("lint",),
    "test": ("test",),
    "build": ("build",),
    "e2e": ("e2e",),
    "changelog": ("changelog", "check"),
}

#: Cell statuses — the matrix's closed vocabulary. ``expected-fail`` is a
#: DECLARED expectation (``expect_verify_fail`` on the portfolio entry),
#: distinct from both green and red.
STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_NOT_APPLICABLE = "not-applicable"
STATUS_EXPECTED_FAIL = "expected-fail"

#: Each tool Exec's stated timeout (ADR-0028): the test-verb bound — a swept
#: test/build leg legitimately compiles cold — reused for every cell.
SWEEP_TIMEOUT: float = 3600.0

#: Where the full sweep writes its JSON report artifact (repo-relative, in the
#: shipit checkout the sweep runs from) — committed there as the TOL01 exit
#: evidence and consumed by ADP02 as the adoption-readiness checklist seed.
REPORT_PATH = Path("docs/reports/fleet-sweep.json")

#: The default ``--source-root``: the hermetic-clone checkout layout the
#: portfolio ``path`` entries index into (ADR-0033's fleet manifest).
DEFAULT_SOURCE_ROOT = Path("~/h")


# --------------------------------------------------------------------------
# The portfolio read — [project.portfolio] as typed entries
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PortfolioEntry:
    """One ``[project.portfolio]`` repo: its stack (the table key), the
    ``owner/name`` slug, the source-checkout ``path`` under the layout root,
    and the optional declared ``expect_verify_fail`` reason (a failure the
    fleet KNOWS about — rendered as expected-fail, never as green)."""

    stack: str
    repo: str
    path: str
    expect_verify_fail: str | None = None


def _parse_entry(where: str, stack: str, spec: object) -> PortfolioEntry:
    """One portfolio list entry into a typed value, loudly (ADR-0030).

    Only the fields the sweep consumes are validated — ``[project]`` stays the
    un-policed consumer namespace, so UNKNOWN keys pass through untouched.
    """
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
        # The path indexes INTO --source-root (``_create_tree`` builds
        # ``source_root / path``); an absolute path silently wins over the join
        # and a ``..`` component escapes the layout, either of which would sweep
        # the wrong checkout. Repo-relative only, same doctrine as [toolchains].
        raise config.ConfigError(
            f"{where}: path must be a repo-relative layout path under "
            f"--source-root — no absolute path, no `..` escape; got {path!r}"
        )
    expect = spec.get("expect_verify_fail")
    if expect is not None and (not isinstance(expect, str) or not expect):
        raise config.ConfigError(
            f"{where}: expect_verify_fail must be a non-empty reason string"
        )
    return PortfolioEntry(stack=stack, repo=repo, path=path, expect_verify_fail=expect)


def load_portfolio(cfg: dict) -> tuple[PortfolioEntry, ...]:
    """The ``[project.portfolio]`` fleet manifest as typed entries, in
    declaration order (stack order, then list order).

    The AUTHORITATIVE iteration source (ADR-0033): the sweep walks exactly
    this table, never a reconstructed repo list. Lives here rather than in
    :mod:`shipit.config` because ``[project]`` (alias ``[custom]``) is the
    consumer-owned escape hatch whose subtree config validation deliberately
    never descends — the sweep is the consumer, so the shape it needs is
    validated at ITS boundary. A missing or malformed table raises
    :class:`~shipit.config.ConfigError` naming the offending entry.

    Duplicate repos are REJECTED here, naming both declaration sites: the
    ``--repo`` filter (:mod:`shipit.verbs.fleet`) keys entries by canonical
    (lowercased) slug, so two entries for one repo — including case-only
    differences — would silently collapse under filtering while the full sweep
    runs both. Failing loud at load keeps the filtered run consistent with the
    full sweep and surfaces the misconfigured manifest.
    """
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
            # Key by the canonical (lowercased) slug — the SAME normalization the
            # --repo filter uses (identity.repo_from_slug) — so a case-only dup is
            # caught, not just a byte-identical one. _parse_entry already validated
            # the slug parses, so this never raises ValueError.
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


# --------------------------------------------------------------------------
# Applicability — derived per repo from its OWN declarations
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolPlan:
    """One tool's applicability verdict for one repo: run it, or record WHY
    not (``reason`` is set exactly when ``applicable`` is False)."""

    tool: str
    applicable: bool
    reason: str | None = None


def derive_plans(
    *, legs_declared: bool, e2e_declared: bool, changelog_dir: bool
) -> tuple[ToolPlan, ...]:
    """The pure applicability rules over a repo's parsed declarations.

    lint applies EVERYWHERE (every repo has lintable files). test + build
    BOTH derive from the path→toolchain map: every ``[toolchains]`` entry is
    a testable and a buildable leg (ADR-0007), and a repo declaring NO map —
    a no-code repo, nothing for install to seed — has no test or build lane
    BY DESIGN: a not-applicable cell, never the verbs' missing-map error (that
    refusal is right on a code repo, noise on a repo with nothing to declare).
    e2e applies where an ``[artifacts]`` entry declares an ``e2e`` table —
    no declaration means no e2e lane BY DESIGN (PRD story 11), a
    not-applicable cell rather than a failure. The changelog check applies
    where the ``CHANGELOG/`` fragment convention exists.
    """

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
    """Derive the per-tool plans from the declarations at ``repo_root``.

    Applicability may only prove ABSENCE from a readable declaration: when
    the repo's ``.shipit.toml`` is missing or malformed, the CONFIG-borne
    facts are unprovable, so test + build + e2e default to applicable — each
    runs and fails with its own diagnosis, an honest red cell rather than a silent skip
    (the fix-discipline surface, story 49). "Malformed" spans every way the
    config resists parsing: a :class:`~shipit.config.ConfigError` (bad TOML,
    non-UTF-8 bytes, or a schema violation — ``config.load`` wraps them all,
    #585) and an ``OSError`` (a permission denial, a mid-read unlink); either
    is unprovable-config, never an uncaught crash of the whole sweep. The
    ``CHANGELOG/`` convention is a FILESYSTEM fact, provable regardless of the
    config, so changelog stays tied to the directory check on BOTH paths — an
    unreadable config never conjures changelog failure noise on a repo that has
    no fragment convention.
    """
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


# --------------------------------------------------------------------------
# The matrix — typed cells, rows, report
# --------------------------------------------------------------------------


def cell_status(rc: int, expect_verify_fail: str | None) -> tuple[str, str | None]:
    """A run's (status, reason): rc 0 is green; a nonzero rc is expected-fail
    with the DECLARED reason when the portfolio entry carries one, red
    otherwise. Pure — the one place the expected-fail carve-out is decided."""
    if rc == 0:
        return STATUS_PASS, None
    if expect_verify_fail:
        return STATUS_EXPECTED_FAIL, expect_verify_fail
    return STATUS_FAIL, None


@dataclass(frozen=True)
class Cell:
    """One matrix cell: a tool's verdict on one repo.

    An EXECUTED cell carries the exact ``argv`` (and its ``cwd``, ``rc``,
    ``duration_ms``); a non-green executed cell also carries the raw
    ``output`` — enough for a fix agent to reproduce without re-running the
    sweep. A not-applicable cell carries only its ``reason``; an
    expected-fail cell's ``reason`` is the declared expectation.
    """

    tool: str
    status: str
    reason: str | None = None
    argv: tuple[str, ...] | None = None
    cwd: str | None = None
    rc: int | None = None
    duration_ms: int | None = None
    output: str | None = None

    def to_dict(self) -> dict:
        """The cell's JSON shape — absent-not-null: a field appears exactly
        when it is meaningful for this cell's status."""
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
        return data


@dataclass(frozen=True)
class RepoResult:
    """One matrix row: a portfolio repo and its per-tool cells (in
    :data:`SWEEP_TOOLS` order, filtered to the swept tools)."""

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
        """The ADP02 seed verdict: every applicable cell green. An
        expected-fail row is NOT adoption-ready — the declared reason still
        stands between the repo and adoption — but it does not hold the exit
        gate red."""
        return not self.red and not self.expected

    def summary(self) -> str:
        """The per-repo adoption-ready line ADP02 consumes as its checklist
        seed — one sentence saying ready or what stands in the way."""
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


#: The report's self-description of who consumes it and how — committed into
#: every artifact so the format SAYS it seeds ADP02 (PRD Further Notes).
ADOPTION_SEED_NOTE = (
    "ADP02 adoption-readiness seed: a repo whose row is all green "
    "(every applicable cell pass) is adoption-ready; each repo's `summary` "
    "line is its checklist entry."
)


@dataclass(frozen=True)
class SweepReport:
    """The per-tool × per-repo matrix — the TOL01 exit-gate evidence artifact.

    ``candidate_build`` stamps WHICH shipit build the fleet was verified
    against (:func:`shipit.buildid.build_sha`; ``None`` when the running
    build is untracked). The exit gate is green when no cell is red — every
    applicable cell pass or declared expected-fail.
    """

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
        """The exit code: 0 when the matrix holds no red cell, 1 otherwise —
        a re-run branches on it mechanically (the fix loop's gate)."""
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


# --------------------------------------------------------------------------
# The orchestrator — a Tree per repo, the candidate build per cell
# --------------------------------------------------------------------------

#: The injectable boundaries (fixture seams, the house pattern of the tool
#: verbs): cut a Tree for one entry, run one tool argv, remove a Tree dir.
CreateTree = Callable[[PortfolioEntry], Tree]
RunTool = Callable[[Sequence[str], Path, Mapping[str, str]], execrun.ExecResult]
RemoveTree = Callable[[Path], None]


def resolve_candidate(explicit: str | Path | None = None) -> Path:
    """The candidate shipit executable every Tree invocation runs under
    ``SHIPIT_EXEC`` (ADR-0033's sanctioned override — the build under test,
    never the consumer's pin).

    ``explicit`` is the ``--shipit-exec`` flag: a deliberate executable PATH,
    taken literally (never a ``PATH`` lookup — that would reintroduce the
    ambient/consumer-build ambiguity the override exists to bypass). Absent, the
    candidate is the RUNNING build's own entrypoint — the sweep verifies the
    build that launched it — resolved from ``sys.argv[0]`` through ``PATH``:
    a console-script launch off ``PATH`` leaves ``argv[0]`` a bare name
    (``"shipit"``), not a cwd file, yet the running build is executable.
    Refuses (:class:`SweepError`) when neither resolves to an executable file.
    """
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
    """Cut one freeform sweep Tree for ``entry`` off its local source checkout.

    The source checkout — ``<source_root>/<path>``, the portfolio's declared
    layout — is the ``--reference`` donor and the origin resolver
    (:func:`~shipit.tree.create.create_from_source`); the Tree itself clones
    from, and points origin at, the repo's GitHub URL, cut from
    ``origin/main`` on a throwaway ``fleet-sweep-…`` branch.
    """
    source = (source_root / entry.path).expanduser()
    if not source.is_dir():
        raise SweepError(
            f"source checkout for {entry.repo} missing at {source} — the sweep "
            "cuts each Tree off the portfolio's local checkout layout "
            "(--source-root)"
        )
    agent_hash = new_agent_hash()
    spec = TreeSpec(
        repo=identity.repo_from_slug(entry.repo),
        agent_hash=agent_hash,
        branch=f"fleet-sweep-{agent_hash}",
    )
    return create_from_source(spec, source_repo=source)


def _run_tool(
    argv: Sequence[str], cwd: Path, env: Mapping[str, str]
) -> execrun.ExecResult:
    """One cell's Exec through the one runner (ADR-0028): ``check=False`` — a
    nonzero rc is the cell's verdict, not a transport failure — at the stated
    :data:`SWEEP_TIMEOUT`.

    The child runs under a SCRUBBED, replacement environment: the sweep's own
    ``PIXI_*`` / Conda-activation project pointers (leaked when the sweep is
    launched from shipit's pixi env) are dropped via the one shared scrubber
    (:func:`shipit.pixienv.scrub_env`, the same policy Tree provisioning and
    spawn launch rely on), then ``env`` — the ``SHIPIT_EXEC`` override — is
    layered on top and passed with ``replace_env=True`` so no leaked pointer can
    creep back in via a merge over ``os.environ`` and bind the swept tool to the
    coordinator checkout instead of its freshly provisioned Tree. ``scrub_env``
    keeps ``PATH`` and every non-leak var, so the Tree's toolchains still
    resolve — the scrub drops only the parent-project pointers. The kept PATH
    can still FRONT the coordinator env's bin dir (an activation edits PATH;
    a scrub cannot un-edit it) — resolution-ORDER hermeticity is
    :func:`_run_cell`'s job, which routes a provisioned Tree's invocation
    through the Tree's own pixi env so its bin dir wins."""
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
    """Tear one sweep Tree down. Best-effort: a failed removal is a WARNING
    (the central root's gc ladder reclaims strays), never a sweep failure."""
    try:
        shutil.rmtree(path)
    except OSError:
        logger.warning("fleet sweep: could not remove Tree %s", path, exc_info=True)


def _failed_row(
    entry: PortfolioEntry, tools: Sequence[str], plans: Sequence[ToolPlan], error: str
) -> RepoResult:
    """The row for a repo whose Tree could not be cut: every applicable cell
    red (or declared expected-fail) with the create error as its output —
    an unverifiable repo is never a silent gap in the matrix."""
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
    tree_root: Path,
    *,
    candidate: Path,
    run_tool: RunTool,
) -> Cell:
    """Execute one applicable cell: the Tree's managed launcher + the tool's
    subcommand, under the ``SHIPIT_EXEC`` override (ADR-0033).

    A provisioned Tree's invocation is routed THROUGH its own pixi env
    (``pixi run --manifest-path <tree>/pixi.toml -- bin/shipit …`` — the
    :func:`shipit.pixienv.run_argv` form, the same launch-path fix as
    :func:`shipit.spawn.launch.pixi_wrap`, ``docs/dev/pixi.lex`` §7), gated on
    :func:`shipit.pixienv.has_default_env` like every routing site. The leak
    this closes (the round-0 shipit self-row red): the swept tool's dispatched
    runners (``pytest``, ``cargo nextest``, …) resolve off PATH, and the
    sweep's inherited PATH still FRONTS the coordinator checkout's own pixi
    env — the scrub drops the parent's project *pointers* but cannot un-edit
    PATH — so a bare launcher invocation let a swept shipit Tree's pytest run
    import the CANDIDATE build's package instead of the Tree's own source.
    The wrap puts the Tree's env bin first, so every dispatched child resolves
    the Tree's OWN provisioning, while ``SHIPIT_EXEC`` (passed through the
    activation untouched) still picks the candidate BUILD — ADR-0033's
    boundary: the override selects the build, never the environment. A Tree
    with no provisioned env (a non-pixi repo) keeps the bare launcher argv;
    its toolchains never resolved through pixi to begin with. The RECORDED
    argv is the wrapped form — the hermetic invocation a fix agent must
    reproduce with.
    """
    launcher = tree_root / "bin" / "shipit"
    argv: tuple[str, ...] = (str(launcher), *TOOL_ARGS[tool])
    if pixienv.has_default_env(tree_root):
        argv = tuple(pixienv.run_argv(list(argv), tree_root))
    if not launcher.is_file():
        status, reason = cell_status(1, entry.expect_verify_fail)
        return Cell(
            tool,
            status,
            reason=reason,
            argv=argv,
            cwd=str(tree_root),
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
        # A launch failure (missing interpreter, timeout, OS error) is the
        # hard-fail form of a red cell — recorded with the runner's whole
        # diagnosis, never a skip.
        status, reason = cell_status(1, entry.expect_verify_fail)
        return Cell(
            tool,
            status,
            reason=reason,
            argv=argv,
            cwd=str(tree_root),
            rc=exc.rc,
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
    """One matrix row: cut the Tree, derive applicability from ITS
    declarations, run each applicable cell, tear the Tree down."""
    try:
        tree = create_tree(entry)
    except (SweepError, ValueError, OSError, execrun.ExecError) as exc:
        logger.error(
            "fleet sweep: tree create failed",
            exc_info=True,
            extra={"sweep_repo": entry.repo},
        )
        # Applicability still derives from declarations — best-effort off the
        # SOURCE checkout (same declarations as origin/main, near enough for
        # a row whose every applicable cell is the create failure).
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
                    entry, plan.tool, tree_root, candidate=candidate, run_tool=run_tool
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
    """Run the fleet verification sweep over ``entries`` and assemble the
    matrix report.

    Sequential by design: one Tree at a time keeps the disk/provisioning
    footprint bounded and the flow-log trail linear. ``create_tree`` /
    ``run_tool`` / ``remove_tree`` inject the effectful boundaries for tests;
    the defaults are the real Tree machinery and the one Exec runner.

    Refuses (:class:`SweepError`) when ``tools`` selects nothing from
    :data:`SWEEP_TOOLS` — an empty selection would run nothing yet report a
    trivially green matrix, a false exit-gate pass.
    """
    if create_tree is None:

        def create_tree(entry: PortfolioEntry) -> Tree:
            return _create_tree(entry, source_root=source_root)

    run_tool = run_tool or _run_tool
    remove_tree = remove_tree or _remove_tree
    selected = tuple(tool for tool in SWEEP_TOOLS if tool in tools)
    if not selected:
        # A domain refusal (ADR-0030): an empty selection — an empty ``tools``
        # or only names outside SWEEP_TOOLS — would run nothing yet report 0 red
        # cells, a FALSE green exit gate. The verb defaults empty→all and
        # click.Choice rejects unknowns, so this guards the direct API caller.
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
