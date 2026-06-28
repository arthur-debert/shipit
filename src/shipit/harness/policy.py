"""Edit-enforcement decision — the ADR-0012 coordinator guard, minimal slice.

`decide(role, tool_name, path) -> Decision` is the security-critical core: the
`edit` **operation** is BLOCKING when the actor's **role** is `coordinator` and
the path is a code path; everything else is allowed. The deny `reason` *is* the
coordinator's role-prompt slice, so the rule arrives as a wall at the moment of
action (the block teaches the next step, not just stops).

WS01 scope (deliberately small + replaceable):
  - `is_code_path()` is a HARDCODED default — anything under a `src/` directory.
    WS02 swaps it for the real path→toolchain classifier (ADR-0007).
  - **break-glass** is not yet an input. WS02 adds it to `decide()`'s signature
    so a logged marker can let the coordinator through.

Pure: a function of its arguments only, no I/O. The boundary
(`shipit hook pretooluse`) translates a `DENY` into the Claude Code
`hookSpecificOutput` JSON and an `ALLOW` into silence (no decision — normal
permission flow proceeds), so this verdict is fully unit-testable on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePath

from .role import Role

#: The coordinator deny reason — the role-prompt slice that teaches the next
#: action. WS03 wires the full generated role prompt; WS01 carries this fixed
#: redirect string, which is also the `permissionDecisionReason` the hook emits.
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


def is_code_path(path: str) -> bool:
    """True iff `path` is a code path the coordinator may not edit (WS01 default).

    HARDCODED for WS01: a path with a `src` directory segment is code. Handles
    both repo-relative (`src/shipit/x.py`) and absolute
    (`/…/shipit/src/shipit/x.py`) forms. WS02 replaces this with the real
    path→toolchain map; docs / `.lex` / config outside `src/` stay non-code, so
    the coordinator's planning + authoring proceed normally.
    """
    if not path:
        return False
    return "src" in PurePath(path).parts


def decide(role: Role, tool_name: str, path: str) -> Decision:
    """Decide whether an `edit` operation is allowed (ADR-0012).

    DENY iff a `coordinator` performs an `edit` operation on a code path; every
    other combination ALLOWs. The only path that blocks is the intended one — a
    subagent edit, a non-edit tool, or a coordinator edit on a non-code path all
    pass.
    """
    if (
        role is Role.COORDINATOR
        and tool_name.strip().lower() in _EDIT_TOOLS
        and is_code_path(path)
    ):
        return Decision(permission=Permission.DENY, reason=COORDINATOR_DENY_REASON)
    return Decision(permission=Permission.ALLOW)
