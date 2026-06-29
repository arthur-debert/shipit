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

import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from .prompts import load_coordinator_slice
from .role import Role

#: The coordinator deny reason — the role-prompt slice that teaches the next
#: action. WS03 repoints this seam at the GENERATED coordinator role-prompt slice
#: (base + coordinator overlay + role map — the exact text injected as the
#: coordinator's context), loaded ONCE from the committed bundled file at import,
#: so the deny wall and the injected prompt are byte-identical and can never
#: disagree. Loaded at import (not inside `decide()`) so the verdict stays pure —
#: `decide()` only references this constant. The slice is regenerated from the
#: lex fragments by `pixi run regen-roles` (shipit.harness.prompts).
COORDINATOR_DENY_REASON = load_coordinator_slice()

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


# --- native-worktree deny guard (TRE01 / ADR-0014) -------------------------
#
# A second, role-independent deny surface on the SAME stable PreToolUse channel:
# block the two ways an agent creates a NATIVE git worktree, because a Tree (the
# isolated checkout where a write-session works) is a *dissociated clone*, not a
# `git worktree` (ADR-0014). The deny reason redirects to `shipit tree create`.
#
# This is **deny, not redirect**: it does NOT couple to Claude Code's
# `WorktreeCreate`/undocumented hook — it rides the PreToolUse `deny` surface the
# policy module already owns. The verdict is a pure function of the tool name +
# (for Bash) the command string; the boundary reads those off the payload.

#: The native-worktree deny reason — redirects to `shipit tree create` and cites
#: ADR-0014 so the wall teaches WHY worktrees are refused, not just THAT they are.
WORKTREE_DENY_REASON = (
    "Trees are dissociated clones, not git worktrees (ADR-0014). Do not create a "
    "native git worktree — run `shipit tree create` to get an isolated checkout. "
    "(A Tree is a full `git clone --reference --dissociate` in the central Trees "
    "root, so it can sit on any branch — including a branch another Tree holds — "
    "and `rm -rf` is a safe delete; a worktree can do neither.)"
)

#: Conservative fallback: match `git worktree add` as a raw substring. Used ONLY
#: when a command can't be tokenized (unbalanced quotes), so a MALFORMED
#: worktree-add still fails closed; the structural matcher below is the primary
#: path. The trailing `\b` keeps it from over-matching `…addcondition`.
_GIT_WORKTREE_ADD_FALLBACK = re.compile(r"\bgit\s+worktree\s+add\b")

#: Shell metacharacters that separate one simple command from the next. With
#: `punctuation_chars=True`, shlex emits runs of these as standalone tokens, so a
#: compound (`cd x && git worktree add …`) splits into segments we inspect
#: independently — a `git worktree add` in ANY segment denies.
_SHELL_SEPARATOR_CHARS = frozenset("();<>|&")

#: git GLOBAL options that take a SEPARATE argument token (`git -C <path> …`,
#: `git -c <name=value> …`). When skipping leading global options to reach the
#: subcommand, these consume the following token too. Options that inline their
#: value with `=` (`--git-dir=…`, `--work-tree=…`) are a single `-`-prefixed token
#: and need no special-casing.
_GIT_OPTS_WITH_ARG = frozenset({"-C", "-c"})


def _matches_enter_worktree(tool_name: str, command: str) -> bool:
    """True iff the call is the `EnterWorktree` tool (case/whitespace tolerant)."""
    return tool_name.strip().lower() == "enterworktree"


def _segment_runs_worktree_add(tokens: list[str]) -> bool:
    """True iff a single simple command (its tokens) runs `git worktree add`.

    Skips leading `VAR=value` env assignments, requires the executable to be
    EXACTLY `git` (a tokenized word — so `mygit` and a quoted `"git worktree add"`
    mention can never reach here), skips any leading git GLOBAL options (so
    `git -C /repo …` and `git --no-pager …` still match), then requires the
    `worktree add` subcommand.
    """
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
    """True iff a Bash `command` actually EXECUTES `git worktree add`.

    Tokenizes structurally (ADR-0014 enforcement) so the wall blocks CREATION,
    not every mention of the text:
      - a quoted phrase is ONE token, never three — `rg "git worktree add"` and
        `printf 'git worktree add'` ALLOW;
      - leading git global options don't hide the subcommand — `git -C /repo
        worktree add …` and `git --no-pager worktree add …` DENY;
      - each simple command in a compound is inspected on its own, so
        `cd x && git worktree add …` DENY while `git worktree list` ALLOWs.

    On un-lexable input (unbalanced quotes) it falls back to a conservative
    substring match, so a malformed worktree-add still fails closed.
    """
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
    """True iff this is a Bash call whose command runs `git worktree add`."""
    if tool_name.strip().lower() != "bash":
        return False
    return _runs_git_worktree_add(command)


@dataclass(frozen=True)
class WorktreeDenyRule:
    """One native-worktree deny rule: a name + a predicate over the call.

    The predicate takes `(tool_name, command)` — `command` is the Bash command
    string (`""` for non-Bash tools) — and returns True when the call is a native
    worktree creation that must be denied.
    """

    name: str
    matches: Callable[[str, str], bool]


#: The deny table — checked in order; the first match wins. Both rules carry the
#: same redirect reason. Append a rule here to cover a new worktree-creating path.
WORKTREE_DENY_RULES: tuple[WorktreeDenyRule, ...] = (
    WorktreeDenyRule("EnterWorktree", _matches_enter_worktree),
    WorktreeDenyRule("git worktree add", _matches_git_worktree_add),
)


def decide_worktree(tool_name: str, command: str = "") -> Decision:
    """Decide a PreToolUse call against the native-worktree deny table. Pure.

    DENY (with the `shipit tree create` redirect) iff `(tool_name, command)`
    matches a rule in :data:`WORKTREE_DENY_RULES`; every other call — ordinary git
    (`status`, `checkout`, `fetch`, `pull`, `push`), `git worktree list/prune`, and
    all `gh` commands — ALLOWs. Independent of role and break-glass.
    """
    for rule in WORKTREE_DENY_RULES:
        if rule.matches(tool_name, command):
            return Decision(permission=Permission.DENY, reason=WORKTREE_DENY_REASON)
    return Decision(permission=Permission.ALLOW)
