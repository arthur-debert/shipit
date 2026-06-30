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
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path


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
    :func:`shipit.proc.run` does — a scrubbed key must not creep back in. ``check``
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
    builds, pushes, or merges — the backend's read-only tool allow-list and the
    ``chmod``'d working files enforce that; the prompt states the intent.

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
