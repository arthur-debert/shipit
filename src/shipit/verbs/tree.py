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

import click

from .. import gh
from ..tree.create import Tree, create, new_agent_hash
from ..tree.layout import TreeSpec


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
