"""``harness/policy`` — the pure edit-, native-worktree- and spawn-isolation deny verdicts.

See docs/adr/0012-enforcement-via-native-hooks.md.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from .prompts import load_coordinator_slice
from .role import Role
from .roleprofile import (
    RoleValidationError,
    delegates_code_authorship,
    parse_role,
    profile_for,
)

#: The generated coordinator role-prompt slice, loaded once at import so `decide` stays pure.
COORDINATOR_DENY_REASON = load_coordinator_slice()

#: Tool names that count as a file-mutating `edit`, matched case-insensitively.
_EDIT_TOOLS = frozenset(
    {
        "apply_patch",
        "edit",
        "functions.apply_patch",
        "multiedit",
        "notebookedit",
        "write",
    }
)

#: Tool names that EXECUTE a shell command — Claude Code's `Bash` plus codex's
#: builtin shell tools — matched case-insensitively.
_SHELL_TOOLS = frozenset(
    {
        "bash",
        "exec_command",
        "shell",
        "unified_exec",
    }
)

#: Tool names that SPAWN a subagent, matched case-insensitively.
_SPAWN_TOOLS = frozenset({"agent"})

#: The `tool_input` keys a shell tool puts its command under: `command` (Claude
#: Code), `cmd` (codex).
_COMMAND_KEYS = ("command", "cmd")


class Permission(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class Decision:
    permission: Permission
    reason: str = ""


_ALLOW = Decision(permission=Permission.ALLOW)


@dataclass(frozen=True)
class ToolCall:
    """A `PreToolUse` payload projected onto the fields the deny rules read."""

    tool_name: str
    command: str = ""
    cwd: str = ""
    subagent_type: str = ""
    #: ``None`` when the spawn omitted `isolation` — the absence the spawn rule fires on.
    isolation: str | None = None

    @property
    def tool(self) -> str:
        """The tool name normalized for registry membership."""
        return self.tool_name.strip().lower()


def tool_call(payload: Mapping[str, object]) -> ToolCall:
    """Project a raw hook payload; a missing or malformed field reads as absent."""
    tool_input = payload.get("tool_input")
    fields = tool_input if isinstance(tool_input, Mapping) else {}
    command = next(
        (str(fields[key]) for key in _COMMAND_KEYS if fields.get(key)),
        "",
    )
    isolation = fields.get("isolation")
    return ToolCall(
        tool_name=str(payload.get("tool_name") or ""),
        command=command,
        cwd=str(payload.get("cwd") or ""),
        subagent_type=str(fields.get("subagent_type") or ""),
        isolation=None if isolation is None else str(isolation),
    )


def is_edit_tool(tool_name: str) -> bool:
    return tool_name.strip().lower() in _EDIT_TOOLS


def decide(role: Role, path: str, is_code: bool, break_glass: bool) -> Decision:
    """DENY iff a code-delegating role edits a code path with no break-glass; else ALLOW."""
    if delegates_code_authorship(role) and is_code and not break_glass:
        return Decision(permission=Permission.DENY, reason=COORDINATOR_DENY_REASON)
    return Decision(permission=Permission.ALLOW)


# The in-session `Agent(isolation:"worktree")` spawn is closed by `WorktreeCreate`.
WORKTREE_DENY_REASON = (
    "Trees are dissociated clones, not git worktrees (ADR-0014). Do not create a "
    "native git worktree — to spawn an isolated Run use `shipit spawn subagent` "
    "(ADR-0017), or run `shipit tree create` for a hand-driven isolated checkout. "
    "(A Tree is a full `git clone --reference --dissociate` in the central Trees "
    "root, so it can sit on any branch — including a branch another Tree holds — "
    "and `rm -rf` is a safe delete; a worktree can do neither.) In-session "
    '`Agent(isolation:"worktree")` is already rerouted into a Tree for you.'
)

_GIT_WORKTREE_ADD_FALLBACK = re.compile(r"\bgit\s+worktree\s+add\b")

#: With `punctuation_chars=True` shlex emits runs of these as standalone tokens.
_SHELL_SEPARATOR_CHARS = frozenset("();<>|&")

#: git global options taking a SEPARATE argument token, which consumes the next token.
_GIT_OPTS_WITH_ARG = frozenset({"-C", "-c"})


def _matches_enter_worktree(call: ToolCall) -> bool:
    return call.tool == "enterworktree"


def _segment_runs_worktree_add(tokens: list[str]) -> bool:
    i = 0
    n = len(tokens)
    while i < n and "=" in tokens[i] and not tokens[i].startswith("-"):
        i += 1
    if i >= n or tokens[i] != "git":
        return False
    i += 1
    while i < n and tokens[i].startswith("-"):
        opt = tokens[i]
        i += 1
        if opt in _GIT_OPTS_WITH_ARG and i < n:
            i += 1
    return i + 1 < n and tokens[i] == "worktree" and tokens[i + 1] == "add"


def _runs_git_worktree_add(command: str) -> bool:
    """True iff a Bash `command` actually EXECUTES `git worktree add`, not merely names it."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return _GIT_WORKTREE_ADD_FALLBACK.search(command) is not None
    segment: list[str] = []
    for tok in tokens:
        if tok and all(ch in _SHELL_SEPARATOR_CHARS for ch in tok):
            if _segment_runs_worktree_add(segment):
                return True
            segment = []
        else:
            segment.append(tok)
    return _segment_runs_worktree_add(segment)


def _matches_git_worktree_add(call: ToolCall) -> bool:
    if call.tool not in _SHELL_TOOLS:
        return False
    return _runs_git_worktree_add(call.command)


@dataclass(frozen=True)
class WorktreeDenyRule:
    name: str
    matches: Callable[[ToolCall], bool]


WORKTREE_DENY_RULES: tuple[WorktreeDenyRule, ...] = (
    WorktreeDenyRule("EnterWorktree", _matches_enter_worktree),
    WorktreeDenyRule("git worktree add", _matches_git_worktree_add),
)


def decide_worktree(call: ToolCall) -> Decision:
    """DENY iff the call matches :data:`WORKTREE_DENY_RULES`; role- and break-glass-independent."""
    for rule in WORKTREE_DENY_RULES:
        if rule.matches(call):
            return Decision(permission=Permission.DENY, reason=WORKTREE_DENY_REASON)
    return _ALLOW


SPAWN_ISOLATION_DENY_REASON = (
    "This role runs in its own Tree, so its spawn must be isolated: re-issue it "
    'with isolation: "worktree" (ADR-0014, ADR-0047). Without that parameter the '
    "subagent inherits THIS session's checkout as its cwd, and two write Runs on "
    "one checkout stomp each other. `shipit spawn subagent` provisions the Tree "
    "itself and needs no parameter — only the in-session Agent spawn does. The "
    "`explorer` role runs in the ambient WorkingDir with no Tree, and is never "
    "refused."
)


def decide_spawn_isolation(call: ToolCall) -> Decision:
    """DENY iff a spawn of a Tree-backed shipit Role omits `isolation`; any other `subagent_type` ALLOWs."""
    if call.tool not in _SPAWN_TOOLS or call.isolation is not None:
        return _ALLOW
    try:
        role = parse_role(call.subagent_type)
    except RoleValidationError:
        return _ALLOW
    if not profile_for(role).checkout.tree_backed:
        return _ALLOW
    return Decision(permission=Permission.DENY, reason=SPAWN_ISOLATION_DENY_REASON)
