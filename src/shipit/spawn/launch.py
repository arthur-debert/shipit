"""``spawn/launch`` — the headless-``claude`` child-process launcher (ADR-0019).

The launch contract WS01 implements *verbatim* from the WS00 spike. For the
``claude`` backend the Run is the ``claude`` CLI in headless print mode —
``claude -p "<task>" --agent <role> --permission-mode bypassPermissions
--output-format json`` — run as a subprocess with **``cwd`` = the Tree**, **stdin
from ``/dev/null``**, and **``ANTHROPIC_API_KEY`` scrubbed from the child env**.

Two of those are non-obvious, load-bearing spike findings (ADR-0019 §2/§3) that a
paper decision would have missed:

- **``--agent <role>``** is not cosmetic: a headless ``claude -p`` child is a fresh
  *top-level* session, so its ``PreToolUse`` payload carries no ``agent_type`` —
  which :func:`shipit.harness.role.resolve_role` maps to ``coordinator``, the role
  the guard forbids from editing. The native ``--agent`` flag populates
  ``agent_type`` so the guard allows the spawned Run's own edits. No change to
  ``resolve_role`` is needed; the launcher just passes the flag.
- **Scrubbing ``ANTHROPIC_API_KEY``** is a hard requirement: a stale/invalid key in
  the env takes precedence over the claude.ai OAuth/keychain login and breaks the
  child's auth ("Invalid API key"). Removing it makes the child use the keychain
  login the parent is already logged in with.

The module is split pure-from-effectful so the whole contract is table-tested
without spawning a real ``claude``: :func:`build_command` / :func:`child_env` are
pure, and :func:`launch` takes an injectable ``runner`` (defaulting to the real
:func:`_subprocess_runner`).
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

#: The env var the launcher MUST remove from the child env (ADR-0019 §3): a
#: stale/invalid value takes precedence over the claude.ai OAuth/keychain login and
#: breaks auth. Scrubbing it is a hard contract requirement, not a nicety.
ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"


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


def build_command(task: str, role: str, *, output_format: str = "json") -> list[str]:
    """The exact ``claude`` print-mode argv ADR-0019 §1 specifies.

    ``claude -p "<task>" --agent <role> --permission-mode bypassPermissions
    --output-format json``. Two args are load-bearing: ``--agent <role>`` populates
    the hook payload's ``agent_type`` so the coordinator-guard allows the Run's own
    edits (§2), and ``--permission-mode bypassPermissions`` is the write-Run mode
    (§4) — still bounded by the guard, which fires inside the child. ``-p`` makes it
    a blocking foreground Run; ``--output-format json`` yields the single result
    envelope the parent treats as the exit signal.
    """
    return [
        "claude",
        "-p",
        task,
        "--agent",
        role,
        "--permission-mode",
        "bypassPermissions",
        "--output-format",
        output_format,
    ]


def child_env(parent_env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The child's environment: the parent's, with ``ANTHROPIC_API_KEY`` REMOVED.

    ADR-0019 §3 (a load-bearing spike finding): a stale/invalid
    ``ANTHROPIC_API_KEY`` in the env takes precedence over the claude.ai
    OAuth/keychain login and breaks the child's auth. Scrubbing it is a hard
    contract requirement so the keychain login is used; everything else inherits.
    ``parent_env`` defaults to the live :data:`os.environ` and is injectable for
    tests.
    """
    source = os.environ if parent_env is None else parent_env
    return {key: value for key, value in source.items() if key != ANTHROPIC_API_KEY}


def launch(
    cmd: list[str],
    *,
    cwd: str | Path,
    env: Mapping[str, str],
    runner: Runner | None = None,
) -> LaunchResult:
    """Run the headless-``claude`` child rooted in the Tree and return its result.

    Rooting is the OS process ``cwd`` (ADR-0019 §1) — NOT a ``cd`` — so the child's
    writes land in the Tree with no leak to the parent checkout, sidestepping the
    bash-cwd-reset footgun (a subagent's bash resets to the parent repo per call;
    the process itself being rooted does not). ``runner`` is injectable so the
    contract is unit-tested without spawning a real ``claude``; it defaults to
    :func:`_subprocess_runner`, which redirects ``stdin`` from ``/dev/null``.
    """
    if runner is None:
        runner = _subprocess_runner
    return runner(cmd, cwd=str(cwd), env=dict(env))


def _subprocess_runner(
    cmd: list[str], *, cwd: str, env: dict[str, str]
) -> LaunchResult:
    """The real subprocess seam: ``claude`` in print mode, ``stdin`` from ``/dev/null``.

    ``stdin`` is redirected from ``/dev/null`` because a TTY-less child otherwise
    waits ~3 s for stdin and warns (ADR-0019 §1). ``env`` REPLACES the child's
    environment (the caller has already scrubbed ``ANTHROPIC_API_KEY`` via
    :func:`child_env`) rather than merging over :data:`os.environ` the way
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
