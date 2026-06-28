"""Edit-enforcement decision — the ADR-0012 coordinator guard (security core).

`decide(role, path, is_code, break_glass) -> Decision` is the security-critical
core: the `edit` **operation** is **blocking** when the actor's **role** is
`coordinator` AND the path is a code path AND no **break-glass** marker is
present; every other combination is allowed. The deny `reason` *is* the
coordinator's role-prompt slice, so the rule arrives as a wall at the moment of
action (the block teaches the next step, not just stops).

Three concerns, kept separate:
  - **The verdict** (`decide`) — pure over `(role, is_code, break_glass)`. `path`
    rides along for the (future) richer reason + the boundary's logging, but the
    security matrix turns only on the role, the code-ness, and break-glass.
  - **The code-path classifier** (`is_code_path`) — its own module,
    `harness.codepath`. The boundary calls it and passes the bool in, so the
    verdict stays a pure function of plain values.
  - **What counts as an `edit` operation** (`is_edit_tool`) — a boundary gate so
    the hook only ever evaluates file-mutating tools; `decide()` presumes it is
    already looking at an edit.

Break-glass is an INPUT here (a bool), not a separate module: reading the marker
is the boundary's (impure) job, so this verdict stays a pure function of its
arguments and is fully unit-testable on its own. The boundary
(`shipit hook pretooluse`) translates a `DENY` into the Claude Code
`hookSpecificOutput` JSON and an `ALLOW` into silence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .role import Role

#: The coordinator deny reason — the role-prompt slice that teaches the next
#: action. This is still the WS01/WS02 PLACEHOLDER redirect string; WS03 swaps it
#: for the generated coordinator role-prompt slice (the same text injected as
#: context), so the deny wall and the prompt never disagree. Keep this constant
#: as the single seam: WS03 changes only what it points at.
COORDINATOR_DENY_REASON = (
    "You are the coordinator — delegate this edit to an implementer; "
    "branch off origin/main."
)

#: Tool names that count as a file-mutating `edit` **operation**. Claude Code
#: spells the write tools `Edit` / `Write` / `MultiEdit` / `NotebookEdit`;
#: matched case-insensitively so a casing drift can't silently disarm the guard.
_EDIT_TOOLS = frozenset({"edit", "write", "multiedit", "notebookedit"})


class Permission(StrEnum):
    """The decision verdict — mirrors Claude Code's `permissionDecision`."""

    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class Decision:
    """An edit-guard verdict: a permission + the reason carried on a deny."""

    permission: Permission
    reason: str = ""


def is_edit_tool(tool_name: str) -> bool:
    """True iff `tool_name` is a file-mutating `edit` operation (boundary gate).

    The hook may fire for any tool; this gate keeps the verdict scoped to the
    write tools so a non-edit call (Read/Bash/…) never reaches `decide()` and is
    allowed to proceed. Matched case-insensitively.
    """
    return tool_name.strip().lower() in _EDIT_TOOLS


def decide(role: Role, path: str, is_code: bool, break_glass: bool) -> Decision:
    """Decide whether an `edit` operation is allowed (ADR-0012). Pure.

    DENY iff a `coordinator` edits a code path with no break-glass marker; every
    other combination ALLOWs. The only path that blocks is the intended one — a
    subagent edit, a coordinator edit on a non-code path, and any edit under
    break-glass all pass. `path` is accepted for caller symmetry + logging; the
    verdict turns on `role`, `is_code`, and `break_glass` only.
    """
    if role is Role.COORDINATOR and is_code and not break_glass:
        return Decision(permission=Permission.DENY, reason=COORDINATOR_DENY_REASON)
    return Decision(permission=Permission.ALLOW)
