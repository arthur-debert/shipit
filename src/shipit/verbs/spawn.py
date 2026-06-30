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
from ..tree.readonly import create_readonly, readonly_plan

#: The backends ``spawn subagent`` can launch today. Only ``claude`` is wired
#: (ADR-0019 is the claude-backend contract); codex / antigravity are a future WS
#: (#153). A ``click.Choice`` over this gates the CLI, and :func:`run_subagent`
#: re-checks it so the programmatic entry point is guarded too.
SUPPORTED_BACKENDS = ("claude",)

#: The role that gets a shared **read-only Tree** + a **Reviewer Run** (ADR-0018)
#: instead of the per-Run write Tree every other role gets: a reviewer is read-only
#: and branch-pinned, so :func:`run_subagent` dispatches on this exact value.
REVIEWER_ROLE = "reviewer"


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
        "own edits. Needs a committed .claude/agents/<role>.md def in the Tree. "
        "`reviewer` is special: it gets a shared READ-ONLY Tree and posts a review "
        "through the PR (ADR-0018), not a write Tree."
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
    Tree. For every role but ``reviewer`` that is a per-Run **write** Tree and the
    proof is the sentinel the child leaves; for ``reviewer`` it is a shared
    **read-only** Tree (ADR-0018) and the proof is the review the child posts THROUGH
    the PR (so no sentinel is checked — the Run reports out-of-band).

    Returns 1 with a clean stderr message (never a traceback) when the backend is
    unsupported, ``--ws`` is not positive, ``--repo`` disagrees with the ambient
    checkout, the command is not run inside a GitHub checkout, a git/gh call fails,
    **Tree creation fails** (fail-closed — no native-worktree fallback), the child
    exits nonzero, or (write Runs only) the child exits 0 without writing the sentinel
    into the Tree.

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

    # The slash-namespaced E/WSnn branch — the WS PR head. A reviewer reviews THIS
    # head; a write Run cuts a fresh branch of this name via the FREEFORM shape.
    branch = f"{epic}/WS{ws:02d}"

    # Reviewer Run (ADR-0018): a shared READ-ONLY Tree on the existing PR head, not a
    # per-Run write Tree. Dispatched before building the write TreeSpec so the two
    # modes never share provisioning.
    if role == REVIEWER_ROLE:
        return _launch_reviewer(
            org=org,
            repo_name=repo_name,
            branch=branch,
            source_repo=root,
            github_url=url,
            backend=backend,
            launcher=launcher,
        )

    # Skeleton Tree: the slash-namespaced E/WSnn branch via the FREEFORM shape, so
    # the base stays the dumb origin/main; the epic-grouped umbrella base
    # (origin/E/umbrella) is the semantic path a later WS swaps in.
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

    _emit_spawned(
        tree,
        role=role,
        backend=backend,
        sentinel=str(launch.sentinel_path(tree.path)),
    )
    return 0


def _launch_reviewer(
    *,
    org: str,
    repo_name: str,
    branch: str,
    source_repo: str,
    github_url: str,
    backend: str,
    launcher: launch.Runner | None,
) -> int:
    """Provision the shared read-only Tree, launch the Reviewer Run, observe. Returns a code.

    The ADR-0018 reviewer path: resolve the shared per-``(repo, branch)`` read-only
    Tree (a second reviewer on the same head REUSES the clone), then launch a headless
    ``claude`` child rooted in it with the read-only ``--tools`` allow-list
    (:data:`~shipit.spawn.launch.REVIEWER_TOOLS`) and the reviewer task — which reads
    the diff and posts a review THROUGH the PR. Unlike the write path there is no
    sentinel: the Run reports out-of-band (the PR), so success is the child's clean
    exit. Fail-closed mirrors the write path — a read-only-Tree error exits 1 loud,
    never a fallback.
    """
    plan = readonly_plan(org=org, repo=repo_name, branch=branch)
    try:
        tree = create_readonly(plan, source_repo=source_repo, github_url=github_url)
    except (gh.GhError, ValueError, proc.ProcError, OSError) as exc:
        # Fail-closed (ADR-0017/0018): a read-only-Tree error fails the spawn LOUD;
        # the launcher below is unreachable unless a real Tree exists.
        print(f"spawn subagent: read-only tree creation failed: {exc}", file=sys.stderr)
        return 1

    cmd = launch.build_command(
        launch.reviewer_task(branch), REVIEWER_ROLE, tools=launch.REVIEWER_TOOLS
    )
    try:
        result = launch.launch(
            cmd, cwd=tree.path, env=launch.child_env(), runner=launcher
        )
    except OSError as exc:
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

    _emit_spawned(tree, role=REVIEWER_ROLE, backend=backend)
    return 0


def _emit_spawned(
    tree: Tree, *, role: str, backend: str, sentinel: str | None = None
) -> None:
    """Print the SPAWNED summary: a ``SPAWNED`` line plus the Run's coordinates as JSON.

    ``sentinel`` is the write-Run proof-of-life path; a reviewer Run reports through
    the PR and has none, so the key is omitted when ``sentinel`` is ``None``.
    """
    payload = {
        "tree": tree.path,
        "branch": tree.branch,
        "base": tree.base,
        "role": role,
        "backend": backend,
    }
    if sentinel is not None:
        payload["sentinel"] = sentinel
    print("SPAWNED")
    print(json.dumps(payload, indent=2))
