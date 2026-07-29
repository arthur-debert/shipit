"""Shared pure renderers for the ``pr`` family."""

from __future__ import annotations

from ...prstate.state import TaskState, TaskStatus


def format_status(status: TaskStatus) -> str:
    """A :class:`TaskStatus` as the readable block."""
    if status.state is TaskState.NO_PR:
        return f"state:  no_pr\nnext:   {status.next_action}"
    reviewers = "  ".join(f"{name}={lc}" for name, lc in status.reviewers.items())
    degraded_list = ", ".join(
        f"{name} {reason}" for name, reason in status.degraded.items()
    )
    degraded_note = f" (degraded: {degraded_list})" if status.degraded else ""
    lines = [
        f"PR #{status.pr}",
        f"state:      {status.state.value}{degraded_note}",
        f"next:       {status.next_action}",
        f"reviewers:  {reviewers}",
        f"threads:    {status.open_threads} open",
        f"checks:     {status.checks.value}",
        f"mergeable:  {status.mergeable}",
        f"cycles:     {status.cycles}",
    ]
    if status.degraded:
        lines.append(f"degraded:   {degraded_list}")
    if status.breaker:
        lines.append(f"breaker:    {status.breaker}")
    return "\n".join(lines)
