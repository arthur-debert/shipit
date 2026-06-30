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
from ..spawn import backends, launch
from ..tree.create import Tree, create, new_agent_hash
from ..tree.layout import TreeSpec
from ..tree.readonly import create_readonly, readonly_plan

#: The backends ``spawn subagent`` can launch today — **adapter-driven** (ADR-0020
#: §Decision 2): derived from the :mod:`shipit.spawn.backends` registry, not a
#: hand-maintained constant, so wiring a backend is one registry entry. Only ``claude``
#: is registered (ADR-0019 is the claude-backend contract); codex / antigravity are a
#: future WS (#153). A ``click.Choice`` over this gates the CLI, and
#: :func:`run_subagent` re-checks it so the programmatic entry point is guarded too.
SUPPORTED_BACKENDS = backends.supported_backends()

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
    "--issue",
    type=int,
    required=False,
    default=None,
    help=(
        "The issue the Run implements. It rides the task prompt (the Run reads it "
        "with `gh issue view`) and the draft PR links it as `for #<issue>`, so the "
        "spawned-Run PR flows through the normal engine like any hand-spawned one. "
        "REQUIRED for a write role (validated in `run_subagent`); a `reviewer` Run "
        "implements no issue, so it is OPTIONAL at the CLI to keep a valid reviewer "
        "spawn (no `--issue`) from being rejected before `run_subagent` runs."
    ),
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
    help=(
        "The agent backend to launch (derived from the adapter registry). `claude` "
        "(ADR-0019) and `antigravity` — the `agy` CLI, write Runs (ADR-0020) — are "
        "wired; `codex` lands alongside."
    ),
)
def subagent_cmd(
    repo: str, epic: str, ws: int, issue: int | None, role: str, backend: str
) -> None:
    """Create a write Tree and launch a backend-agent Run that reports via a draft PR.

    Resolve the ambient repo identity, create a write Tree by reusing
    ``shipit tree create`` (kept dumb — base ``origin/main``; the epic-grouped
    umbrella base is a later WS, deferred follow-up #176), then launch a headless
    ``claude`` child whose
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
    role: str,
    issue: int | None = None,
    backend: str = "claude",
    launcher: launch.Runner | None = None,
) -> int:
    """Resolve identity → create the Tree → launch the Run → link its PR. Returns a code.

    Tree. There are two modes, dispatched on ``role``:

    - every role but ``reviewer`` gets a per-Run **write** Tree (WS02): the child
      implements ``--issue`` and opens a draft PR on the Tree's branch, and the proof
      is that Run↔PR linkage — the SPAWNED summary reports it for ``shipit pr status``;
    - ``reviewer`` (ADR-0018) gets a shared **read-only** Tree on the existing PR head
      and posts its review THROUGH that PR, so it needs no ``--issue`` and leaves no
      Run↔PR linkage to resolve — its proof is the child's clean exit (the review is
      out-of-band, in the PR).

    Returns 1 with a clean stderr message (never a traceback) when the backend is
    unsupported, ``--ws`` is not positive, ``--issue`` is missing/not positive for a
    WRITE Run, ``--repo`` disagrees with the ambient checkout, the command is not run
    inside a GitHub checkout, a git/gh call fails, **Tree creation fails** (fail-closed
    — no native-worktree fallback), or the child exits nonzero. For a write Run it also
    fails when the child exits 0 without opening a PR on the branch, that PR's state
    cannot be read, or the PR is not an OPEN, DRAFT PR targeting the Tree's intended
    base (an invalid lifecycle state the coordinator must not be handed).

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
    # The explicit guard above fails an unknown backend LOUD at the verb boundary (no
    # silent default to claude); only then do we resolve its adapter (ADR-0020). The
    # adapter supplies the per-backend argv / auth-env / read-only posture; everything
    # else below (Tree, prompts, launch, PR resolution) is backend-agnostic.
    adapter = backends.resolve(backend)
    if ws < 1:
        print(
            f"spawn subagent: --ws must be a positive integer (got {ws})",
            file=sys.stderr,
        )
        return 1
    if role != REVIEWER_ROLE and (issue is None or issue < 1):
        # ``--issue`` rides the task prompt and the draft PR's ``for #<issue>`` link;
        # a missing or zero/negative value (which click's int type still accepts) would
        # forge a nonsensical issue reference. Refuse it before any Tree/child work,
        # mirroring the ``--ws`` guard above. A reviewer Run implements no issue (it
        # reviews an existing PR head), so the requirement does not apply to it.
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
            adapter=adapter,
            launcher=launcher,
        )

    # Skeleton Tree: the slash-namespaced E/WSnn branch via the FREEFORM shape, so
    # the base stays the dumb origin/main; the epic-grouped umbrella base
    # (origin/E/umbrella) — and the matching epic-branch PR target — is the semantic
    # path a later WS swaps in. DEFERRED (maintainer decision, "keep it dumb"):
    # follow-up #176. Docs that describe the verb are reconciled to this shipped
    # origin/main behavior, not the deferred epic-base resolution.
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

    # Launch the backend child rooted in the Tree through its adapter (ADR-0020): the
    # cwd IS the Tree, the adapter's child_env scrubs the backend's auth-shadowing vars
    # (for claude, ANTHROPIC_API_KEY), and build_command conveys the role (for claude,
    # --agent <role>, so the guard allows the Run's own edits). The task tells the Run to
    # implement the issue and open a draft PR from this branch (the result channel —
    # ADR-0019 §6); base_branch drops the remote prefix off the Tree's base so the PR
    # targets a branch name (origin/main -> main). The Tree path is passed as `cwd`: most
    # backends root via the process cwd and ignore it, but `agy` ignores its process cwd
    # and is rooted ONLY by `--add-dir <Tree>`, so the adapter needs the path (ADR-0020).
    base_branch = tree.base.split("/", 1)[-1] if "/" in tree.base else tree.base
    task = launch.write_task(
        role, issue=issue, branch=tree.branch, base_branch=base_branch
    )
    cmd = adapter.build_command(task, role, cwd=tree.path)
    try:
        result = launch.launch(
            cmd, cwd=tree.path, env=adapter.child_env(), runner=launcher
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
            f"spawn subagent: {adapter.name} child exited {result.returncode}"
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


def _launch_reviewer(
    *,
    org: str,
    repo_name: str,
    branch: str,
    source_repo: str,
    github_url: str,
    adapter: backends.BackendAdapter,
    launcher: launch.Runner | None,
) -> int:
    """Provision the shared read-only Tree, launch the Reviewer Run, observe. Returns a code.

    The ADR-0018 reviewer path: resolve the shared per-``(repo, branch)`` read-only
    Tree (a second reviewer on the same head REUSES the clone), then launch the backend
    child rooted in it with the adapter's **read-only posture**
    (:attr:`~shipit.spawn.backends.base.BackendAdapter.reviewer_tools` — for ``claude``
    the read-only allow-list; a backend with no allow-list returns ``None`` and read-only
    rides solely on the chmod'd Tree, ADR-0020 §Decision 3) and the reviewer task — which
    reads the diff and posts a review THROUGH the PR. Unlike the write path there is no
    Run↔PR linkage to resolve: the Run reports out-of-band (the review lands in the
    existing PR), so success is the child's clean exit. Fail-closed mirrors the write
    path — a read-only-Tree error exits 1 loud, never a fallback.
    """
    plan = readonly_plan(org=org, repo=repo_name, branch=branch)
    try:
        tree = create_readonly(plan, source_repo=source_repo, github_url=github_url)
    except (gh.GhError, ValueError, proc.ProcError, OSError) as exc:
        # Fail-closed (ADR-0017/0018): a read-only-Tree error fails the spawn LOUD;
        # the launcher below is unreachable unless a real Tree exists.
        print(f"spawn subagent: read-only tree creation failed: {exc}", file=sys.stderr)
        return 1

    # `cwd=tree.path` is threaded here exactly as on the write path: most backends ignore
    # it (they root via the process cwd), but `agy` is rooted ONLY by `--add-dir <Tree>`,
    # so `build_command` must never be called without it — otherwise `--backend antigravity
    # --role reviewer` would raise ValueError and leak a traceback. The read-only Tree path
    # is the right root here. Full non-Claude reviewer semantics (read-only posture) are
    # WS04; this only unifies the call signature so the traceback can't occur.
    cmd = adapter.build_command(
        launch.reviewer_task(branch),
        REVIEWER_ROLE,
        tools=adapter.reviewer_tools,
        cwd=tree.path,
    )
    try:
        result = launch.launch(
            cmd, cwd=tree.path, env=adapter.child_env(), runner=launcher
        )
    except OSError as exc:
        print(f"spawn subagent: {exc}", file=sys.stderr)
        return 1
    if result.returncode != 0:
        detail = result.stderr.strip()
        print(
            f"spawn subagent: {adapter.name} child exited {result.returncode}"
            + (f"\n{detail}" if detail else ""),
            file=sys.stderr,
        )
        return 1

    _emit_spawned(tree, role=REVIEWER_ROLE, backend=adapter.name)
    return 0


def _emit_spawned(
    tree: Tree, *, role: str, backend: str, pr: dict | None = None
) -> None:
    """Print the SPAWNED summary: a ``SPAWNED`` line plus the Run's coordinates as JSON.

    A WRITE Run passes ``pr``, the Run↔PR linkage the coordinator acts on: ``number``
    feeds ``shipit pr status <N>`` to drive the spawned-Run PR through the normal engine
    (reviews, ready, merge) exactly like a hand-spawned implementer's; ``state`` /
    ``is_draft`` echo how the Run left it (a draft, per the role's PR protocol). A
    reviewer Run reports through the existing PR and opens none, so it passes no ``pr``
    and the block is omitted.
    """
    payload = {
        "tree": tree.path,
        "branch": tree.branch,
        "base": tree.base,
        "role": role,
        "backend": backend,
    }
    if pr is not None:
        payload["pr"] = pr["number"]
        payload["pr_state"] = pr["state"]
        payload["pr_is_draft"] = pr.get("isDraft")
    print("SPAWNED")
    print(json.dumps(payload, indent=2))
