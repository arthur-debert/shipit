"""Backend-agnostic child-process launch machinery and the write-Run task prompts."""

from __future__ import annotations

import logging
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from .. import execrun, pixienv, workenv

logger = logging.getLogger("shipit.spawn")

#: Explicitly unbounded: a write Run legitimately works an entire issue end-to-end.
#: Callers with a bounded posture pass their own deadline instead.
LAUNCH_TIMEOUT: float | None = None


@dataclass(frozen=True)
class LaunchResult:
    """The finished Run's process exit and its raw streams."""

    returncode: int
    stdout: str
    stderr: str


#: The injectable subprocess seam; a ``None`` ``timeout`` means unbounded.
Runner = Callable[..., LaunchResult]

#: The per-stream cap on a failed child's reported tail: wide enough for a headless
#: backend's error block, bounded so a runaway stream cannot bury the refusal.
STREAM_TAIL_CHARS = 2000


def launch(
    cmd: list[str],
    *,
    cwd: str | Path,
    env: Mapping[str, str],
    timeout: float | None = LAUNCH_TIMEOUT,
    runner: Runner | None = None,
) -> LaunchResult:
    """Run the backend child rooted at ``cwd`` on a SCRUBBED env; ``timeout`` is its deadline."""
    if runner is None:
        runner = _exec_runner
    return runner(cmd, cwd=str(cwd), env=scrub_tree_env(env), timeout=timeout)


def _exec_runner(
    cmd: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout: float | None = LAUNCH_TIMEOUT,
) -> LaunchResult:
    """The real seam. A nonzero child is a result; only transport failures raise."""
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


def route_argv(argv: list[str], work_env: workenv.WorkEnv) -> list[str]:
    """Carry out the Work Env's routing decision; an activation snapshot is refused."""
    root = work_env.working_dir.path
    if work_env.routing is workenv.ExecutionRouting.PIXI_RUN:
        logger.debug(
            "launch argv routed through the tree's pixi env at %s (work env)",
            root,
            extra={"pixi_wrapped": True},
        )
        return pixienv.run_argv(argv, root)
    if work_env.routing is workenv.ExecutionRouting.AMBIENT:
        logger.debug(
            "launch argv left bare: the work env at %s routes ambient",
            root,
            extra={"pixi_wrapped": False},
        )
        return argv
    raise ValueError(
        f"unsupported launch routing at this seam: {work_env.routing.value}; "
        "activation-snapshot contexts require their activation consumer"
    )


def scrub_tree_env(env: Mapping[str, str]) -> dict[str, str]:
    """A fresh env with leaked ``PIXI_*`` / Conda-activation project pointers dropped."""
    scrubbed = pixienv.scrub_env(env)
    dropped = sorted(set(env) - set(scrubbed))
    if dropped:
        # Variable NAMES only — never values.
        logger.debug(
            "scrubbed %d leaked env var(s) from the child env: %s",
            len(dropped),
            ", ".join(dropped),
            extra={"dropped": len(dropped)},
        )
    return scrubbed


def stream_tail(text: str, *, limit: int = STREAM_TAIL_CHARS) -> str:
    """``text`` stripped, kept to its trailing ``limit`` chars; a trim is marked inline."""
    trimmed = text.strip()
    if len(trimmed) <= limit:
        return trimmed
    return f"…({len(trimmed) - limit} earlier chars elided)…\n{trimmed[-limit:]}"


def child_failure_detail(
    result: LaunchResult,
    *,
    backend: str,
    tree_path: str,
    duration_ms: int,
) -> str:
    """A nonzero child's refusal text: a bounded tail of BOTH streams, or why there is none."""
    headline = (
        f"{backend} child exited {result.returncode} after {duration_ms}ms "
        f"in the tree at {tree_path}"
    )
    # stdout FIRST: a headless backend reports its own errors there, so stderr-only
    # reporting is structurally blind to the most common failure (#1153).
    streams = [
        f"--- child {name} (tail) ---\n{tail}"
        for name, tail in (
            ("stdout", stream_tail(result.stdout)),
            ("stderr", stream_tail(result.stderr)),
        )
        if tail
    ]
    if not streams:
        return (
            f"{headline}, and wrote NOTHING to either stdout or stderr — it left no "
            "account of why it failed. Inspect that tree and the child's own "
            "transcript; the exit code is the only signal it produced."
        )
    return "\n".join([headline, *streams])


def write_task(
    role: str, *, issue: int, branch: str, base_branch: str, closes: bool
) -> str:
    """The task text for a write Run; ``closes`` picks the issue-link keyword."""
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


def shepherd_task(*, pr_number: int, branch: str, base_branch: str) -> str:
    """The task text for a shepherd Run: address one existing PR in place."""
    push_refspec = shlex.quote(f"HEAD:refs/heads/{branch}")
    return (
        "You are a spawned shepherd Run launched by `shipit spawn subagent`, "
        f"attached to existing pull request #{pr_number} on branch {branch!r} "
        f"(base {base_branch!r}). Work in this writable Tree and address the "
        "currently open review feedback for that PR. Commit the fixes and push "
        f"them back to the same branch with `git push origin {push_refspec}`. Do NOT "
        "open a new pull request, do NOT run the implementer draft-PR handshake, "
        "do NOT run `shipit pr next`, do NOT flip the PR ready, do NOT wait for "
        "reviewers, and do NOT merge. If a review comment should not be changed, "
        "leave a clear rationale on the existing PR and resolve the thread; "
        "otherwise fix it and resolve it. Sweep the diff for other instances of "
        "the same finding class before pushing. You are a HEADLESS Run: ending "
        "your turn exits your process, and any background tasks still running "
        "are killed with it. Run long work in the foreground or await it "
        "synchronously; never end your turn while background work is in flight."
    )
