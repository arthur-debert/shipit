"""``shipit spawn`` — shipit-owned subagent spawning (ADR-0017 / ADR-0019).

A NESTED click group mirroring ``shipit tree``: ``shipit spawn <verb>`` is the
surface for launching backend-agent **Runs** that shipit owns end to end. The
first verb, ``subagent``, is the walking skeleton (TRE03-WS01): it creates a write
**Tree** by REUSING the tree-creation path, then launches a headless ``claude``
child rooted in that Tree per the ADR-0019 launch contract, and observes the child
did its (trivial) work IN the Tree.

The verb is thin: resolve the ambient repo identity at the gh/git boundary, hand a
typed :class:`TreeSpec` to the existing pure planner + effectful orchestrator
(:func:`shipit.tree.create.create`) — Tree creation is never reimplemented — then
launch the child through the unit-testable :mod:`shipit.spawn.launch` seam.

**Fail-closed** (ADR-0017/0019): a Tree-creation error fails the spawn loud —
NEVER a silent fallback to a native ``git worktree``. The launcher is reached only
after a Tree exists, so a failed ``create`` short-circuits to a clean exit-1 and
nothing is ever launched against the parent checkout.
"""

from __future__ import annotations

import json
import sys

import click

from .. import gh, proc
from ..spawn import launch
from ..tree.create import Tree, create, new_agent_hash
from ..tree.layout import TreeSpec

#: The backends ``spawn subagent`` can launch today. Only ``claude`` is wired
#: (ADR-0019 is the claude-backend contract); codex / antigravity are a future WS
#: (#153). A ``click.Choice`` over this gates the CLI, and :func:`run_subagent`
#: re-checks it so the programmatic entry point is guarded too.
SUPPORTED_BACKENDS = ("claude",)


@click.group(
    name="spawn",
    help=(
        "Spawn backend-agent Runs shipit owns end to end.\n\n"
        "`subagent` creates a write Tree and launches a headless claude child "
        "rooted in it (ADR-0019), so the Run's work happens in the Tree, never the "
        "parent checkout. `--help` is the map."
    ),
)
def spawn() -> None:
    """Root of the ``spawn`` subcommand group; verbs are attached below."""


@spawn.command(name="subagent")
@click.option(
    "--repo",
    required=True,
    help=(
        "Target repo (e.g. shipit). The skeleton spawns from the ambient checkout "
        "and uses this to guard against running in the wrong one; multi-repo "
        "selection is a later WS."
    ),
)
@click.option(
    "--epic",
    required=True,
    help="Epic code the Run belongs to, e.g. TRE03 (rides the Tree branch E/WSnn).",
)
@click.option(
    "--ws",
    type=int,
    required=True,
    help="Work stream number N (the WSnn half of the Tree branch E/WSnn).",
)
@click.option(
    "--role",
    required=True,
    help=(
        "The Run's role, passed verbatim to `claude --agent <role>` (ADR-0019 §2) — "
        "load-bearing: it populates the hook payload so the guard allows the Run's "
        "own edits. Needs a committed .claude/agents/<role>.md def in the Tree."
    ),
)
@click.option(
    "--backend",
    type=click.Choice(SUPPORTED_BACKENDS),
    default="claude",
    show_default=True,
    help="The agent backend to launch. Only `claude` is wired today (ADR-0019).",
)
def subagent_cmd(repo: str, epic: str, ws: int, role: str, backend: str) -> None:
    """Create a write Tree and launch a backend-agent Run rooted in it.

    The walking skeleton (TRE03-WS01): resolve the ambient repo identity, create a
    write Tree by reusing ``shipit tree create`` (kept dumb — base ``origin/main``;
    the epic-grouped umbrella base is a later WS), then launch a headless ``claude``
    child whose ``cwd`` IS that Tree (ADR-0019). The child performs one trivial,
    verifiable action — writing a sentinel file — and ``spawn`` confirms it happened
    in the Tree, not the parent checkout.

    Fail-closed: a Tree-creation error exits 1 loudly — never a silent fallback to a
    native ``git worktree``. A child that exits nonzero, or that exits 0 without
    leaving the sentinel in the Tree, is also a clean exit-1.
    """
    raise SystemExit(
        run_subagent(repo=repo, epic=epic, ws=ws, role=role, backend=backend)
    )


def run_subagent(
    *,
    repo: str,
    epic: str,
    ws: int,
    role: str,
    backend: str = "claude",
    launcher: launch.Runner | None = None,
) -> int:
    """Resolve identity → create the Tree → launch the child → observe. Returns a code.

    Returns 0 once a headless ``claude`` child has run rooted in a freshly-created
    write Tree and left its sentinel there. Returns 1 with a clean stderr message
    (never a traceback) when the backend is unsupported, ``--ws`` is not positive,
    ``--repo`` disagrees with the ambient checkout, the command is not run inside a
    GitHub checkout, a git/gh call fails, **Tree creation fails** (fail-closed — no
    native-worktree fallback), the child exits nonzero, or the child exits 0 without
    writing the sentinel into the Tree.

    ``launcher`` injects the subprocess seam so the launch contract is unit-tested
    without spawning a real ``claude``; ``None`` uses the real
    :func:`shipit.spawn.launch._subprocess_runner`.
    """
    if backend not in SUPPORTED_BACKENDS:
        supported = ", ".join(SUPPORTED_BACKENDS)
        print(
            f"spawn subagent: unsupported backend {backend!r} (supported: "
            f"{supported}); non-claude backends are a later WS (#153).",
            file=sys.stderr,
        )
        return 1
    if ws < 1:
        print(
            f"spawn subagent: --ws must be a positive integer (got {ws})",
            file=sys.stderr,
        )
        return 1

    root = gh.repo_root()
    if not root:
        print("spawn subagent: not inside a git checkout", file=sys.stderr)
        return 1
    try:
        org_repo = gh.current_repo()
        url = gh.git_remote_url(cwd=root)
    except gh.GhError as exc:
        print(f"spawn subagent: {exc}", file=sys.stderr)
        return 1

    if "/" not in org_repo:
        # A well-formed ambient identity is always "org/repo"; a slashless value
        # would put the whole string in ``org`` and leave ``repo_name`` empty, which
        # can slip past the --repo guard below and feed an empty repo into the
        # TreeSpec. Refuse it loud rather than build a malformed Tree.
        print(
            f"spawn subagent: ambient repo {org_repo!r} is not in org/repo form; "
            "cannot resolve the target repo identity.",
            file=sys.stderr,
        )
        return 1

    org, _, repo_name = org_repo.partition("/")
    # --repo is the wrong-checkout guard, not a repo SELECTOR yet: the skeleton
    # resolves identity from the ambient checkout, so a --repo that names a
    # different repo is refused rather than silently ignored. Multi-repo selection
    # is a later WS.
    if repo not in (repo_name, org_repo):
        print(
            f"spawn subagent: --repo {repo!r} but the ambient checkout is "
            f"{org_repo!r}; the skeleton spawns from the target checkout "
            "(multi-repo selection is a later WS).",
            file=sys.stderr,
        )
        return 1

    # Skeleton Tree: the slash-namespaced E/WSnn branch via the FREEFORM shape, so
    # the base stays the dumb origin/main; the epic-grouped umbrella base
    # (origin/E/umbrella) is the semantic path a later WS swaps in.
    branch = f"{epic}/WS{ws:02d}"
    spec = TreeSpec(
        org=org,
        repo=repo_name,
        agent_hash=new_agent_hash(),
        branch=branch,
    )
    try:
        tree = create(spec, source_repo=root, github_url=url)
    except (gh.GhError, ValueError, proc.ProcError, OSError) as exc:
        # Fail-closed (ADR-0017/0019): a Tree-creation error fails the spawn LOUD.
        # There is deliberately no native-worktree fallback — the launcher below is
        # unreachable unless a real Tree exists, so a failed create can never end up
        # launching a Run against the parent checkout.
        print(f"spawn subagent: tree creation failed: {exc}", file=sys.stderr)
        return 1

    # Launch the headless claude child rooted in the Tree (ADR-0019 contract): the
    # cwd IS the Tree, ANTHROPIC_API_KEY is scrubbed, and --agent <role> conveys the
    # role to the harness so the guard allows the Run's own edits.
    cmd = launch.build_command(launch.skeleton_task(role), role)
    try:
        result = launch.launch(
            cmd, cwd=tree.path, env=launch.child_env(), runner=launcher
        )
    except OSError as exc:
        # The child never started: `claude` is missing/not on PATH, or the Tree path
        # became unavailable. The Tree exists, so this is a launch failure, not the
        # fail-closed create path — still a clean exit-1, never an escaping traceback.
        print(f"spawn subagent: {exc}", file=sys.stderr)
        return 1
    if result.returncode != 0:
        detail = result.stderr.strip()
        print(
            f"spawn subagent: claude child exited {result.returncode}"
            + (f"\n{detail}" if detail else ""),
            file=sys.stderr,
        )
        return 1
    if not launch.sentinel_present(tree.path):
        print(
            "spawn subagent: child exited 0 but left no sentinel at "
            f"{launch.sentinel_path(tree.path)}; the Run did not run in the Tree.",
            file=sys.stderr,
        )
        return 1

    _emit_spawned(tree, role=role, backend=backend)
    return 0


def _emit_spawned(tree: Tree, *, role: str, backend: str) -> None:
    """Print the SPAWNED summary: a ``SPAWNED`` line plus the Run's coordinates as JSON."""
    print("SPAWNED")
    print(
        json.dumps(
            {
                "tree": tree.path,
                "branch": tree.branch,
                "base": tree.base,
                "role": role,
                "backend": backend,
                "sentinel": str(launch.sentinel_path(tree.path)),
            },
            indent=2,
        )
    )
