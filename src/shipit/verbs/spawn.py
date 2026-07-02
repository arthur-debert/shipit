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

from .. import execrun, gh, identity, logcontext
from ..spawn import backends, launch
from ..tree.create import Tree, create, new_agent_hash
from ..tree.layout import (
    TreeSpec,
    epic_umbrella_base,
    issue_branch,
    work_stream_branch,
)
from ..tree.readonly import create_readonly, readonly_plan

#: The backends ``spawn subagent`` can launch today — **adapter-driven** (ADR-0020
#: §Decision 2): derived from the :mod:`shipit.spawn.backends` registry, not a
#: hand-maintained constant, so wiring a backend is one registry entry. ``claude``
#: (ADR-0019), ``codex``, and ``antigravity`` (the ``agy`` CLI) are all registered —
#: write Runs (WS02/WS03) and reviewer Runs (WS04a). A ``click.Choice`` over this gates
#: the CLI, and :func:`run_subagent` re-checks it so the programmatic entry is guarded too.
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
    required=False,
    default=None,
    help=(
        "Epic code the Run belongs to, e.g. TRE03 (rides the Tree branch E/WSnn). The "
        "EPIC shape: give it WITH --ws. Omit BOTH for a standalone --issue Tree "
        "(branch issues/<id>/<session>, base origin/main)."
    ),
)
@click.option(
    "--ws",
    type=int,
    required=False,
    default=None,
    help=(
        "Work stream number N (the WSnn half of the Tree branch E/WSnn). Give it WITH "
        "--epic; omit both for a standalone --issue Tree."
    ),
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
        "spawn (no `--issue`) from being rejected before `run_subagent` runs. Without "
        "--epic/--ws it ALSO selects the standalone-issue shape (branch "
        "issues/<id>/<session>)."
    ),
)
@click.option(
    "--session",
    default="work",
    show_default=True,
    help=(
        "Session name for a standalone-issue Tree's branch issues/<id>/<session>. The "
        "suffix keeps issues/<id>/ a ref DIRECTORY so a +1 session on the same issue "
        "(e.g. --session onboard) coexists with the default `work` (naming.lex §3). "
        "Ignored by the --epic/--ws (work-stream) shape."
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
    repo: str,
    epic: str | None,
    ws: int | None,
    issue: int | None,
    role: str,
    session: str,
    backend: str,
) -> None:
    """Create a write Tree and launch a backend-agent Run that reports via a draft PR.

    Resolve the ambient repo identity, create a write Tree by reusing
    ``shipit tree create``, then launch a headless ``claude`` child whose ``cwd`` IS
    that Tree (ADR-0019). Two write shapes, dispatched on whether ``--epic``/``--ws``
    are given:

    \b
    - **epic/work stream** (``--epic E --ws N``): the Tree is cut from the epic-grouped
      umbrella base (``origin/E/umbrella``) so the Run's draft PR targets the EPIC
      branch (``E/umbrella``), matching the coordinator-driven epic topology (#176).
    - **standalone issue** (``--issue N`` with NO ``--epic``/``--ws``): the Tree is cut
      from ``origin/main`` on branch ``issues/<id>/<session>`` (session default
      ``work``), so the Run's draft PR targets ``main``.

    Either way the Run implements ``--issue`` and opens a draft PR from the Tree's
    branch; ``spawn`` resolves that PR back from the branch and reports the Run↔PR
    linkage so the coordinator drives it with ``shipit pr status``.

    Fail-closed: if the epic umbrella branch is absent on the remote, or a
    Tree-creation error occurs, the spawn exits 1 loudly — never a silent fallback
    to ``origin/main`` or to a native ``git worktree``. A child that exits nonzero,
    that exits 0 without having opened a PR on the Tree's branch, or that opened a PR
    which is not an OPEN, DRAFT PR targeting the intended base, is also a clean exit-1.
    """
    raise SystemExit(
        run_subagent(
            repo=repo,
            epic=epic,
            ws=ws,
            issue=issue,
            role=role,
            session=session,
            backend=backend,
        )
    )


def run_subagent(
    *,
    repo: str,
    role: str,
    epic: str | None = None,
    ws: int | None = None,
    issue: int | None = None,
    session: str = "work",
    backend: str = "claude",
    launcher: launch.Runner | None = None,
) -> int:
    """Resolve identity → create the Tree → launch the Run → link its PR. Returns a code.

    Two axes decide the Tree. **Role** picks write vs read-only:

    - every role but ``reviewer`` gets a per-Run **write** Tree: the child implements
      ``--issue`` and opens a draft PR on the Tree's branch, and the proof is that
      Run↔PR linkage — the SPAWNED summary reports it for ``shipit pr status``;
    - ``reviewer`` (ADR-0018) gets a shared **read-only** Tree on the existing PR head
      and posts its review THROUGH that PR, so it needs no ``--issue`` and leaves no
      Run↔PR linkage to resolve — its proof is the child's clean exit.

    **Shape** picks the branch/base, dispatched on whether ``--epic``/``--ws`` are given:

    - **epic/work stream** (``--epic E --ws N``): branch ``E/WSnn`` cut from the
      epic-grouped umbrella base (``origin/E/umbrella``, #176), so the draft PR targets
      the epic branch ``E/umbrella``;
    - **standalone issue** (``--issue N`` with NO ``--epic``/``--ws``): branch
      ``issues/<id>/<session>`` (session default ``work``) cut from ``origin/main``, so
      the draft PR targets ``main``. A reviewer follows the same two shapes to resolve
      the head it reviews.

    Returns 1 with a clean stderr message (never a traceback) when the backend is
    unsupported, ``--epic``/``--ws`` are given only half (incomplete epic shape), ``--ws``
    is not positive, ``--issue`` is missing/not positive for a WRITE Run, neither shape is
    given for a reviewer, ``--session`` is empty for a standalone-issue Run (it is ignored
    by the ``--epic``/``--ws`` shape), ``--repo`` disagrees with the ambient
    checkout, the command is not run inside a GitHub checkout, a git/gh call fails,
    **Tree creation fails** (fail-closed — no native-worktree fallback), or the child
    exits nonzero. For a write Run it also fails when the child exits 0 without opening a
    PR on the branch, that PR's state cannot be read, or the PR is not an OPEN, DRAFT PR
    targeting the Tree's intended base (an invalid lifecycle state the coordinator must
    not be handed).

    ``launcher`` injects the subprocess seam so the launch contract is unit-tested
    without spawning a real ``claude``; ``None`` uses the real
    :func:`shipit.spawn.launch._exec_runner` (a consumer view over
    :func:`shipit.execrun.run`).
    """
    if backend not in SUPPORTED_BACKENDS:
        supported = ", ".join(SUPPORTED_BACKENDS)
        print(
            f"spawn subagent: unsupported backend {backend!r} (supported: "
            f"{supported}); wiring a new backend is one entry in the adapter registry "
            "(ADR-0020).",
            file=sys.stderr,
        )
        return 1
    # The explicit guard above fails an unknown backend LOUD at the verb boundary (no
    # silent default to claude); only then do we resolve its adapter (ADR-0020). The
    # adapter supplies the per-backend argv / auth-env / read-only posture; everything
    # else below (Tree, prompts, launch, PR resolution) is backend-agnostic.
    adapter = backends.resolve(backend)

    # Shape gate (before any I/O). --epic and --ws are a PAIR (the epic/work-stream
    # shape); one without the other is an incomplete shape and refused loud. Their
    # ABSENCE selects the standalone-issue shape (branch issues/<id>/<session>).
    has_epic = epic is not None or ws is not None
    if has_epic and (epic is None or ws is None):
        print(
            "spawn subagent: the epic shape needs both --epic and --ws "
            f"(got epic={epic!r}, ws={ws!r}); omit both for a standalone --issue Tree.",
            file=sys.stderr,
        )
        return 1
    if has_epic and ws < 1:
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
        # reviews an existing PR head), so the requirement does not apply to it. This
        # holds for BOTH write shapes — the epic Run's PR links ``for #<issue>`` and the
        # standalone Run's issue also names its branch.
        print(
            f"spawn subagent: --issue must be a positive integer (got {issue})",
            file=sys.stderr,
        )
        return 1
    if not has_epic and issue is None:
        # Reachable only for a reviewer (a write role already required --issue above):
        # with neither an epic shape nor an issue there is no branch to resolve a head
        # from. Refuse it loud with a clear, reviewer-specific message HERE — otherwise the
        # reviewer dispatch below would take the issue path and call
        # `issue_branch(None, session)`, which raises a generic ValueError ("issue number
        # must be a positive integer", via its isinstance/`< 1` guard). A clean exit-1
        # either way, but this message names the ACTUAL problem (no shape given), not a
        # confusing complaint about the issue number.
        print(
            "spawn subagent: a reviewer needs a branch to review — give --epic E --ws N "
            "or --issue N.",
            file=sys.stderr,
        )
        return 1

    root = gh.repo_root()
    if not root:
        print("spawn subagent: not inside a git checkout", file=sys.stderr)
        return 1
    try:
        # Identity derives LOCALLY from the origin remote (ADR-0024): one canonical,
        # case-normalized Repo value object — a malformed remote fails loud
        # (ValueError) rather than feeding a bogus identity into the TreeSpec.
        repo_identity = identity.resolve_repo(root)
        url = gh.git_remote_url(cwd=root)
    except (execrun.ExecError, ValueError) as exc:
        print(f"spawn subagent: {exc}", file=sys.stderr)
        return 1

    # --repo is the wrong-checkout guard, not a repo SELECTOR yet: the skeleton
    # resolves identity from the ambient checkout, so a --repo that names a
    # different repo is refused rather than silently ignored. Compared through the
    # canonical identity (lowercased — GitHub slugs are case-insensitive), so a
    # mixed-case --repo never false-negatives against a case-varying origin.
    # Multi-repo selection is a later WS.
    if repo.strip().lower() not in (repo_identity.name, repo_identity.slug):
        print(
            f"spawn subagent: --repo {repo!r} but the ambient checkout is "
            f"{repo_identity.slug!r}; the skeleton spawns from the target checkout "
            "(multi-repo selection is a later WS).",
            file=sys.stderr,
        )
        return 1

    # Reviewer Run (ADR-0018): a shared READ-ONLY Tree on the existing PR head, not a
    # per-Run write Tree. Its target branch follows the SHAPE — the epic work-stream head
    # E/WSnn, or a standalone-issue head issues/<id>/<session> — built from the same
    # grammar helpers the write planner uses so a reviewer pins exactly the branch a
    # write Run pushed. Dispatched before the write path so the two never share provisioning.
    if role == REVIEWER_ROLE:
        try:
            review_branch = (
                work_stream_branch(epic, ws)
                if has_epic
                else issue_branch(issue, session)
            )
        except ValueError as exc:
            # Fail loud, identically to the write path: work_stream_branch validates the
            # epic code (an empty/invalid epic must NOT silently yield "/WS01") and
            # issue_branch validates the session — both raise ValueError, surfaced here as
            # the verb's clean exit-1, never a traceback.
            print(f"spawn subagent: {exc}", file=sys.stderr)
            return 1
        return _launch_reviewer(
            repo=repo_identity,
            branch=review_branch,
            source_repo=root,
            github_url=url,
            adapter=adapter,
            launcher=launcher,
        )

    # WRITE Run: build the shape's TreeSpec, then hand off to the shared launch tail.
    if has_epic:
        # Epic-base resolution (#176): a work stream rides the epic topology, so its Tree
        # is cut from the epic-grouped umbrella base (origin/E/umbrella) and its draft PR
        # targets the EPIC branch (E/umbrella), NOT main. The EPIC shape of the TreeSpec
        # resolves both — branch E/WSnn, base origin/E/umbrella — through the same pure
        # planner `shipit tree create` uses; the PR target falls out of `tree.base` in
        # `_launch_write` (origin/E/umbrella -> E/umbrella), exactly as origin/main -> main.
        try:
            umbrella_base = epic_umbrella_base(epic)  # origin/E/umbrella
        except ValueError as exc:
            # An invalid/empty epic code (not a single alphanumeric token) would build a
            # malformed or path-traversing umbrella ref, so the pure helper refuses it.
            # Catch that here and emit the same clean exit-1-with-diagnostic the rest of
            # the verb uses for fail-closed paths — never an escaping traceback.
            print(f"spawn subagent: {exc}", file=sys.stderr)
            return 1
        umbrella_branch = umbrella_base.split("/", 1)[-1]  # E/umbrella
        # Fail-closed (ADR-0017/0019): the epic umbrella branch MUST exist on the remote
        # before we cut a work stream from it. If it does not, refuse LOUD — never
        # silently fall back to origin/main, which would land the WS PR on the wrong base
        # and break the coordinator-driven epic topology. Checked against the remote here
        # (pre-clone) so the diagnostic names the missing epic branch precisely, rather
        # than surfacing as an opaque `git checkout` failure deep in tree creation.
        try:
            umbrella_exists = gh.remote_branch_exists(umbrella_branch, cwd=root)
        except execrun.ExecError as exc:
            print(f"spawn subagent: {exc}", file=sys.stderr)
            return 1
        if not umbrella_exists:
            print(
                f"spawn subagent: epic base branch {umbrella_branch!r} does not exist "
                f"on origin; cannot cut work stream {epic}/WS{ws:02d} from it. Create "
                "the epic umbrella branch first — refusing to fall back to origin/main, "
                "which would target the WS PR at the wrong base (#176, fail-closed).",
                file=sys.stderr,
            )
            return 1
        spec = TreeSpec(
            repo=repo_identity,
            agent_hash=new_agent_hash(),
            epic=epic,
            ws=ws,
        )
    else:
        # Standalone-issue shape (no epic): branch issues/<id>/<session>, base
        # origin/main. Validate the branch grammar (positive issue, non-empty session)
        # BEFORE any side effect, mirroring the epic umbrella pre-check — a bad --session
        # must fail loud here, not deep in tree creation. `origin/main` always exists, so
        # there is no umbrella-style remote pre-check to run.
        try:
            issue_branch(issue, session)  # validation only; the spec re-plans it
        except ValueError as exc:
            print(f"spawn subagent: {exc}", file=sys.stderr)
            return 1
        spec = TreeSpec(
            repo=repo_identity,
            agent_hash=new_agent_hash(),
            issue=issue,
            session=session,
        )

    return _launch_write(
        spec,
        source_repo=root,
        github_url=url,
        role=role,
        issue=issue,
        backend=backend,
        adapter=adapter,
        launcher=launcher,
    )


def _launch_write(
    spec: TreeSpec,
    *,
    source_repo: str,
    github_url: str,
    role: str,
    issue: int | None,
    backend: str,
    adapter: backends.BackendAdapter,
    launcher: launch.Runner | None,
) -> int:
    """Create the write Tree from ``spec``, launch the Run, resolve its PR. Returns a code.

    The shared write tail for BOTH shapes (epic/work stream and standalone issue): the
    caller builds the shape's :class:`TreeSpec` and does any shape-specific pre-checks
    (the epic umbrella existence), then this seam materializes the Tree, launches the
    backend child rooted in it, and resolves the Run↔PR linkage the coordinator drives —
    identically whichever shape produced the spec, since ``tree.base``/``tree.branch``
    already encode it.

    Fail-closed (ADR-0017/0019): a Tree-creation error fails LOUD with no native-worktree
    fallback (the launcher is unreachable unless a real Tree exists). After a clean child
    exit it also fails when no PR was opened on the branch, the PR state is unreadable, or
    the PR is not an OPEN, DRAFT PR targeting ``tree.base`` — each a clean exit-1, never a
    SPAWNED line.
    """
    try:
        tree = create(spec, source_repo=source_repo, github_url=github_url)
    except (ValueError, execrun.ExecError, OSError) as exc:
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
    # targets a branch name (origin/E/umbrella -> E/umbrella, origin/main -> main). The
    # Tree path is passed as `cwd`: most backends root via the process cwd and ignore it,
    # but `agy` ignores its process cwd and is rooted ONLY by `--add-dir <Tree>`.
    # SPAWN SEAM for the domain-key context (ADR-0029): the Tree's identity binds
    # here — the coordinator's records from this point carry `tree` (its path, the
    # same identity the SPAWNED payload reports) — and `env_export` below threads
    # every bound key (tree, plus the repo bound at the CLI entry) into the Run's
    # environment, so each `shipit` command the Run executes inside the Tree
    # rebinds them at its own logging setup and its records correlate back here.
    logcontext.bind(tree=tree.path)
    base_branch = tree.base.split("/", 1)[-1] if "/" in tree.base else tree.base
    task = launch.write_task(
        role, issue=issue, branch=tree.branch, base_branch=base_branch
    )
    # Route the write Run THROUGH the Tree's pixi env (ADR-0019 amendment): a provisioned
    # write Tree carries `.pixi/envs/default`, so `pixi_wrap` re-expresses the argv as
    # `pixi run --manifest-path <tree>/pixi.toml -- <argv>` and the child's tools resolve
    # to its OWN env (else they'd resolve the coordinator's env — docs/dev/pixi.lex §7).
    # `scrub_tree_env` drops leaked PIXI_*/CONDA_* on top of the adapter's auth scrub.
    cmd = launch.pixi_wrap(adapter.build_command(task, role, cwd=tree.path), tree.path)
    try:
        result = launch.launch(
            cmd,
            cwd=tree.path,
            env=launch.scrub_tree_env(logcontext.env_export(adapter.child_env())),
            runner=launcher,
        )
    except execrun.ExecError as exc:
        # The child never started: `claude` is missing/not on PATH, or the Tree path
        # became unavailable. The Exec runner normalizes every launch-level OS failure
        # into ExecError (ADR-0028) — a nonzero CHILD is a LaunchResult, never raised
        # (check=False), so reaching here always means a transport failure. The Tree
        # exists, so this is a launch failure, not the fail-closed create path — still
        # a clean exit-1, never an escaping traceback.
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

    # A PR existing on the branch is necessary but not sufficient: the contract is that
    # the Run reported back through an OPEN, DRAFT PR targeting the Tree's intended base.
    # A ready-for-review PR, a closed/merged one, or one opened against the wrong base is
    # an INVALID lifecycle state the coordinator must not be handed as success — each is a
    # clean exit-1, never a SPAWNED line.
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
    repo: identity.Repo,
    branch: str,
    source_repo: str,
    github_url: str,
    adapter: backends.BackendAdapter,
    launcher: launch.Runner | None,
) -> int:
    """Provision the shared read-only Tree, launch the Reviewer Run, observe. Returns a code.

    The ADR-0018 reviewer path: resolve the shared per-``(repo, branch)`` read-only
    Tree (a second reviewer on the same head REUSES the clone), then launch the backend
    child rooted in it with the adapter's **read-only posture** (``read_only=True`` on
    :meth:`~shipit.spawn.backends.base.BackendAdapter.build_command` — for ``claude`` the
    read-only ``--tools`` allow-list; for ``codex`` / ``agy``, which have no allow-list,
    the least-privilege posture that still lets the agent self-post via ``gh pr review``,
    with read-only enforced by the chmod'd Tree, ADR-0020 §Decision 3) and the reviewer
    task — which reads the diff and posts a review THROUGH the PR. Backend-agnostic: the
    same call drives claude, codex, and antigravity. Unlike the write path there is no
    Run↔PR linkage to resolve: the Run reports out-of-band (the review lands in the
    existing PR), so success is the child's clean exit. Fail-closed mirrors the write
    path — a read-only-Tree error exits 1 loud, never a fallback.
    """
    plan = readonly_plan(repo=repo, branch=branch)
    try:
        tree = create_readonly(plan, source_repo=source_repo, github_url=github_url)
    except (ValueError, execrun.ExecError, OSError) as exc:
        # Fail-closed (ADR-0017/0018): a read-only-Tree error fails the spawn LOUD;
        # the launcher below is unreachable unless a real Tree exists.
        print(f"spawn subagent: read-only tree creation failed: {exc}", file=sys.stderr)
        return 1

    # SPAWN SEAM (ADR-0029), mirroring the write path: the shared read-only Tree's
    # identity binds here, and `env_export` at the launch threads the bound keys
    # into the Reviewer Run's environment so its `shipit`/`gh` activity correlates.
    logcontext.bind(tree=tree.path)

    # The reviewer posture (ADR-0020 §Decision 3): `read_only=True` builds the backend's
    # reviewer argv (claude → read-only --tools; codex → workspace-write+network, NOT the
    # write bypass; agy → drop --dangerously-skip-permissions). `cwd=tree.path` is
    # required by the agy adapter (it ignores the process cwd and roots ONLY via
    # `--add-dir <Tree>`) and ignored by the rest. The chmod'd read-only Tree is the
    # load-bearing FS guard whatever the backend's native posture.
    # `pixi_wrap` is a no-op here by design: a reviewer's read-only Tree is clone+checkout
    # with NO provisioned `.pixi/envs/default`, so it stays BARE (routing a chmod'd tree
    # through `pixi run` would force a solve / fail). The gate, not the call site, decides.
    cmd = launch.pixi_wrap(
        adapter.build_command(
            launch.reviewer_task(branch),
            REVIEWER_ROLE,
            read_only=True,
            cwd=tree.path,
        ),
        tree.path,
    )
    try:
        result = launch.launch(
            cmd,
            cwd=tree.path,
            env=launch.scrub_tree_env(logcontext.env_export(adapter.child_env())),
            runner=launcher,
        )
    except execrun.ExecError as exc:
        # Transport failure only (ADR-0028): the runner raised because the child never
        # started; a nonzero reviewer child is a LaunchResult handled below.
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
