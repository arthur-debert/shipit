"""``shipit tree`` — the Tree command group (PRD docs/prd/where-to-do-work.md).

A NESTED click group: ``shipit tree <verb>`` is the surface for isolated Trees.
``create`` exposes the full spec grammar (naming.lex §3) — the ``--issue N``,
``--epic E --ws N``, and freeform ``--branch NAME`` shapes — each resolved by the
pure planner; ``list`` / ``remove`` / ``gc`` are sibling verbs, each added as one
``from .`` import + one ``@tree.command`` block, so concurrent work streams touch
disjoint lines.

The verb is thin: resolve the ambient repo identity (org/repo, local checkout,
origin URL) at the gh/git boundary, hand a typed :class:`TreeSpec` to the pure
planner + effectful orchestrator, and print the READY summary. All the real logic
lives in :mod:`shipit.tree`.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

import click

from .. import gh
from ..tree import cleanup, layout, registry
from ..tree.cleanup import Cleanup
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
    default=None,
    help="Issue shape: provision a Tree for issue N (branch fix/<n>-<slug>).",
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
    help="Freeform shape: provision a Tree on branch NAME, cut from origin/main.",
)
@click.option(
    "--slug",
    default="",
    help="Optional short slug for the branch/dir name; sanitized to lowercase-dashed.",
)
def create_cmd(
    issue: int | None,
    epic: str | None,
    ws: int | None,
    branch: str | None,
    slug: str,
) -> None:
    """Provision an isolated Tree and print its READY summary.

    Accepts exactly ONE of three shapes (naming.lex §3); the planner resolves each
    to a concrete dir/branch/base:

    \b
    - ``--issue N [--slug S]``       → branch ``fix/<n>-<slug>``, base ``origin/main``
    - ``--epic E --ws N [--slug S]`` → branch ``E/WSnn``, base ``origin/E/umbrella``
    - ``--branch NAME``              → branch ``NAME`` verbatim, base ``origin/main``

    Creates a fully-independent clone under the central root on the resolved branch,
    then prints ``READY {path, branch, base}``. The clone's ``origin`` is the repo's
    GitHub URL, so ``git``/``gh`` work inside it unchanged. Giving zero shapes, more
    than one, or a partial epic (only one of ``--epic``/``--ws``) is a clean exit-1
    error.
    """
    raise SystemExit(
        run_create(issue=issue, epic=epic, ws=ws, branch=branch, slug=slug)
    )


def run_create(
    *,
    issue: int | None = None,
    epic: str | None = None,
    ws: int | None = None,
    branch: str | None = None,
    slug: str = "",
) -> int:
    """Select a shape -> resolve repo identity -> plan -> clone -> print READY.

    Returns 0 on success; 1 with a clean stderr message when the flag grammar is
    wrong (zero/multiple/partial shapes), the command is not run inside a GitHub
    checkout, the planner rejects the spec, or a git/gh call fails.
    """
    try:
        _select_shape(issue=issue, epic=epic, ws=ws, branch=branch)
    except ValueError as exc:
        print(f"tree create: {exc}", file=sys.stderr)
        return 1

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
        epic=epic,
        ws=ws,
        branch=branch,
        slug=slug,
    )
    try:
        result = create(spec, source_repo=root, github_url=url)
    except (gh.GhError, ValueError) as exc:
        print(f"tree create: {exc}", file=sys.stderr)
        return 1
    _emit_ready(result)
    return 0


def _select_shape(
    *,
    issue: int | None,
    epic: str | None,
    ws: int | None,
    branch: str | None,
) -> str:
    """Validate that exactly one of the three shapes is requested; return its name.

    The flag-grammar gate (a CLI concern): exactly one of {``--issue``,
    ``--epic`` + ``--ws``, ``--branch``} must be given, and the epic shape needs BOTH
    halves. Any violation raises a clean :class:`ValueError` (no planner-module
    prefix), surfaced as an exit-1 stderr message by :func:`run_create`. The per-shape
    DOMAIN invariants — epic-code format, positive work-stream number, non-empty
    freeform name — stay the planner's job; it re-validates the built spec and its
    ``ValueError`` is caught the same way.
    """
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


@tree.command(name="remove")
@click.argument("target")
def remove_cmd(target: str) -> None:
    """Delete a single Tree identified by TARGET (its path or its directory name).

    A Tree is a disposable, fully-independent clone, so removing it is just deleting
    its directory — no worktree to prune, no shared state to corrupt. TARGET must
    resolve to exactly one Tree under the central root; an unknown or ambiguous TARGET
    is a clean error (the Tree is left untouched).
    """
    raise SystemExit(run_remove(target))


def run_remove(target: str) -> int:
    """Resolve TARGET to one Tree and delete its clone dir. Returns an exit code.

    Returns 0 after removing the one matching Tree; 1 with a stderr message when no
    Tree matches or more than one does (never guess which to delete).
    """
    root = layout.central_root()
    records = registry.scan(root)
    matches = _match_trees(records, target)
    if not matches:
        print(f"tree remove: no Tree matching {target!r}", file=sys.stderr)
        return 1
    if len(matches) > 1:
        paths = ", ".join(record.path for record in matches)
        print(
            f"tree remove: {target!r} is ambiguous — matches {paths}", file=sys.stderr
        )
        return 1
    record = matches[0]
    try:
        shutil.rmtree(record.path)
    except OSError as exc:
        print(f"tree remove: could not remove {record.path}: {exc}", file=sys.stderr)
        return 1
    print(f"REMOVED {record.path}")
    return 0


def _match_trees(records: list[TreeRecord], target: str) -> list[TreeRecord]:
    """Trees whose absolute path equals TARGET or whose dir name equals TARGET.

    Matching on the basename lets a coordinator name a Tree by its short id
    (``7-aaaa``) without typing the whole central-root path; matching the full path
    stays exact. The path form takes precedence — an exact path match is unambiguous.
    """
    by_path = [record for record in records if record.path == target]
    if by_path:
        return by_path
    return [record for record in records if Path(record.path).name == target]


@tree.command(name="gc")
def gc_cmd() -> None:
    """Sweep the central root: remove only provably-safe Trees, list ambiguous ones.

    Scans every Tree, classifies the fleet, then deletes ONLY the Trees whose PR is
    merged, working tree clean, nothing unpushed, and which are aged past the
    threshold. Trees that merely look abandoned are LISTED as stale (never deleted),
    and anything with live or local work is left untouched. Conservative by default.
    """
    raise SystemExit(run_gc())


def run_gc() -> int:
    """Scan, classify, then remove only the removable set and list the stale set.

    Returns 0 always: an empty root or a fleet with nothing to reclaim is a valid
    outcome, not an error. Repo identity is irrelevant — ``gc`` spans the whole
    central root, like ``list``.
    """
    root = layout.central_root()
    records = registry.scan(root)
    pr_states = {record.path: _pr_state(record) for record in records}
    decision = cleanup.classify(records, now=time.time(), pr_states=pr_states)
    _emit_gc(decision)
    return 0


def _pr_state(record: TreeRecord) -> str | None:
    """The PR's remote state (``"MERGED"`` / ``"OPEN"`` / ``"CLOSED"`` …) for one Tree.

    Reads through the same ``gh`` boundary the registry uses, from inside the clone, so
    ``gc`` sees the authoritative merge state rather than re-parsing the rendered label.
    A draft open PR is normalized to ``"DRAFT"`` (mirroring ``registry._pr_label``) so the
    fleet has ONE state vocabulary and ``cleanup.classify``'s draft branch is reachable.
    ``None`` when the Tree has no branch or no PR.
    """
    if not record.branch:
        return None
    pr = gh.pr_for_head(record.branch, cwd=record.path)
    if not pr:
        return None
    state = pr.get("state")
    if not isinstance(state, str):
        return None
    state = state.upper()
    if state == "OPEN" and pr.get("isDraft"):
        return "DRAFT"
    return state


def _emit_gc(decision: Cleanup) -> None:
    """Delete the removable Trees, then report what was removed, kept stale, or kept.

    Deletion is best-effort per Tree: if one ``rmtree`` fails (a read-only file, a lock,
    a vanished dir), the failure goes to stderr and the sweep CONTINUES to the next Tree
    rather than aborting mid-fleet. The summary's ``removed`` count reflects what actually
    came off disk, not what was merely planned.
    """
    removed = 0
    for record in decision.removable:
        try:
            shutil.rmtree(record.path)
        except OSError as exc:
            print(f"FAILED  {record.path}: {exc}", file=sys.stderr)
            continue
        removed += 1
        print(f"REMOVED {record.path}")
    for record in decision.stale:
        print(f"STALE   {record.path} (ambiguous — left for review, not removed)")
    stale = len(decision.stale)
    kept = len(decision.keep)
    print(f"gc: removed {removed}, stale {stale}, kept {kept}")
