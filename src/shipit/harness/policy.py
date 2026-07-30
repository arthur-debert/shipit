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

#: Claude Code's shell tool. Split from codex's because the managed
#: `PreToolUse` matcher that must route them is per-harness.
_CLAUDE_SHELL_TOOLS = frozenset({"bash"})

#: codex's builtin shell tools (codex-cli 0.146.0).
_CODEX_SHELL_TOOLS = frozenset({"exec_command", "shell", "unified_exec"})

#: Tool names that EXECUTE a shell command, matched case-insensitively.
_SHELL_TOOLS = _CLAUDE_SHELL_TOOLS | _CODEX_SHELL_TOOLS

#: Tool names that SPAWN a subagent, matched case-insensitively.
_SPAWN_TOOLS = frozenset({"agent"})

#: Tool names that ENTER a native git worktree, matched case-insensitively.
_WORKTREE_TOOLS = frozenset({"enterworktree"})

#: Every Claude Code tool a deny rule can fire on. A managed `PreToolUse` matcher
#: must cover this whole set, or the rule behind the uncovered name is
#: unreachable — the defect #1182 exists to close.
CLAUDE_GUARDED_TOOLS = _CLAUDE_SHELL_TOOLS | _SPAWN_TOOLS | _WORKTREE_TOOLS

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
    #: ``None`` when the spawn omitted `isolation` or passed it blank — the
    #: absence the spawn rule fires on. Any non-blank value counts as isolated:
    #: the harness, not shipit, decides which isolation modes are valid.
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
    isolation = str(fields.get("isolation") or "").strip()
    return ToolCall(
        tool_name=str(payload.get("tool_name") or ""),
        command=command,
        cwd=str(payload.get("cwd") or ""),
        subagent_type=str(fields.get("subagent_type") or ""),
        isolation=isolation or None,
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

#: Shell punctuation shlex emits as its own token. Newlines are included so an
#: unquoted one cannot hide inside a word, and every one of these is stripped
#: from a word's edges — an escaped newline glues itself to the next word
#: (`git \<newline>worktree` lexes as `['git', '\nworktree']`).
_SHELL_PUNCTUATION = "();<>|&\n\r"

#: git global options taking a SEPARATE argument token, which consumes the next token.
_GIT_OPTS_WITH_ARG = frozenset({"-C", "-c"})


def _matches_enter_worktree(call: ToolCall) -> bool:
    return call.tool in _WORKTREE_TOOLS


def _shell_words(command: str) -> list[str] | None:
    """A command's words, punctuation-stripped and empties dropped; ``None`` if it does not lex."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=_SHELL_PUNCTUATION)
        lexer.whitespace_split = True
        # Newlines must reach the token stream as punctuation rather than being
        # silently discarded as whitespace. shlex still owns quoting, so a
        # newline INSIDE quotes stays part of its word.
        lexer.whitespace = lexer.whitespace.replace("\n", "").replace("\r", "")
        return [word for tok in lexer if (word := tok.strip(_SHELL_PUNCTUATION))]
    except ValueError:
        return None


def _git_args_reach_worktree_add(words: list[str], i: int) -> bool:
    """True iff `git`'s arguments from `i` begin `worktree add`, past any git global options."""
    n = len(words)
    while i < n and words[i].startswith("-"):
        option = words[i]
        i += 1
        if option in _GIT_OPTS_WITH_ARG and i < n:
            i += 1
    return i + 1 < n and words[i] == "worktree" and words[i + 1] == "add"


def _runs_git_worktree_add(command: str) -> bool:
    """True iff any word of `command` is a `git` whose arguments begin `worktree add`.

    Quoting supplies the discrimination: a mention (`echo "git worktree add"`)
    lexes to ONE word and can never match, while an invocation is adjacent
    words wherever it sits in the command. Position is therefore irrelevant, so
    no wrapper or keyword ahead of `git` — `sudo`, `env`, `time`, `then` — can
    defeat it, and that set never has to be enumerated.

    Best-effort by construction, and evadable: `eval`, variable indirection and
    `sh -c` all hide the words. It is a hygiene nudge for a cooperating agent,
    not a security boundary, so it errs toward denying (an unquoted
    `echo git worktree add` is refused).
    """
    words = _shell_words(command)
    if words is None:
        return _GIT_WORKTREE_ADD_FALLBACK.search(command) is not None
    return any(
        word == "git" and _git_args_reach_worktree_add(words, i + 1)
        for i, word in enumerate(words)
    )


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
