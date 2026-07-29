"""``shipit tree`` — the Tree command group: create, list, remove, gc."""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import fields

import click

from .. import execrun, git, identity
from ..tree import cleanup, fleet, gc, layout, registry, removal
from ..tree.create import Tree, create, new_tree_naming
from ..tree.layout import TreeSpec
from ..tree.removal import GateAction, RemovalError
from ._errors import cli_errors
from ._params import DURATION, json_option
from ._render import emit

logger = logging.getLogger("shipit.tree")


@click.group(
    name="tree",
    help=(
        "Isolated Trees — independent clones a write-session works in.\n\n"
        "`create` provisions a ready Tree (its own checkout, on a fresh branch) "
        "so concurrent agents never collide on one working tree. `--help` is the map."
    ),
)
def tree() -> None:
    """Root of the ``tree`` subcommand group; verbs are attached below."""


@tree.command(name="create")
@click.option(
    "--issue",
    type=int,
    default=None,
    help="Issue shape: provision a Tree for issue N (branch issues/<n>/<session>).",
)
@click.option(
    "--session",
    default="work",
    show_default=True,
    help=(
        "Issue shape: session name in the branch issues/<n>/<session>. The suffix "
        "keeps issues/<n>/ a ref directory so a +1 session on the same issue "
        "(e.g. --session onboard) coexists with the default `work` (naming.lex §3). "
        "Ignored by the --epic/--ws and --branch shapes."
    ),
)
@click.option(
    "--epic",
    default=None,
    help="Epic shape (with --ws): epic code E, e.g. HAR02 (branch E/WSnn).",
)
@click.option(
    "--ws",
    type=int,
    default=None,
    help="Epic shape (with --epic): work stream number N (branch E/WSnn).",
)
@click.option(
    "--branch",
    default=None,
    help=(
        "Freeform shape: provision a Tree on branch NAME. Existing remote heads "
        "start from origin/NAME; new branches start from origin/main."
    ),
)
@click.option(
    "--slug",
    default="",
    help=(
        "Optional short label, sanitized to lowercase-dashed. Rides the Tree DIR leaf "
        "only (never the branch): --issue and --epic both keep their canonical branch "
        "(issues/<n>/<session>, E/WSnn); ignored for --branch."
    ),
)
def create_cmd(
    issue: int | None,
    session: str,
    epic: str | None,
    ws: int | None,
    branch: str | None,
    slug: str,
) -> None:
    """Provision an isolated Tree and print its READY summary."""
    raise SystemExit(
        run_create(
            issue=issue, session=session, epic=epic, ws=ws, branch=branch, slug=slug
        )
    )


def run_create(
    *,
    issue: int | None = None,
    session: str = "work",
    epic: str | None = None,
    ws: int | None = None,
    branch: str | None = None,
    slug: str = "",
) -> int:
    try:
        _select_shape(issue=issue, epic=epic, ws=ws, branch=branch)
    except ValueError as exc:
        print(f"tree create: {exc}", file=sys.stderr)
        return 1

    root = git.repo_root()
    if not root:
        print("tree create: not inside a git checkout", file=sys.stderr)
        return 1
    try:
        repo_identity = identity.resolve_repo(root)
        url = git.remote_url(cwd=root)
        base = _freeform_base(branch, cwd=root) if branch is not None else None
    except (execrun.ExecError, ValueError) as exc:
        print(f"tree create: {exc}", file=sys.stderr)
        return 1

    spec = TreeSpec(
        repo=repo_identity,
        **new_tree_naming("claude"),
        issue=issue,
        session=session,
        epic=epic,
        ws=ws,
        branch=branch,
        base=base,
        slug=slug,
    )
    try:
        result = create(spec, source_repo=root, github_url=url)
    except (ValueError, execrun.ExecError, OSError) as exc:
        logger.error("tree create failed", exc_info=True)
        print(f"tree create: {exc}", file=sys.stderr)
        return 1
    _emit_ready(result)
    return 0


def _freeform_base(branch: str, *, cwd: str) -> str | None:
    """The base override for a freeform branch, or None if it has no remote ref."""
    if not layout.sanitize_slug(branch):
        return None
    return f"origin/{branch}" if git.remote_branch_exists(branch, cwd=cwd) else None


def _select_shape(
    *,
    issue: int | None,
    epic: str | None,
    ws: int | None,
    branch: str | None,
) -> str:
    """The name of the single requested shape; raises if not exactly one."""
    has_epic = epic is not None or ws is not None
    shapes = [
        name
        for name, present in (
            ("epic", has_epic),
            ("issue", issue is not None),
            ("branch", branch is not None),
        )
        if present
    ]
    if len(shapes) != 1:
        raise ValueError(
            "exactly one shape must be given — --issue N, --epic E --ws N, "
            f"or --branch NAME (got {', '.join(shapes) or 'none'})"
        )
    if has_epic and (epic is None or ws is None):
        raise ValueError(
            f"the epic shape needs both --epic and --ws (got epic={epic!r}, ws={ws!r})"
        )
    return shapes[0]


def _emit_ready(result: Tree) -> None:
    """Print a ``READY`` line plus the ``{path, branch, base}`` JSON."""
    print("READY")
    print(
        json.dumps(
            {"path": result.path, "branch": result.branch, "base": result.base},
            indent=2,
        )
    )


@tree.command(name="list")
@json_option
def list_cmd(as_json: bool) -> None:
    """List every Tree under the central root with its at-a-glance state."""
    raise SystemExit(run_list(as_json=as_json))


@cli_errors
def run_list(*, as_json: bool = False) -> int:
    """Render the Tree fleet; returns an exit code."""
    records = registry.scan(layout.central_root())
    emit(fleet.build(records, now=time.time()), format_fleet, as_json=as_json)
    return 0


_LIST_COLUMNS: tuple[tuple[str, Callable[[fleet.FleetTree], str]], ...] = (
    ("PATH", lambda row: row.path),
    ("CREATED", lambda row: row.created or "-"),
    ("BRANCH", lambda row: row.branch or "(detached)"),
    ("BASE", lambda row: _format_base(row)),
    ("AGE", lambda row: _format_age(row.age_seconds)),
    ("DIRTY", lambda row: "dirty" if row.dirty else "clean"),
)


def format_fleet(result: fleet.Fleet) -> str:
    """The fleet as a fixed-width table, or a hint when empty."""
    if not result.trees:
        return "No Trees under the central root."
    headers = [header for header, _ in _LIST_COLUMNS]
    rows = [[cell(row) for _, cell in _LIST_COLUMNS] for row in result.trees]
    all_rows = [headers, *rows]
    widths = [max(len(row[col]) for row in all_rows) for col in range(len(headers))]
    return "\n".join(_format_row(row, widths) for row in all_rows)


def _format_base(row: fleet.FleetTree) -> str:
    """The BASE cell: the upstream ref, annotated with ahead/behind when diverged."""
    base = row.base or "-"
    marks = []
    if row.ahead:
        marks.append(f"+{row.ahead}")
    if row.behind:
        marks.append(f"-{row.behind}")
    return f"{base} ({'/'.join(marks)})" if marks else base


def _format_row(cells: list[str], widths: list[int]) -> str:
    return "  ".join(cell.ljust(widths[col]) for col, cell in enumerate(cells)).rstrip()


def _format_age(seconds: float) -> str:
    """A compact human age (``"3d"``, ``"4h"``, ``"5m"``, ``"12s"``)."""
    secs = int(max(seconds, 0))
    for unit_seconds, suffix in ((86400, "d"), (3600, "h"), (60, "m")):
        if secs >= unit_seconds:
            return f"{secs // unit_seconds}{suffix}"
    return f"{secs}s"


@tree.command(name="remove")
@click.argument("target")
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help=(
        "Skip the confirmation prompt unconditionally. The non-interactive default: "
        "removing a Tree with uncommitted or unpushed work without a TTY requires this."
    ),
)
def remove_cmd(target: str, yes: bool) -> None:
    """Delete a single Tree identified by TARGET (its path or its directory name)."""
    raise SystemExit(run_remove(target, assume_yes=yes))


def _stdin_is_tty() -> bool:
    """Whether stdin is an interactive terminal, robust to a missing stream."""
    stream = sys.stdin
    if stream is None or getattr(stream, "closed", False):
        return False
    try:
        return stream.isatty()
    except (ValueError, OSError):
        return False


@cli_errors
def run_remove(
    target: str,
    *,
    assume_yes: bool = False,
    confirm: Callable[[str], bool] | None = None,
    is_tty: Callable[[], bool] | None = None,
) -> int:
    """Resolve TARGET to one Tree and delete its clone dir; returns an exit code."""
    if confirm is None:
        confirm = click.confirm
    if is_tty is None:
        is_tty = _stdin_is_tty
    records = registry.scan(layout.central_root())
    record = removal.resolve_target(records, target)
    gate = removal.gate(record, assume_yes=assume_yes, interactive=is_tty())
    if gate.action is GateAction.REFUSE:
        raise RemovalError(gate.reason)
    if gate.action is GateAction.CONFIRM and not confirm(gate.prompt or ""):
        raise RemovalError(f"aborted — {record.path} left untouched")
    removal.remove(record)
    print(f"REMOVED {record.path}")
    return 0


@tree.command(name="gc")
@click.option(
    "--dry-run",
    is_flag=True,
    help=(
        "Preview only: print the removable/keep partition for the whole fleet "
        "and delete NOTHING. The preview is the exact decision the real sweep acts on."
    ),
)
@click.option(
    "--threshold",
    default=None,
    type=DURATION,
    metavar="DURATION",
    help=(
        "How long a Tree must be IDLE — nothing written anywhere in it and no commit "
        "made — before it counts as abandoned, as a human duration (e.g. 48h, 36h, "
        "90m). Defaults to 48h when omitted. A Tree with uncommitted changes or "
        "unpushed commits is kept no matter how idle."
    ),
)
def gc_cmd(dry_run: bool, threshold: float | None) -> None:
    """Sweep the central root: remove only provably-safe Trees."""
    raise SystemExit(run_gc(dry_run=dry_run, idle_threshold_seconds=threshold))


@cli_errors
def run_gc(
    *, dry_run: bool = False, idle_threshold_seconds: float | None = None
) -> int:
    """Preview or sweep the gc plan; returns an exit code."""
    plan = gc.plan_fleet(
        layout.central_root(),
        idle_threshold_seconds=(
            cleanup.IDLE_THRESHOLD_SECONDS
            if idle_threshold_seconds is None
            else idle_threshold_seconds
        ),
    )
    if dry_run:
        _render_gc_preview(plan)
        return 1 if plan.incomplete else 0
    result = gc.sweep(plan, on_removed=_print_removed)
    _render_gc_result(result)
    return 1 if result.incomplete else 0


def _print_removed(path: str) -> None:
    """The streaming sink: announce one Tree the sweep just took off disk."""
    print(f"REMOVED {path}", flush=True)


def _render_gc_result(result: gc.GcResult) -> None:
    """Render the sweep's tail: the failures and the summary."""
    for failure in result.failed:
        print(f"FAILED  {failure.path}: {failure.error}", file=sys.stderr)
    counts = f"removed {len(result.removed)}, kept {result.kept}"
    print(f"gc: {_lead(result)}{counts}")
    _render_incomplete_view(result, verb="judged")


def _lead(view: gc.GcPlan | gc.GcResult) -> str:
    """The summary's leading clause — empty for a complete view, loud otherwise."""
    if not view.incomplete:
        return ""
    return (
        f"INCOMPLETE — {view.unexamined} of {view.total} unexamined "
        "(a signal could not be read); "
    )


def _render_incomplete_view(view: gc.GcPlan | gc.GcResult, *, verb: str) -> None:
    """Explain a partially-judged fleet on stderr; print nothing if it was whole."""
    if not view.incomplete:
        return
    print(
        f"gc: {verb} {view.judged} of {view.total}; {view.unexamined} kept UNEXAMINED "
        "— a signal could not be read (the unpushed-commit list, the activity walk, or "
        "HEAD's commit stamp), so those Trees were kept without a verdict, not judged "
        "safe. This verdict covers only part of the root.",
        file=sys.stderr,
    )
    print(
        "gc: every one of those signals is read from the Tree itself, so the likeliest "
        "causes are local — a permissions change, a vanished mount, or a Tree being "
        "written as it was read. Re-running usually clears a transient failure.",
        file=sys.stderr,
    )
    print(
        "gc: if the gap covers most of the fleet, suspect the root itself rather than "
        "any one Tree — check that the central root is readable and fully mounted.",
        file=sys.stderr,
    )


def _render_gc_preview(plan: gc.GcPlan) -> None:
    """Render the removable/keep partition without touching disk."""
    counts: list[str] = []
    for field in fields(plan.partition):
        bucket = getattr(plan.partition, field.name)
        for record in bucket:
            print(f"{field.name.upper():<9} {record.path}")
        counts.append(f"{field.name} {len(bucket)}")
    logger.debug("gc --dry-run: %s", ", ".join(counts))
    if plan.incomplete:
        logger.warning(
            "gc --dry-run: would judge %d of %d; %d kept unexamined (a signal could "
            "not be read — incomplete view of the fleet)",
            plan.judged,
            plan.total,
            plan.unexamined,
        )
    print(f"gc --dry-run (no Trees deleted): {_lead(plan)}{', '.join(counts)}")
    _render_incomplete_view(plan, verb="would judge")
