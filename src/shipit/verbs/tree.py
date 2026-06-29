"""``shipit tree`` — the Tree command group (PRD docs/prd/where-to-do-work.md).

A NESTED click group: ``shipit tree <verb>`` is the surface for isolated Trees.
WS01 wires only ``create`` (the thinnest end-to-end thread); ``list`` / ``remove``
/ ``gc`` are added by later workstreams as one ``from .`` import + one
``tree.add_command(...)`` line each, mirroring how the ``pr`` group grows, so
concurrent work streams touch disjoint lines.

The verb is thin: resolve the ambient repo identity (org/repo, local checkout,
origin URL) at the gh/git boundary, hand a typed :class:`TreeSpec` to the pure
planner + effectful orchestrator, and print the READY summary. All the real logic
lives in :mod:`shipit.tree`.
"""

from __future__ import annotations

import json
import sys
import time

import click

from .. import gh
from ..tree import layout, registry
from ..tree.create import Tree, create, new_agent_hash
from ..tree.layout import TreeSpec
from ..tree.registry import TreeRecord


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
    required=True,
    help="Issue number to provision a Tree for (branch fix/<n>-<slug>).",
)
@click.option(
    "--slug",
    default="",
    help="Optional short slug for the branch name; sanitized to lowercase-dashed.",
)
def create_cmd(issue: int, slug: str) -> None:
    """Provision an isolated Tree for issue --issue and print its READY summary.

    Creates a fully-independent clone under the central root on a new
    ``fix/<n>-<slug>`` branch cut from ``origin/main``, then prints
    ``READY {path, branch, base}``. The clone's ``origin`` is the repo's GitHub
    URL, so ``git``/``gh`` work inside it unchanged.
    """
    raise SystemExit(run_create(issue=issue, slug=slug))


def run_create(*, issue: int, slug: str = "") -> int:
    """Resolve repo identity -> plan -> clone -> print READY. Returns an exit code.

    Returns 0 on success; 1 with a clean stderr message when the command is not
    run inside a GitHub checkout or a git/gh call fails.
    """
    root = gh.repo_root()
    if not root:
        print("tree create: not inside a git checkout", file=sys.stderr)
        return 1
    try:
        org_repo = gh.current_repo()
        url = gh.git_remote_url(cwd=root)
    except gh.GhError as exc:
        print(f"tree create: {exc}", file=sys.stderr)
        return 1

    org, _, repo = org_repo.partition("/")
    spec = TreeSpec(
        org=org,
        repo=repo,
        agent_hash=new_agent_hash(),
        issue=issue,
        slug=slug,
    )
    try:
        result = create(spec, source_repo=root, github_url=url)
    except gh.GhError as exc:
        print(f"tree create: {exc}", file=sys.stderr)
        return 1
    _emit_ready(result)
    return 0


def _emit_ready(result: Tree) -> None:
    """Print the READY summary: a ``READY`` line plus the ``{path, branch, base}`` JSON."""
    print("READY")
    print(
        json.dumps(
            {"path": result.path, "branch": result.branch, "base": result.base},
            indent=2,
        )
    )


@tree.command(name="list")
def list_cmd() -> None:
    """List every Tree under the central root with its at-a-glance state.

    Renders the whole fleet — path, branch, base, age, dirty?, PR state — derived
    purely by SCANNING the central root (no manifest); the state is whatever the
    clones on disk say right now.
    """
    raise SystemExit(run_list())


def run_list() -> int:
    """Scan the central root and render the Tree fleet. Returns an exit code.

    Always returns 0: an empty or missing root is a valid "no Trees yet" state, not
    an error. Repo identity is irrelevant here — the central root spans every repo,
    so ``list`` shows the whole fleet (PRD user story 14/22).
    """
    root = layout.central_root()
    records = registry.scan(root)
    _render_list(records, now=time.time())
    return 0


#: The fleet table's columns, in render order: each is ``(header, field-extractor)``.
#: A new column is one tuple here — the renderer widths every column to its content.
_LIST_COLUMNS: tuple[tuple[str, str], ...] = (
    ("PATH", "path"),
    ("BRANCH", "branch"),
    ("BASE", "base"),
    ("AGE", "age"),
    ("DIRTY", "dirty"),
    ("PR", "pr"),
)


def _render_list(records: list[TreeRecord], *, now: float) -> None:
    """Print the fleet as a fixed-width table (one Tree per row), or a hint when empty."""
    if not records:
        print("No Trees under the central root.")
        return
    headers = [header for header, _ in _LIST_COLUMNS]
    rows = [_row_cells(record, now=now) for record in records]
    # Width each column to its widest cell, header included. Pass a single generator
    # to max() (header counts as just another row) rather than star-unpacking one
    # positional arg per row — that materializes an arg list and can hit arg limits.
    all_rows = [headers, *rows]
    widths = [max(len(row[col]) for row in all_rows) for col in range(len(headers))]
    print(_format_row(headers, widths))
    for row in rows:
        print(_format_row(row, widths))


def _row_cells(record: TreeRecord, *, now: float) -> list[str]:
    """The rendered string cells for one Tree row, in :data:`_LIST_COLUMNS` order."""
    values = {
        "path": record.path,
        "branch": record.branch or "(detached)",
        "base": _base_cell(record),
        "age": _format_age(now - record.mtime),
        "dirty": "dirty" if record.dirty else "clean",
        "pr": record.pr or "-",
    }
    return [values[field] for _, field in _LIST_COLUMNS]


def _base_cell(record: TreeRecord) -> str:
    """The BASE cell: the upstream ref, annotated with ahead/behind when diverged."""
    base = record.base or "-"
    marks = []
    if record.ahead:
        marks.append(f"+{record.ahead}")
    if record.behind:
        marks.append(f"-{record.behind}")
    return f"{base} ({'/'.join(marks)})" if marks else base


def _format_row(cells: list[str], widths: list[int]) -> str:
    """Left-justify each cell to its column width and join with two spaces."""
    return "  ".join(cell.ljust(widths[col]) for col, cell in enumerate(cells)).rstrip()


def _format_age(seconds: float) -> str:
    """A compact human age (``"3d"``, ``"4h"``, ``"5m"``, ``"12s"``) for a Tree's mtime."""
    secs = int(max(seconds, 0))
    for unit_seconds, suffix in ((86400, "d"), (3600, "h"), (60, "m")):
        if secs >= unit_seconds:
            return f"{secs // unit_seconds}{suffix}"
    return f"{secs}s"
