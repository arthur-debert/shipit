"""``harness/policy`` — the pure edit-enforcement and native-worktree deny verdicts.

See docs/adr/0012-enforcement-via-native-hooks.md.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from .prompts import load_coordinator_slice
from .role import Role
from .roleprofile import delegates_code_authorship

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


class Permission(StrEnum):
    """The verdict — mirrors Claude Code's `permissionDecision`."""

    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class Decision:
    """An edit-guard verdict: a permission + the reason carried on a deny."""

    permission: Permission
    reason: str = ""


def is_edit_tool(tool_name: str) -> bool:
    """True iff `tool_name` is a file-mutating `edit` operation — the boundary gate."""
    return tool_name.strip().lower() in _EDIT_TOOLS


def decide(role: Role, path: str, is_code: bool, break_glass: bool) -> Decision:
    """DENY iff a code-delegating role edits a code path with no break-glass; else ALLOW.

    `path` rides along for logging; the verdict turns on the other three only.
    """
    if delegates_code_authorship(role) and is_code and not break_glass:
        return Decision(permission=Permission.DENY, reason=COORDINATOR_DENY_REASON)
    return Decision(permission=Permission.ALLOW)


# The native-worktree deny guard: a second, role-independent deny surface on the
# same PreToolUse channel. The in-session `Agent(isolation:"worktree")` spawn,
# which PreToolUse cannot see, is closed by the `WorktreeCreate` hook instead.

WORKTREE_DENY_REASON = (
    "Trees are dissociated clones, not git worktrees (ADR-0014). Do not create a "
    "native git worktree — to spawn an isolated Run use `shipit spawn subagent` "
    "(ADR-0017), or run `shipit tree create` for a hand-driven isolated checkout. "
    "(A Tree is a full `git clone --reference --dissociate` in the central Trees "
    "root, so it can sit on any branch — including a branch another Tree holds — "
    "and `rm -rf` is a safe delete; a worktree can do neither.) In-session "
    '`Agent(isolation:"worktree")` is already rerouted into a Tree for you.'
)

#: Substring fallback used only when a command cannot be tokenized, so it fails closed.
_GIT_WORKTREE_ADD_FALLBACK = re.compile(r"\bgit\s+worktree\s+add\b")

#: With `punctuation_chars=True` shlex emits runs of these as standalone tokens,
#: which is how a compound command splits into independently inspected segments.
_SHELL_SEPARATOR_CHARS = frozenset("();<>|&")

#: git global options taking a SEPARATE argument token, which consumes the next token.
_GIT_OPTS_WITH_ARG = frozenset({"-C", "-c"})


def _matches_enter_worktree(tool_name: str, command: str) -> bool:
    return tool_name.strip().lower() == "enterworktree"


def _segment_runs_worktree_add(tokens: list[str]) -> bool:
    """True iff ONE simple command's tokens run `git worktree add`."""
    i = 0
    n = len(tokens)
    # Skip leading environment assignments (`FOO=bar git …`).
    while i < n and "=" in tokens[i] and not tokens[i].startswith("-"):
        i += 1
    if i >= n or tokens[i] != "git":
        return False
    i += 1
    # Skip leading global options, consuming the argument of `-C` / `-c`.
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


def _matches_git_worktree_add(tool_name: str, command: str) -> bool:
    if tool_name.strip().lower() != "bash":
        return False
    return _runs_git_worktree_add(command)


@dataclass(frozen=True)
class WorktreeDenyRule:
    """One deny rule: a name + a predicate over `(tool_name, command)` — `""` for non-Bash."""

    name: str
    matches: Callable[[str, str], bool]


#: The deny table — checked in order; the first match wins.
WORKTREE_DENY_RULES: tuple[WorktreeDenyRule, ...] = (
    WorktreeDenyRule("EnterWorktree", _matches_enter_worktree),
    WorktreeDenyRule("git worktree add", _matches_git_worktree_add),
)


def decide_worktree(tool_name: str, command: str = "") -> Decision:
    """DENY iff the call matches :data:`WORKTREE_DENY_RULES`; role- and break-glass-independent."""
    for rule in WORKTREE_DENY_RULES:
        if rule.matches(tool_name, command):
            return Decision(permission=Permission.DENY, reason=WORKTREE_DENY_REASON)
    return Decision(permission=Permission.ALLOW)
