"""``harness/policy`` — the edit-, native-worktree-, cross-Tree-write and spawn-isolation deny verdicts.

See docs/adr/0012-enforcement-via-native-hooks.md.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..session import current as session_current
from ..tree import layout
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
    #: ``None`` is the absence the spawn rule fires on, and covers three payload
    #: shapes: `isolation` omitted, present but blank, and present but not a
    #: string. Only a non-blank STRING counts as isolated — which isolation modes
    #: are valid is the harness's business, so the value itself is not checked.
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
    # A non-`str` isolation reads as ABSENT, never as a value: coercing one would
    # turn any truthy junk (`{"a": 1}`, `[1]`, `1`) into a non-blank string and
    # so into "isolated", which is the one direction that must not fail open.
    raw_isolation = fields.get("isolation")
    isolation = raw_isolation.strip() if isinstance(raw_isolation, str) else ""
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

#: Shell punctuation shlex emits as its own token, so a separator cannot hide
#: inside a word (`git worktree add;` must still yield an `add` word).
_SHELL_PUNCTUATION = "();<>|&"

#: Stripped from a word's EDGES to reconcile shlex with the shell: shlex keeps an
#: escaped newline as a literal character where the shell splices it out. An
#: INTERIOR one is left alone, which matches the shell too — `git \<newline>worktree`
#: runs `git worktree`, but `git\<newline>worktree` (no space) runs `gitworktree`,
#: which is not git at all.
_CONTINUATION_CHARS = "\n\r"


def _matches_enter_worktree(call: ToolCall) -> bool:
    return call.tool in _WORKTREE_TOOLS


def _shell_words(command: str) -> list[str] | None:
    """A command's words, shell-like: shlex POSIX splitting with CR/LF stripped from word EDGES; ``None`` if it does not lex.

    The edge strip reproduces the shell's line-continuation word-join, but it is
    NOT what the shell does to a quoted value: shlex reports no provenance, so a
    quoted `"\\n"` is indistinguishable from a syntactic continuation and is
    stripped too, and a word that strips to empty is dropped rather than kept as
    an empty argument. Words whose CR/LF is INTERIOR keep it, so a quoted mention
    stays one word. docs/adr/0080 states the accepted false positives; #1189 owns
    the limitation.
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=_SHELL_PUNCTUATION)
        lexer.whitespace_split = True
        return [word for tok in lexer if (word := tok.strip(_CONTINUATION_CHARS))]
    except ValueError:
        return None


def _runs_git_worktree_add(command: str) -> bool:
    """True iff `command` has adjacent `worktree` `add` words with a `git` word before them.

    Quoting supplies the discrimination: a mention (`echo "git worktree add"`)
    lexes to ONE word and can never match, while an invocation is adjacent
    words wherever it sits. Nothing is counted or skipped — no wrapper, keyword
    or option ahead of the pair can misalign it, and none of those sets ever has
    to be enumerated. The `git` word is required so `grep worktree add file`
    does not match.

    Best-effort by construction, and evadable: `eval`, variable indirection and
    `sh -c` all hide the words. It is a hygiene nudge for a cooperating agent,
    not a security boundary, so it errs toward denying (an unquoted
    `echo git worktree add` is refused).
    """
    words = _shell_words(command)
    if words is None:
        return _GIT_WORKTREE_ADD_FALLBACK.search(command) is not None
    return any(
        word == "worktree"
        and i + 1 < len(words)
        and words[i + 1] == "add"
        and "git" in words[:i]
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


CROSS_TREE_WRITE_DENY_REASON = (
    "This command runs a write-capable command against {foreign}, which is not "
    "this Run's Tree ({own}). A Tree has ONE writer: two Runs writing one "
    "checkout stomp each other, and a leaked write lands on the other Run's "
    "branch as its own work (#1179, ADR-0014). Write only inside your own Tree "
    "and move changes between Runs through the remote (push, then `gh pr`); "
    "reading another Tree in place is fine, this refusal is about writing. If "
    "you meant to write there, you are in the wrong Run — hand back instead. "
    "One cause worth knowing: your PATH and PIXI_*/CONDA_* point at the "
    "SPAWNING session's Tree, so tool output can print its path as if it were "
    "your repo root; `git rev-parse --show-toplevel` is the one that tells the "
    "truth. This check reads the command as TEXT: a path assembled at runtime, "
    "or a writer it does not know, walks straight past it — it is a hygiene "
    "nudge, not a boundary."
)

#: Non-git commands that can WRITE a file. `git`, `rm` and `chmod` are absent on
#: purpose — reclaiming a Tree and making one read-only are legitimate cross-Tree
#: traffic; docs/adr/0083 states each omission before you add one back.
_WRITE_COMMANDS = (
    "cp",
    "dd",
    "mv",
    "patch",
    "perl",
    "python",
    "python3",
    "rsync",
    "sed",
    "tee",
    "truncate",
)

#: A write command as a WORD: `/usr/bin/sed` counts, since a leading path is part
#: of the invocation, while `foo-cp` and `mycp` do not. A DOTTED SUFFIX is not
#: examined, so `python3.13` counts (an interpreter) and so do `sed.orig` and
#: `sed.py` (not invocations) — over-denials accepted rather than discriminated,
#: because this rule is deny-biased and advisory; docs/adr/0083 tables them.
_RUNS_A_WRITE_COMMAND = re.compile(
    r"(?<![\w.:-])(?:" + "|".join(_WRITE_COMMANDS) + r")(?![\w-])"
)

#: A `>`/`>>` whose target is the text that follows it, matched against the
#: command's head so that `2>/dev/null …` cannot count as writing a later path.
#: The quoted branches leave the quote OPEN: a redirect target may contain
#: spaces, and `SHIPIT_TREES_ROOT` is allowed to (`> "/trees root/<leaf>/x"`).
_REDIRECTS_INTO_TAIL = re.compile(r""">>?\s*(?:"[^"]*|'[^']*|(?:\\.|[^\s'";|&<>])*)$""")


def _own_tree(cwd: str) -> layout.FlatLeaf | None:
    """The caller's own Tree, from the payload ``cwd``; ``None`` when it names no Tree.

    Never raises, and never falls back to the process cwd — the hook runs in the
    SPAWNING session's checkout, so that fallback would name the wrong Tree.
    """
    if not cwd:
        return None
    try:
        tree = session_current.containing_tree(Path(cwd))
    except (OSError, ValueError):
        return None
    return None if tree is None else layout.parse_flat_leaf(tree.name)


def _written_foreign_tree(command: str, own: layout.FlatLeaf) -> str | None:
    """The first Tree other than ``own`` this command writes into, else ``None``."""
    writes = _RUNS_A_WRITE_COMMAND.search(command) is not None
    for mention in layout.find_flat_leaves(command):
        if mention.leaf.tree_id.lower() == own.tree_id.lower():
            continue
        if writes or _REDIRECTS_INTO_TAIL.search(command[: mention.start]):
            return mention.leaf.name
    return None


def decide_cross_tree_write(call: ToolCall) -> Decision:
    """DENY iff a shell command names a Tree other than the caller's own AND runs a write-capable non-git command or redirects into it.

    ALLOWs whenever the caller's own Tree is unknown, so a call from outside the
    Trees root — where "another Tree" has no meaning — is never refused. Text
    matching: docs/adr/0083 lists the accepted false positives and the inputs
    that walk past it.
    """
    if call.tool not in _SHELL_TOOLS or not call.command:
        return _ALLOW
    own = _own_tree(call.cwd)
    if own is None:
        return _ALLOW
    foreign = _written_foreign_tree(call.command, own)
    if foreign is None:
        return _ALLOW
    return Decision(
        permission=Permission.DENY,
        reason=CROSS_TREE_WRITE_DENY_REASON.format(foreign=foreign, own=own.name),
    )
