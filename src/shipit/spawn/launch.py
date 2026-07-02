"""``spawn/launch`` — the **backend-agnostic** child-process launch machinery.

The pieces here are the same whatever the backend is (``claude``, ``codex``,
``antigravity``) — they are the shared half of the ADR-0020 seam. What *varies* per
backend (the argv, the auth-env scrub, the reviewer read-only posture) lives behind a
:class:`~shipit.spawn.backends.base.BackendAdapter` in :mod:`shipit.spawn.backends`; this
module holds everything that does NOT vary:

- :func:`launch` — runs a resolved ``cmd``/``cwd``/``env`` through an injectable
  ``runner``, rooting the child in the Tree (``cwd`` = the Tree, **stdin from
  ``/dev/null``**). The runner is the subprocess seam, swapped for a fake in tests.
- :func:`write_task` / :func:`reviewer_task` — the English PR-contract prompts a Run is
  handed. These are backend-agnostic: they convey *what work to do and how to report
  it* (the PR is the result channel, ADR-0019 §6), not how any particular CLI is shaped.

Rooting is the OS process ``cwd`` (ADR-0019 §1), NOT a ``cd`` — so the child's writes
land in the Tree with no leak to the parent checkout, sidestepping the bash-cwd-reset
footgun. The module is split pure-from-effectful so the contract is table-tested without
spawning a real child: :func:`launch` takes an injectable ``runner`` (defaulting to the
real :func:`_subprocess_runner`).

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
chmod'd tree or fail outright. :func:`scrub_tree_env` mirrors the provisioning scrub
(:func:`shipit.tree.create.provision_env`) — both rely SOLELY on the shared predicate
:func:`shipit.tree.create.is_leaked_env_var`, so they cannot drift: on top of the
adapter's auth-env scrub it drops leaked ``PIXI_*`` project pointers and Conda
**activation** vars (``CONDA_PREFIX`` & friends; installation-level ``CONDA_EXE`` etc. are
KEPT) so the child — and the agent's own ``pixi`` calls — re-resolve from the Tree. ``--clean-env`` is NOT used: it was falsified
(it strips ``HOME``/``PATH``, so the child finds neither python nor the ``claude`` binary,
rc 127); the curated passthrough keeps ``HOME``/``PATH`` intact (spike, 2026-06-30).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..tree.create import is_leaked_env_var


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


#: The injectable subprocess seam: given the resolved ``cmd``/``cwd``/``env`` it runs
#: the child and returns its :class:`LaunchResult`. Tests pass a fake so the launch
#: contract is asserted WITHOUT spawning a real ``claude``; production uses
#: :func:`_subprocess_runner`.
Runner = Callable[..., LaunchResult]


def launch(
    cmd: list[str],
    *,
    cwd: str | Path,
    env: Mapping[str, str],
    runner: Runner | None = None,
) -> LaunchResult:
    """Run the resolved backend child rooted in the Tree and return its result.

    ``cmd`` / ``env`` come from the selected backend's adapter (ADR-0020); this launcher
    is backend-agnostic. Rooting is the OS process ``cwd`` (ADR-0019 §1) — NOT a ``cd``
    — so the child's writes land in the Tree with no leak to the parent checkout,
    sidestepping the bash-cwd-reset footgun (a subagent's bash resets to the parent repo
    per call; the process itself being rooted does not). ``runner`` is injectable so the
    contract is unit-tested without spawning a real child; it defaults to
    :func:`_subprocess_runner`, which redirects ``stdin`` from ``/dev/null``.
    """
    if runner is None:
        runner = _subprocess_runner
    return runner(cmd, cwd=str(cwd), env=dict(env))


def _subprocess_runner(
    cmd: list[str], *, cwd: str, env: dict[str, str]
) -> LaunchResult:
    """The real subprocess seam: the backend child, ``stdin`` from ``/dev/null``.

    ``stdin`` is redirected from ``/dev/null`` because a TTY-less child otherwise
    waits ~3 s for stdin and warns (ADR-0019 §1). ``env`` REPLACES the child's
    environment (the adapter's ``child_env`` has already scrubbed the backend's
    auth-shadowing vars) rather than merging over :data:`os.environ` the way
    :func:`shipit.execrun.run` does — a scrubbed key must not creep back in. ``check``
    is False: a nonzero child is a normal lifecycle outcome the verb reports, not an
    exception.
    """
    completed = subprocess.run(  # noqa: S603 — cmd is a constructed list, never shell-interpolated
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    )
    return LaunchResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


#: The provisioned-env sentinel: a Tree carries a usable pixi env iff this directory
#: exists under it (``pixi install`` materializes ``<tree>/.pixi/envs/default``). It is
#: the gate :func:`pixi_wrap` keys on — present ⇒ route the child through ``pixi run``;
#: absent (a reviewer's read-only Tree, or a non-pixi repo) ⇒ leave the argv bare.
PIXI_DEFAULT_ENV = (".pixi", "envs", "default")


def pixi_wrap(argv: list[str], tree_path: str | Path) -> list[str]:
    """Re-express ``argv`` to run THROUGH the Tree's pixi env — when the Tree has one.

    The launch bug (``docs/dev/pixi.lex`` §7, ADR-0019 amendment): ``cwd=<Tree>`` roots
    the child's writes in the Tree but does not activate pixi, so the child inherits the
    coordinator/system env and its tools resolve to the WRONG ``.pixi`` env. The fix is to
    launch the backend child *through* pixi:
    ``pixi run --manifest-path <tree>/pixi.toml -- <argv>`` (the ``--`` separates pixi's
    own args from the child argv; explicit ``--manifest-path`` overrides any leaked
    ``PIXI_PROJECT_MANIFEST``).

    The wrap is GATED on the Tree carrying a provisioned env (:data:`PIXI_DEFAULT_ENV`
    exists): a **write** Tree is ``pixi install``-provisioned, so it is routed; a
    **reviewer's read-only** Tree (ADR-0018, clone+checkout, no provision) and a
    **non-pixi repo** have no such env, so their argv is returned UNCHANGED — routing
    them through ``pixi run`` would force a solve into a chmod'd tree or fail outright.
    Pure (a filesystem ``exists`` probe only), so the gate is table-tested without pixi.
    """
    tree = Path(tree_path)
    if not tree.joinpath(*PIXI_DEFAULT_ENV).exists():
        return argv
    return ["pixi", "run", "--manifest-path", str(tree / "pixi.toml"), "--", *argv]


def scrub_tree_env(env: Mapping[str, str]) -> dict[str, str]:
    """Drop leaked ``PIXI_*`` / Conda-activation project pointers from a child's env.

    Applied on TOP of the adapter's auth-env scrub (``BackendAdapter.child_env``), so the
    child inherits neither a stale auth var NOR a parent-project ``PIXI_PROJECT_MANIFEST``
    / ``CONDA_PREFIX`` that would bind it to the PARENT env. This mirrors the provisioning
    scrub (:func:`shipit.tree.create.provision_env`) on the launch path — the same leak
    class shipit already fixed once (#167) — by relying SOLELY on the shared predicate
    :func:`~shipit.tree.create.is_leaked_env_var`, so the ``PIXI_*`` cache-var carve-out
    AND the Conda activation-vs-installation carve-out cannot drift between the two paths.
    The predicate scrubs only the Conda **activation** vars (``CONDA_PREFIX`` and friends),
    KEEPING installation-level ``CONDA_EXE`` / ``CONDA_PYTHON_EXE`` so a Conda-managed
    shell's ``pixi run`` is undisturbed. With explicit ``--manifest-path`` (see
    :func:`pixi_wrap`) the scrub is belt-and-suspenders for the child's own activation, but
    it still cleans the env the agent's *own* ``pixi`` calls inherit. Returns a fresh dict
    (never the caller's).
    """
    return {key: value for key, value in env.items() if not is_leaked_env_var(key)}


def write_task(role: str, *, issue: int, branch: str, base_branch: str) -> str:
    """The task a spawned WRITE Run performs: do the issue's work, report via a draft PR.

    WS02 replaces the WS01 sentinel skeleton with real, PR-reported work (acceptance
    #156): the Run, rooted in its Tree on ``branch`` (cut from ``base_branch``),
    implements issue ``#issue`` and opens a **draft** PR from that branch. The PR is
    the deliverable channel (ADR-0019 §6) — the parent never scrapes the Tree; it
    learns the result by resolving the PR the Run opened on ``branch``.

    The draft-PR-and-stop discipline (open one draft PR linking ``for #issue``, then
    STOP at PR-open — never flip ready or merge) lives in the role's own system prompt,
    which ``--agent <role>`` loads; this task only conveys *which* issue and restates
    the PR contract so the launched Run can never miss the one observable shipit reads
    back: a draft PR whose head is exactly ``branch``.
    """
    return (
        f"You are a spawned {role} Run launched by `shipit spawn subagent`, working in "
        f"an isolated Tree checkout on branch {branch!r} (cut from {base_branch!r}). "
        f"Implement issue #{issue}: read it with `gh issue view {issue}`, make the "
        f"change with tests, and get the checks green. Then commit, push {branch!r}, and "
        f"open a DRAFT pull request from it against {base_branch!r} "
        f"(`gh pr create --draft --base {base_branch} --head {branch}`) whose body "
        f"references `for #{issue}`. STOP once the draft PR is open — do not flip it "
        f"ready, request reviews, or merge."
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
