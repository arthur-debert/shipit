"""Shared pure renderers for the ``pr`` family — render-seam helpers, not a verb.

The one status block both `pr status` and `pr next` print lives here so the
two verbs render identically WITHOUT importing each other's module (CLI01-WS03:
no pr verb imports another verb; shared rendering goes through the render
seam). Pure ``format_*(result) -> str`` functions per ADR-0030 — no printing;
:func:`~shipit.verbs._render.emit` owns the terminal write.
"""

from __future__ import annotations

from ...prstate.state import TaskState, TaskStatus


def format_status(status: TaskStatus) -> str:
    """The pure text renderer: a :class:`TaskStatus` as the readable block.

    A plain string function (no printing — the render seam owns the terminal),
    so text-output tests assert on the return value. ``no_pr`` renders the
    short two-line form; a full status renders the labelled block.
    """
    if status.state is TaskState.NO_PR:
        return f"state:  no_pr\nnext:   {status.next_action}"
    reviewers = "  ".join(f"{name}={lc}" for name, lc in status.reviewers.items())
    # A degraded PR is annotated INLINE on the state line — "ready (degraded:
    # codex-local failed)" — so the one line a reader scans already carries the
    # warning (ADR-0006: a degraded PR is never silently "fine"). The full set is
    # also listed on its own line for legibility when several reviewers degraded.
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
