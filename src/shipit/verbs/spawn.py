"""``shipit spawn`` — shipit-owned subagent spawning (ADR-0017 / ADR-0019).

A NESTED click group mirroring ``shipit tree``: ``shipit spawn <verb>`` is the
surface for launching backend-agent **Runs** that shipit owns end to end. The
first verb, ``subagent``, creates a write **Tree** by REUSING the tree-creation
path, then launches a headless ``claude`` child rooted in that Tree per the
ADR-0019 launch contract. The Run does real work that **culminates in a draft PR**
(TRE03-WS02): it implements its issue and opens a draft PR from the Tree's branch,
and ``spawn`` surfaces the Run↔PR linkage so the coordinator drives that PR with
the existing ``shipit pr status <N>`` engine — exactly as a hand-spawned
implementer does.

The verb is thin: resolve the ambient repo identity at the gh/git boundary, hand a
typed :class:`TreeSpec` to the existing pure planner + effectful orchestrator
(:func:`shipit.tree.create.create`) — Tree creation is never reimplemented — launch
the child through the unit-testable :mod:`shipit.spawn.launch` seam, then resolve the
PR the Run opened on the Tree's branch back through the same :mod:`shipit.gh`
boundary the fleet scan uses (no side database — the PR on the branch IS the link).

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
    "--issue",
    type=int,
    required=True,
    help=(
        "The issue the Run implements. It rides the task prompt (the Run reads it "
        "with `gh issue view`) and the draft PR links it as `for #<issue>`, so the "
        "spawned-Run PR flows through the normal engine like any hand-spawned one."
    ),
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
def subagent_cmd(
    repo: str, epic: str, ws: int, issue: int, role: str, backend: str
) -> None:
    """Create a write Tree and launch a backend-agent Run that reports via a draft PR.

    Resolve the ambient repo identity, create a write Tree by reusing
    ``shipit tree create`` (kept dumb — base ``origin/main``; the epic-grouped
    umbrella base is a later WS), then launch a headless ``claude`` child whose
    ``cwd`` IS that Tree (ADR-0019). The Run implements ``--issue`` and opens a draft
    PR from the Tree's branch; ``spawn`` resolves that PR back from the branch and
    reports the Run↔PR linkage so the coordinator drives it with ``shipit pr status``.

    Fail-closed: a Tree-creation error exits 1 loudly — never a silent fallback to a
    native ``git worktree``. A child that exits nonzero, that exits 0 without having
    opened a PR on the Tree's branch, or that opened a PR which is not an OPEN, DRAFT PR
    targeting the intended base, is also a clean exit-1.
    """
    raise SystemExit(
        run_subagent(
            repo=repo, epic=epic, ws=ws, issue=issue, role=role, backend=backend
        )
    )


def run_subagent(
    *,
    repo: str,
    epic: str,
    ws: int,
    issue: int,
    role: str,
    backend: str = "claude",
    launcher: launch.Runner | None = None,
) -> int:
    """Resolve identity → create the Tree → launch the Run → link its PR. Returns a code.

    Returns 0 once a headless ``claude`` child has run rooted in a freshly-created
    write Tree and opened a PR on that Tree's branch — the Run↔PR linkage the
    SPAWNED summary reports for ``shipit pr status``. Returns 1 with a clean stderr
    message (never a traceback) when the backend is unsupported, ``--ws`` or
    ``--issue`` is not positive, ``--repo`` disagrees with the ambient checkout, the
    command is not run inside a GitHub checkout, a git/gh call fails, **Tree creation
    fails** (fail-closed — no native-worktree fallback), the child exits nonzero, the
    child exits 0 without opening a PR on the branch, that PR's state cannot be read,
    or the PR is not an OPEN, DRAFT PR targeting the Tree's intended base (an invalid
    lifecycle state the coordinator must not be handed).

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
    if issue < 1:
        # ``--issue`` rides the task prompt and the draft PR's ``for #<issue>`` link;
        # a zero/negative value (which click's int type still accepts) would forge a
        # nonsensical issue reference. Refuse it before any Tree/child work, mirroring
        # the ``--ws`` guard above.
        print(
            f"spawn subagent: --issue must be a positive integer (got {issue})",
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
    # role to the harness so the guard allows the Run's own edits. The task tells the
    # Run to implement the issue and open a draft PR from this branch (the result
    # channel — ADR-0019 §6); base_branch drops the remote prefix off the Tree's base
    # so the PR targets a branch name (origin/main -> main).
    base_branch = tree.base.split("/", 1)[-1] if "/" in tree.base else tree.base
    task = launch.write_task(
        role, issue=issue, branch=tree.branch, base_branch=base_branch
    )
    cmd = launch.build_command(task, role)
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

    # The Run reports back through the PR (ADR-0019 §6): resolve the PR it opened on
    # the Tree's branch through the SAME gh boundary the fleet scan uses — no side
    # database, the PR on the branch IS the Run↔PR link. A branch with provably no PR
    # means the Run did not report back; an undetermined state must not masquerade as
    # success. Both are a clean exit-1.
    pr = gh.pr_for_head(tree.branch, cwd=tree.path)
    if pr is None:
        print(
            f"spawn subagent: child exited 0 but opened no PR on {tree.branch!r}; "
            "the Run did not report back through a draft PR.",
            file=sys.stderr,
        )
        return 1
    if pr is gh.UNKNOWN:
        print(
            f"spawn subagent: child exited 0 but the PR state for {tree.branch!r} "
            "could not be read (gh unreadable); not claiming success.",
            file=sys.stderr,
        )
        return 1

    # A PR existing on the branch is necessary but not sufficient: the WS02 contract is
    # that the Run reported back through an OPEN, DRAFT PR targeting the Tree's intended
    # base. A ready-for-review PR, a closed/merged one, or one opened against the wrong
    # base is an INVALID lifecycle state the coordinator must not be handed as success —
    # each is a clean exit-1, never a SPAWNED line.
    if pr["state"] != "OPEN":
        print(
            f"spawn subagent: child exited 0 but the PR on {tree.branch!r} is "
            f"{pr['state']}, not OPEN; the Run did not report back through an open "
            "draft PR.",
            file=sys.stderr,
        )
        return 1
    if pr.get("isDraft") is not True:
        print(
            f"spawn subagent: child exited 0 but the PR on {tree.branch!r} is not a "
            "draft; the Run must report back through a draft PR (the turn-signal the "
            "coordinator drives).",
            file=sys.stderr,
        )
        return 1
    if pr.get("baseRefName") != base_branch:
        print(
            f"spawn subagent: child exited 0 but the PR on {tree.branch!r} targets "
            f"base {pr.get('baseRefName')!r}, not the intended {base_branch!r}; the "
            "Run reported back against the wrong base.",
            file=sys.stderr,
        )
        return 1

    _emit_spawned(tree, role=role, backend=backend, pr=pr)
    return 0


def _emit_spawned(tree: Tree, *, role: str, backend: str, pr: dict) -> None:
    """Print the SPAWNED summary: a ``SPAWNED`` line plus the Run's coordinates as JSON.

    The ``pr`` block is the Run↔PR linkage the coordinator acts on: ``number`` is what
    feeds ``shipit pr status <N>`` to drive the spawned-Run PR through the normal engine
    (reviews, ready, merge) exactly like a hand-spawned implementer's; ``state`` /
    ``is_draft`` echo how the Run left it (a draft, per the role's PR protocol).
    """
    print("SPAWNED")
    print(
        json.dumps(
            {
                "tree": tree.path,
                "branch": tree.branch,
                "base": tree.base,
                "role": role,
                "backend": backend,
                "pr": pr["number"],
                "pr_state": pr["state"],
                "pr_is_draft": pr.get("isDraft"),
            },
            indent=2,
        )
    )
