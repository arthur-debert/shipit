"""``spawn/launch`` — the **backend-agnostic** child-process launch machinery.

The pieces here are the same whatever the backend is (``claude``, ``codex``,
``antigravity``) — they are the shared half of the ADR-0020 seam. What *varies* per
backend (the argv, the auth-env scrub, the reviewer read-only posture) lives behind a
:class:`~shipit.spawn.backends.base.BackendAdapter` in :mod:`shipit.spawn.backends`; this
module holds everything that does NOT vary:

- :func:`launch` — runs a resolved ``cmd``/``cwd``/``env`` through an injectable
  ``runner``, rooting the child in the Tree (``cwd`` = the Tree, **stdin from
  ``/dev/null``**). The runner is the subprocess seam, swapped for a fake in tests.
  The real runner is a **consumer view over the one Exec runner**
  (:func:`shipit.execrun.run`, ADR-0028) with the launch contract's own semantics
  pinned on top: ``check=False`` (a nonzero agent child is a normal lifecycle
  outcome the verb reports — never an :class:`~shipit.execrun.ExecError`, ADR-0019
  §6), ``replace_env=True`` (the adapter's scrubbed env IS the child env), and a
  per-call ``timeout`` defaulting to :data:`LAUNCH_TIMEOUT` (``None`` — an agent
  **write** Run legitimately runs far past the runner's 5-minute default, so no
  bound may kill it). One caller overrides that default: the review producer
  (:mod:`shipit.review.producer`) passes the reviewer's configured ``--timeout``
  as a real process deadline, because a review is a bounded, non-blocking degrade
  (ADR-0006) — a stalled review backend must be killed and settled ``timed_out``,
  not waited on forever (#404). Expiry surfaces as an
  :class:`~shipit.execrun.ExecError` (``cause=CAUSE_TIMEOUT``, partial streams
  carried) that the producer maps to its ``timed_out`` outcome. In exchange, every
  launch is an Exec like any other: one structured record with argv, cwd, rc, and
  ``duration_ms``.
- :func:`write_task` / :func:`reviewer_task` — the English PR-contract prompts a Run is
  handed. These are backend-agnostic: they convey *what work to do and how to report
  it* (the PR is the result channel, ADR-0019 §6), not how any particular CLI is shaped.

Rooting is the OS process ``cwd`` (ADR-0019 §1), NOT a ``cd`` — so the child's writes
land in the Tree with no leak to the parent checkout, sidestepping the bash-cwd-reset
footgun. The module is split pure-from-effectful so the contract is table-tested without
spawning a real child: :func:`launch` takes an injectable ``runner`` (defaulting to the
real :func:`_exec_runner`).

**Routing the child through pixi (ADR-0019 amendment 2026-06-30).** ``cwd=<Tree>`` roots
the child's writes in the Tree but does NOT activate the Tree's pixi env, so the child
would inherit the *coordinator's* (or system) env and its
``python``/``pytest``/``ruff``/``shipit`` would resolve to the WRONG ``.pixi`` env — the
Tree is provisioned, then bypassed (``docs/dev/pixi.lex`` §7/§8). :func:`pixi_wrap` fixes
this by re-expressing the backend argv as ``pixi run --manifest-path <tree>/pixi.toml --
<argv>`` so the child lands in the Tree's OWN env — but ONLY when the Tree actually
carries a provisioned env (``<tree>/.pixi/envs/default`` exists). A **reviewer's
read-only Tree** (ADR-0018) and a **non-pixi repo** have none, so :func:`pixi_wrap`
leaves their argv BARE — routing those through ``pixi run`` would force a solve into a
chmod'd tree or fail outright. The pixi knowledge behind both halves — the
provisioned-env sentinel and the wrapped argv — lives in the pixi adapter
(:func:`shipit.pixienv.has_default_env` / :func:`shipit.pixienv.run_argv`,
ADR-0028); this module keeps only the launch-side ROUTING DECISION and its
narration. The WRITE tail now carries that decision on its resolved
:class:`~shipit.workenv.WorkEnv` (RPE01-WS05): the spawn boundary probes the
sentinel once, resolves the Work Env, and :func:`route_argv` CONSUMES its
``routing`` field — same gate, decided once and described. :func:`pixi_wrap`
(probe + wrap fused) remains the read-only/reviewer tail's router until the
remaining boundaries resolve Work Envs too (RPE01-WS06).
:func:`scrub_tree_env` mirrors the provisioning scrub
(:func:`shipit.tree.create.provision_env`) — both rely SOLELY on the adapter's shared
predicate (:func:`shipit.pixienv.scrub_env` over
:func:`shipit.pixienv.is_leaked_env_var`), so they cannot drift: on top of the
adapter's auth-env scrub it drops leaked ``PIXI_*`` project pointers and Conda
**activation** vars (``CONDA_PREFIX`` & friends; installation-level ``CONDA_EXE`` etc. are
KEPT) so the child — and the agent's own ``pixi`` calls — re-resolve from the Tree. ``--clean-env`` is NOT used: it was falsified
(it strips ``HOME``/``PATH``, so the child finds neither python nor the ``claude`` binary,
rc 127); the curated passthrough keeps ``HOME``/``PATH`` intact (spike, 2026-06-30).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .. import execrun, pixienv, workenv

#: The spawn subsystem's logger (shared with the verb): launch MECHANICS narrate
#: at DEBUG per the spray conventions (ADR-0029) — the pixi-routing decision and
#: the env scrub, the two silent gates a mis-rooted child is usually traced to.
#: The launch itself is one Exec whose argv/rc/duration record the Exec runner
#: already emits (ADR-0028), so nothing is duplicated here.
logger = logging.getLogger("shipit.spawn")

#: The launch path's DEFAULT per-Exec timeout: **explicitly** ``None`` (ADR-0028
#: allows it as a deliberate per-call choice, never the default). A **write** Run is
#: the legitimate arbitrarily-long child shipit launches — an implementer Run works
#: an entire issue end-to-end — so no bound the Exec runner could enforce over it is
#: safe: the runner's 5-minute :data:`shipit.execrun.DEFAULT_TIMEOUT` (or any
#: "generous" multiple of it) must never be what kills a Run mid-work. The Run's
#: lifecycle end is its process exit (ADR-0019 §6). This is only the DEFAULT, though:
#: :func:`launch` takes a per-call ``timeout``, and the review producer overrides it
#: with the reviewer's ``--timeout`` as a real deadline (#404) — a review is a
#: bounded, non-blocking degrade (ADR-0006), not an unbounded write Run, so a stalled
#: review backend MUST be killed rather than waited on forever. A backend with its
#: own native bound (``agy --print-timeout``) keeps it; the seam deadline is the
#: backstop underneath it.
LAUNCH_TIMEOUT: float | None = None


@dataclass(frozen=True)
class LaunchResult:
    """The finished Run's lifecycle handle (ADR-0019 §6).

    The parent learns start/finish from the **process exit**, never by scraping the
    deliverable (results land in the PR). The raw streams ride along so the verb can
    surface a failing child's ``stderr`` rather than swallowing it.
    """

    returncode: int
    stdout: str
    stderr: str


#: The injectable subprocess seam: given the resolved ``cmd``/``cwd``/``env`` and the
#: per-call ``timeout`` (the process deadline, ``None`` = unbounded) it runs the child
#: and returns its :class:`LaunchResult`. Tests pass a fake so the launch contract is
#: asserted WITHOUT spawning a real ``claude``; production uses :func:`_exec_runner`.
Runner = Callable[..., LaunchResult]


def launch(
    cmd: list[str],
    *,
    cwd: str | Path,
    env: Mapping[str, str],
    timeout: float | None = LAUNCH_TIMEOUT,
    runner: Runner | None = None,
) -> LaunchResult:
    """Run the resolved backend child rooted in the Tree and return its result.

    ``cmd`` / ``env`` come from the selected backend's adapter (ADR-0020); this launcher
    is backend-agnostic. Rooting is the OS process ``cwd`` (ADR-0019 §1) — NOT a ``cd``
    — so the child's writes land in the Tree with no leak to the parent checkout,
    sidestepping the bash-cwd-reset footgun (a subagent's bash resets to the parent repo
    per call; the process itself being rooted does not). ``runner`` is injectable so the
    contract is unit-tested without spawning a real child; it defaults to
    :func:`_exec_runner`, whose Exec pins ``stdin`` to ``/dev/null``.

    ``timeout`` is the child's process deadline, defaulting to :data:`LAUNCH_TIMEOUT`
    (``None`` — unbounded, the write/spawn-Run posture). The review producer passes the
    reviewer's ``--timeout`` here so a stalled review backend is KILLED at the deadline
    (#404): expiry raises :class:`~shipit.execrun.ExecError` (``cause=CAUSE_TIMEOUT``,
    partial streams carried), which the producer turns into its ``timed_out`` outcome.
    """
    if runner is None:
        runner = _exec_runner
    return runner(cmd, cwd=str(cwd), env=dict(env), timeout=timeout)


def _exec_runner(
    cmd: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout: float | None = LAUNCH_TIMEOUT,
) -> LaunchResult:
    """The real seam: one Exec through the runner (ADR-0028), as a consumer view.

    The launch contract's semantics ride on the runner's parameters, each one
    load-bearing:

    - ``check=False`` — a nonzero child is a normal lifecycle outcome the verb
      reports (ADR-0019 §6), a :class:`LaunchResult`, never an
      :class:`~shipit.execrun.ExecError`. The runner records it at DEBUG, like any
      other caller-declared-normal rc.
    - ``replace_env=True`` — ``env`` REPLACES the child's environment (the
      adapter's ``child_env`` has already scrubbed the backend's auth-shadowing
      vars) rather than merging over ``os.environ``; a scrubbed key must not creep
      back in.
    - ``timeout`` — the child's process deadline, defaulting to
      :data:`LAUNCH_TIMEOUT` (``None``: the runner's 5-minute default must never
      kill a legitimately long WRITE Run). The review producer passes a real
      deadline instead (#404) so a stalled review backend is killed and settled
      ``timed_out``; expiry raises :class:`~shipit.execrun.ExecError`
      (``cause=CAUSE_TIMEOUT``) with the partial streams, unlike the transport
      failures below.
    - stdin: the runner pins a no-``input`` Exec's stdin to ``/dev/null``
      (ADR-0020) — a TTY-less child otherwise waits ~3 s for stdin and warns
      (ADR-0019 §1).

    What can still raise is the transport itself: a missing backend binary or any
    OS-level launch failure surfaces as :class:`~shipit.execrun.ExecError` (no raw
    ``OSError`` escapes the runner), which the spawn verb maps to its clean exit-1.
    """
    result = execrun.run(
        cmd,
        cwd=cwd,
        env=env,
        replace_env=True,
        check=False,
        timeout=timeout,
    )
    return LaunchResult(
        returncode=result.rc,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def pixi_wrap(argv: list[str], tree_path: str | Path) -> list[str]:
    """Re-express ``argv`` to run THROUGH the Tree's pixi env — when the Tree has one.

    The launch bug (``docs/dev/pixi.lex`` §7, ADR-0019 amendment): ``cwd=<Tree>`` roots
    the child's writes in the Tree but does not activate pixi, so the child inherits the
    coordinator/system env and its tools resolve to the WRONG ``.pixi`` env. The fix is to
    launch the backend child *through* pixi — the wrapped argv is built by the pixi
    adapter (:func:`shipit.pixienv.run_argv`: explicit ``--manifest-path`` overrides
    any leaked ``PIXI_PROJECT_MANIFEST``; ADR-0028 puts the argv in pixi's domain
    home, this launcher keeps the routing decision).

    The wrap is GATED on the Tree carrying a provisioned env
    (:func:`shipit.pixienv.has_default_env`): a **write** Tree is ``pixi
    install``-provisioned, so it is routed; a **reviewer's read-only** Tree
    (ADR-0018, clone+checkout, no provision) and a **non-pixi repo** have no such
    env, so their argv is returned UNCHANGED — routing them through ``pixi run``
    would force a solve into a chmod'd tree or fail outright. Pure (a filesystem
    ``exists`` probe only), so the gate is table-tested without pixi.

    This probe-and-wrap fusion now serves only the READ-ONLY/reviewer tail
    (RPE01-WS05): the write tail probes the same sentinel once at the spawn
    seam, resolves a :class:`~shipit.workenv.WorkEnv`, and routes through
    :func:`route_argv` — the decision carried, not recalculated. The remaining
    callers converge on Work Env in RPE01-WS06.
    """
    tree = Path(tree_path)
    if not pixienv.has_default_env(tree):
        # Mechanics at DEBUG (ADR-0029): the routing DECISION is the diagnosis-
        # relevant fact when a child resolves the wrong tools — record which way
        # the gate went, and why.
        logger.debug(
            "launch argv left bare: %s carries no provisioned pixi env "
            "(read-only or non-pixi tree)",
            tree,
            extra={"pixi_wrapped": False},
        )
        return argv
    logger.debug(
        "launch argv routed through the tree's pixi env at %s",
        tree,
        extra={"pixi_wrapped": True},
    )
    return pixienv.run_argv(argv, tree)


def route_argv(argv: list[str], work_env: workenv.WorkEnv) -> list[str]:
    """Route ``argv`` per the resolved Work Env's execution-routing decision.

    The Work Env CONSUMER on the launch path (RPE01-WS05): the decision was
    made once, at the boundary that resolved ``work_env`` over supplied facts
    (:func:`shipit.workenv.resolve_write_run_env`); this function only carries
    it out — ``PIXI_RUN`` re-expresses the argv through the checkout's own
    pixi env via the pixi adapter's builder (:func:`shipit.pixienv.run_argv`,
    ADR-0028: the argv stays in pixi's domain home), anything else leaves the
    argv BARE (``AMBIENT`` — a non-pixi checkout keeps its existing launch
    behavior, honestly unrouted; ``ACTIVATION_SNAPSHOT`` contexts do not launch
    through this seam). No probe here: recomputing the gate is exactly the
    duplication Work Env exists to end. Pure argv-in/argv-out; the routing
    narration lands at DEBUG like :func:`pixi_wrap`'s (ADR-0029 mechanics).
    """
    root = work_env.working_dir.path
    if work_env.routing is workenv.ExecutionRouting.PIXI_RUN:
        logger.debug(
            "launch argv routed through the tree's pixi env at %s (work env)",
            root,
            extra={"pixi_wrapped": True},
        )
        return pixienv.run_argv(argv, root)
    logger.debug(
        "launch argv left bare: the work env at %s routes %s, not pixi-run",
        root,
        work_env.routing.value,
        extra={"pixi_wrapped": False},
    )
    return argv


def scrub_tree_env(env: Mapping[str, str]) -> dict[str, str]:
    """Drop leaked ``PIXI_*`` / Conda-activation project pointers from a child's env.

    Applied on TOP of the adapter's auth-env scrub (``BackendAdapter.child_env``), so the
    child inherits neither a stale auth var NOR a parent-project ``PIXI_PROJECT_MANIFEST``
    / ``CONDA_PREFIX`` that would bind it to the PARENT env. This mirrors the provisioning
    scrub (:func:`shipit.tree.create.provision_env`) on the launch path — the same leak
    class shipit already fixed once (#167) — by relying SOLELY on the pixi adapter's
    shared scrub (:func:`shipit.pixienv.scrub_env` over
    :func:`shipit.pixienv.is_leaked_env_var`), so the ``PIXI_*`` cache-var carve-out
    AND the Conda activation-vs-installation carve-out cannot drift between the two paths.
    The predicate scrubs only the Conda **activation** vars (``CONDA_PREFIX`` and friends),
    KEEPING installation-level ``CONDA_EXE`` / ``CONDA_PYTHON_EXE`` so a Conda-managed
    shell's ``pixi run`` is undisturbed. With explicit ``--manifest-path`` (see
    :func:`pixi_wrap`) the scrub is belt-and-suspenders for the child's own activation, but
    it still cleans the env the agent's *own* ``pixi`` calls inherit. Returns a fresh dict
    (never the caller's).
    """
    scrubbed = pixienv.scrub_env(env)
    dropped = sorted(set(env) - set(scrubbed))
    if dropped:
        # Mechanics at DEBUG (ADR-0029): variable NAMES only — never values, which
        # is also why this logs the drop-list rather than the surviving env.
        logger.debug(
            "scrubbed %d leaked env var(s) from the child env: %s",
            len(dropped),
            ", ".join(dropped),
            extra={"dropped": len(dropped)},
        )
    return scrubbed


def write_task(
    role: str, *, issue: int, branch: str, base_branch: str, closes: bool
) -> str:
    """The task a spawned WRITE Run performs: do the issue's work, report via a draft PR.

    WS02 replaces the WS01 sentinel skeleton with real, PR-reported work (acceptance
    #156): the Run, rooted in its Tree on ``branch`` (cut from ``base_branch``),
    implements issue ``#issue`` and opens a **draft** PR from that branch. The PR is
    the deliverable channel (ADR-0019 §6) — the parent never scrapes the Tree; it
    learns the result by resolving the PR the Run opened on ``branch``.

    ``closes`` selects the issue-link keyword by write shape (#649): ``closes=True``
    for a **standalone-issue** Run, whose PR body links ``closes #issue`` so the
    merge auto-closes the issue; ``closes=False`` for an **epic work-stream** Run,
    whose PR body links ``for #issue`` — deliberately NON-closing, because a WS
    issue must stay open until the umbrella PR integrates and closes the epic's
    issues. GitHub treats only ``closes`` (and its siblings) as a closing keyword,
    so the wrong keyword here either strands a merged standalone issue open or
    prematurely closes a WS issue at WS-merge time.

    The draft-PR-and-stop discipline (open one draft PR linking the issue, run
    ``shipit pr next`` once so the engine places the initial review requests, then
    STOP — never flip ready, address review rounds, or merge) lives in the role's own
    system prompt, which ``--agent <role>`` loads; this task only conveys *which*
    issue and restates the PR contract so the launched Run can never miss the one
    observable shipit reads back: a draft PR whose head is exactly ``branch``.

    The task also carries the **bank-state protocol** (#587): a Run that nears its
    wall-clock/budget before the draft PR is open must commit whatever exists to
    ``branch`` with a ``WIP:``-prefixed message and PUSH it, rather than exiting
    with loose work. The spawn still fails its exit contract (no open draft PR),
    but the pushed WIP commit turns that failure into a resumable handoff the
    coordinator can re-brief from — twice a killed Run's whole diagnosis was
    stranded uncommitted in its dead Tree, recoverable only by luck. The push is
    spelled ``git push -u origin {branch}`` because at bank time the branch is
    fresh (no draft PR yet ⇒ no upstream set), so a bare ``git push`` would reject
    the WIP commit and lose exactly the work the protocol exists to salvage.

    The task also carries the **headless foreground rule** (#663): a spawned Run
    is a headless child, so ending its turn exits the process — and any
    background tasks still running are killed with it. The affordance that is
    safe in an interactive session (launch background work, end the turn, get
    re-invoked on completion) is a silent kill in a headless Run: nothing ever
    re-invokes it. The rule is backend-neutral (it is about the Run's process
    lifecycle, not any CLI's flags) and rides EVERY spawned write Run's task:
    long work runs in the foreground (blocking) or is synchronously awaited, and
    the turn never ends while background work is in flight. Observed on
    RVW02-WS05, whose first Run backgrounded its replay pipelines, ended its
    turn to "wait for completion notifications", and lost them all at exit.
    """
    link = f"closes #{issue}" if closes else f"for #{issue}"
    return (
        f"You are a spawned {role} Run launched by `shipit spawn subagent`, working in "
        f"an isolated Tree checkout on branch {branch!r} (cut from {base_branch!r}). "
        f"Implement issue #{issue}: read it with `gh issue view {issue}`, make the "
        f"change with tests, and get the checks green. Then commit, push the branch "
        f"(`git push -u origin {branch}` — the branch is fresh, so set its upstream), "
        f"and open a DRAFT pull request from it against {base_branch!r} "
        f"(`gh pr create --draft --base {base_branch} --head {branch}`) whose body "
        f"references `{link}`. Once the draft PR is open, run `shipit pr next` "
        f"ONCE from the PR branch (the engine places the initial review requests), "
        f"then STOP — do not flip it ready, address review rounds, or merge. "
        f"If you are about to run out of time or budget BEFORE the draft PR is open, "
        f"bank your state instead of exiting with loose work: commit whatever exists "
        f"(even partial) to {branch!r} with a commit message starting `WIP:` that says "
        f"what is done and what remains, and push the branch with "
        f"`git push -u origin {branch}` (a fresh branch has no upstream yet, so a bare "
        f"`git push` would reject the commit and lose it) — a pushed WIP commit "
        f"turns the failed spawn into a resumable handoff instead of a silent loss. "
        f"You are a HEADLESS Run: ending your turn exits your process, and any "
        f"background tasks still running are killed with it — nothing re-invokes a "
        f"headless Run when background work completes (only interactive sessions "
        f"get that). Run long work (tests, builds, long scripts) in the foreground, "
        f"blocking, or await it synchronously before continuing; never end your "
        f"turn — even to 'wait for completion notifications' — while background "
        f"work is in flight."
    )


def reviewer_task(branch: str) -> str:
    """The task a spawned **reviewer** Run performs (ADR-0018): read the diff, review.

    The reviewer runs in a SHARED read-only Tree already checked out on ``branch``
    (the PR head), so its result is delivered THROUGH the PR (ADR-0017): it reads the
    diff and the surrounding code, then posts exactly one review with ``gh pr review``
    (approve / request-changes / comment) for the PR on this branch. It never edits,
    builds, pushes, or merges — the ``chmod``'d read-only Tree is the load-bearing FS
    guard across every backend (ADR-0018 / ADR-0020 §Decision 3), and each adapter adds
    its own native read-only posture as best-effort defense-in-depth: ``claude`` narrows
    to a read-only ``--tools`` allow-list, while ``codex`` / ``agy`` (no granular
    allow-list) rely on a sandbox/permission posture (codex ``--sandbox`` + network
    config; agy dropping ``--dangerously-skip-permissions``). The prompt states the
    intent on top of that.

    The diff is read with ``gh pr diff`` — NOT a hardcoded ``git diff origin/main…``:
    a work stream / epic PR targets its umbrella branch, not ``main``, so a baked-in
    base would compute the wrong range. ``gh pr diff`` uses the PR's own base/head, so
    the reviewer sees exactly the PR's changes whatever the base is.
    """
    return (
        "You are a spawned reviewer Run launched by `shipit spawn subagent`. You are "
        f"in a shared READ-ONLY checkout of the PR head `{branch}`. Read the PR's diff "
        "with `gh pr diff` (it uses the PR's actual base and head — do not assume the "
        "base is `main`) and the code it touches, judge it against the issue it closes "
        "and this repo's conventions, then post exactly ONE review through the PR with "
        "`gh pr review` (approve, request-changes, or comment). Do not edit, build, "
        "push, or merge — if a change is needed, say so in the review. Then stop."
    )
