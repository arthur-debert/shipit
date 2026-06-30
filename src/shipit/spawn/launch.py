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

#: The trivial, verifiable artifact the skeleton's spawned child writes at the root
#: of its Tree — the one observable that proves (acceptance #155) the Run executed
#: IN the Tree, not the parent checkout. Real, PR-reported work replaces this in
#: later work streams; the sentinel is purely the walking skeleton's proof of life.
SENTINEL_NAME = ".shipit-spawn-sentinel"

#: The exact, entire contents the sentinel must have — kept in one place so the task
#: prompt that instructs the child and :func:`sentinel_present`, which verifies the
#: child wrote precisely this, never drift.
SENTINEL_BODY = "spawned by shipit\n"

#: The read-only tool allow-list for a **reviewer** Run (ADR-0018 / ADR-0019 §4): a
#: reviewer reads the diff and code and posts a review, so it gets the read tools plus
#: ``Bash`` (to run ``git diff`` and ``gh pr review``) but NOT ``Write`` / ``Edit`` —
#: the read-only posture rides the ``--tools`` allow-list, mirroring the reviewer
#: agent-def frontmatter. Passed to :func:`build_command` as ``tools``.
REVIEWER_TOOLS = ("Read", "Grep", "Glob", "Bash")


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


def build_command(
    task: str,
    role: str,
    *,
    output_format: str = "json",
    tools: tuple[str, ...] | list[str] | None = None,
) -> list[str]:
    """The exact ``claude`` print-mode argv ADR-0019 §1 specifies.

    ``claude -p "<task>" --agent <role> --permission-mode bypassPermissions
    [--tools "<allowlist>"] --output-format json``. Two args are load-bearing:
    ``--agent <role>`` populates the hook payload's ``agent_type`` so the
    coordinator-guard allows the Run's own edits (§2), and ``--permission-mode
    bypassPermissions`` is the write-Run mode (§4) — still bounded by the guard,
    which fires inside the child. ``-p`` makes it a blocking foreground Run;
    ``--output-format json`` yields the single result envelope the parent treats as
    the exit signal.

    ``tools`` narrows tool access per role (§4): a **reviewer** passes
    :data:`REVIEWER_TOOLS` so the child gets only read-only tools (no ``Write`` /
    ``Edit``) via ``--tools "<comma-joined>"``. ``None`` (a write Run) omits the flag
    and inherits the role's full toolset.
    """
    cmd = [
        "claude",
        "-p",
        task,
        "--agent",
        role,
        "--permission-mode",
        "bypassPermissions",
    ]
    if tools:
        cmd += ["--tools", ",".join(tools)]
    cmd += ["--output-format", output_format]
    return cmd


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


def skeleton_task(role: str) -> str:
    """The trivial, verifiable task the skeleton's spawned child performs.

    It instructs the child to write :data:`SENTINEL_NAME` (containing
    :data:`SENTINEL_BODY`) at the root of its Tree — the one observable that proves
    (acceptance #155) the Run executed IN the Tree, not the parent checkout. Real,
    PR-reported work replaces this in later work streams.
    """
    return (
        f"You are a spawned {role} Run launched by `shipit spawn subagent` to prove "
        f"the Tree launch contract (ADR-0019). Create a file named {SENTINEL_NAME!r} "
        f"at the root of this checkout whose entire contents are the single line "
        f"{SENTINEL_BODY.strip()!r}. Do nothing else, then stop."
    )


def reviewer_task(branch: str) -> str:
    """The task a spawned **reviewer** Run performs (ADR-0018): read the diff, review.

    The reviewer runs in a SHARED read-only Tree already checked out on ``branch``
    (the PR head), so its result is delivered THROUGH the PR (ADR-0017): it reads the
    diff and the surrounding code, then posts exactly one review with ``gh pr review``
    (approve / request-changes / comment) for the PR on this branch. It never edits,
    builds, pushes, or merges — the read-only ``--tools`` allow-list and the
    ``chmod``'d working files enforce that; the prompt states the intent.
    """
    return (
        "You are a spawned reviewer Run launched by `shipit spawn subagent`. You are "
        f"in a shared READ-ONLY checkout of the PR head `{branch}`. Read the PR's diff "
        "(`git diff origin/main...HEAD` or `gh pr diff`) and the code it touches, "
        "judge it against the issue it closes and this repo's conventions, then post "
        "exactly ONE review through the PR with `gh pr review` (approve, "
        "request-changes, or comment). Do not edit, build, push, or merge — if a "
        "change is needed, say so in the review. Then stop."
    )


def sentinel_path(tree_path: str | Path) -> Path:
    """Where the skeleton sentinel lives for a Tree at ``tree_path``."""
    return Path(tree_path) / SENTINEL_NAME


def sentinel_present(tree_path: str | Path) -> bool:
    """Whether the spawned child wrote the *correct* sentinel into the Tree.

    The proof of life (acceptance #155) is not merely a file at the right path but a
    file whose entire contents are :data:`SENTINEL_BODY` — the exact line the skeleton
    task instructs the child to write. Existence alone is too weak: an empty,
    truncated, or stray write would falsely report success. A missing file, an
    unreadable one, or any content that is not :data:`SENTINEL_BODY` all count as
    absent.
    """
    try:
        return sentinel_path(tree_path).read_text() == SENTINEL_BODY
    except OSError:
        # Missing, a directory, or otherwise unreadable — all "not present".
        return False
