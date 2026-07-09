"""Edit-enforcement decision: the role x is_code x break_glass security matrix.

The security-critical core (ADR-0012): a coordinator edit on a code path is
denied UNLESS break-glass is set; every other combination is allowed. Asserts
external behavior (the verdict + that a deny carries the redirect reason), never
internal call shapes.
"""

from __future__ import annotations

import pytest

from shipit.harness.policy import (
    COORDINATOR_DENY_REASON,
    WORKTREE_DENY_REASON,
    Decision,
    Permission,
    decide,
    decide_worktree,
    is_edit_tool,
)
from shipit.harness.role import Role


@pytest.mark.parametrize(
    ("role", "is_code", "break_glass", "expected"),
    [
        # The ONE intended deny: coordinator, code path, no break-glass.
        (Role.COORDINATOR, True, False, Permission.DENY),
        # Break-glass lets the coordinator through on a code path.
        (Role.COORDINATOR, True, True, Permission.ALLOW),
        # Coordinator on a non-code path (docs/config) is allowed.
        (Role.COORDINATOR, False, False, Permission.ALLOW),
        (Role.COORDINATOR, False, True, Permission.ALLOW),
        # Every non-coordinator role is allowed everywhere — code or not,
        # break-glass or not. The guard turns on the coordinator role alone.
        (Role.IMPLEMENTER, True, False, Permission.ALLOW),
        (Role.IMPLEMENTER, True, True, Permission.ALLOW),
        (Role.IMPLEMENTER, False, False, Permission.ALLOW),
        (Role.SHEPHERD, True, False, Permission.ALLOW),
        (Role.SHEPHERD, False, False, Permission.ALLOW),
        (Role.EXPLORER, True, False, Permission.ALLOW),
        (Role.EXPLORER, False, False, Permission.ALLOW),
    ],
)
def test_decide_matrix(role, is_code, break_glass, expected):
    assert decide(role, "any/path", is_code, break_glass).permission is expected


def test_coordinator_deny_carries_the_redirect_reason():
    decision = decide(Role.COORDINATOR, "src/shipit/cli.py", True, False)
    assert decision == Decision(Permission.DENY, COORDINATOR_DENY_REASON)
    assert "delegate" in decision.reason
    assert "origin/main" in decision.reason


def test_coordinator_deny_reason_is_the_generated_role_slice():
    """WS03: the deny reason is no longer a placeholder string — it carries the
    GENERATED coordinator role-prompt slice (base + coordinator overlay + the role
    map), so the deny wall and the injected coordinator prompt are the same text.
    Assert the overlay's marching orders AND the role-map marker are present."""
    reason = decide(Role.COORDINATOR, "src/shipit/cli.py", True, False).reason
    assert "You are the COORDINATOR" in reason  # the overlay scopes the role
    assert "never implement" in reason  # the coordinator's core rule
    assert "The roles you delegate to" in reason  # the role-map marker
    assert "implementer" in reason and "shepherd" in reason  # the map's contents


def test_allow_carries_no_reason():
    assert decide(Role.IMPLEMENTER, "src/shipit/cli.py", True, False).reason == ""
    assert decide(Role.COORDINATOR, "src/shipit/cli.py", True, True).reason == ""


@pytest.mark.parametrize(
    ("tool", "expected"),
    [
        ("Edit", True),
        ("Write", True),
        ("MultiEdit", True),
        ("NotebookEdit", True),
        ("apply_patch", True),
        ("functions.apply_patch", True),
        ("edit", True),  # case-insensitive — a casing drift can't disarm the guard.
        ("  Write  ", True),  # surrounding whitespace tolerated.
        ("Read", False),
        ("Bash", False),
        ("Grep", False),
        ("", False),
    ],
)
def test_is_edit_tool(tool, expected):
    assert is_edit_tool(tool) is expected


# --- native-worktree deny guard (TRE01 / ADR-0014) -------------------------


@pytest.mark.parametrize(
    ("tool_name", "command", "expected"),
    [
        # The two intended denies: the EnterWorktree tool, and a Bash command that
        # runs `git worktree add` (anywhere in the string, any internal spacing).
        ("EnterWorktree", "", Permission.DENY),
        ("enterworktree", "", Permission.DENY),  # case-insensitive on the tool name
        ("  EnterWorktree  ", "", Permission.DENY),  # surrounding whitespace tolerated
        ("Bash", "git worktree add ../tree-x my-branch", Permission.DENY),
        ("Bash", "git   worktree   add ../t b", Permission.DENY),  # extra whitespace
        ("Bash", "cd /repo && git worktree add ../t b", Permission.DENY),  # compound
        ("Bash", "git worktree add ../t b; ls", Permission.DENY),  # `;`-separated
        ("bash", "git worktree add ../t b", Permission.DENY),  # tool name case
        # Global git options between `git` and `worktree add` must NOT bypass the
        # wall — the structural matcher skips leading global options (incl. ones
        # that take a separate argument like `-C <path>` / `-c <name=value>`).
        ("Bash", "git -C /repo worktree add ../t b", Permission.DENY),
        ("Bash", "git --no-pager worktree add ../t b", Permission.DENY),
        ("Bash", "git -c core.hooksPath= worktree add ../t b", Permission.DENY),
        ("Bash", "FOO=bar git worktree add ../t b", Permission.DENY),  # env prefix
        # Ordinary git is unaffected — including the sibling worktree subcommands.
        ("Bash", "git status", Permission.ALLOW),
        ("Bash", "git checkout -b feature/x", Permission.ALLOW),
        ("Bash", "git fetch origin", Permission.ALLOW),
        ("Bash", "git pull --rebase", Permission.ALLOW),
        ("Bash", "git push -u origin HEAD", Permission.ALLOW),
        ("Bash", "git worktree list", Permission.ALLOW),
        ("Bash", "git worktree prune", Permission.ALLOW),
        # gh commands are unaffected.
        ("Bash", "gh pr create --draft", Permission.ALLOW),
        ("Bash", "gh pr ready 123", Permission.ALLOW),
        # The policy blocks CREATION, not every mention of the text: a command
        # that merely quotes / searches / prints the phrase is allowed, because
        # tokenizing makes the quoted phrase a single token, not the `git`
        # executable running the `worktree add` subcommand.
        ("Bash", 'rg "git worktree add"', Permission.ALLOW),
        ("Bash", "printf 'git worktree add'", Permission.ALLOW),
        ("Bash", "echo git worktree add", Permission.ALLOW),  # echo, not git
        # `worktree add` is only a deny under Bash — not on some other tool, and
        # not as a bare substring without the `git` verb.
        ("Read", "git worktree add ../t b", Permission.ALLOW),
        ("Bash", "echo worktree add", Permission.ALLOW),
        # Empty / unrelated calls allow.
        ("Bash", "", Permission.ALLOW),
        ("Edit", "", Permission.ALLOW),
    ],
)
def test_decide_worktree_matrix(tool_name, command, expected):
    assert decide_worktree(tool_name, command).permission is expected


def test_worktree_deny_carries_the_redirect_reason():
    for decision in (
        decide_worktree("EnterWorktree", ""),
        decide_worktree("Bash", "git worktree add ../t b"),
    ):
        assert decision == Decision(Permission.DENY, WORKTREE_DENY_REASON)
        # The wall redirects to the supported verb and cites the ADR.
        assert "shipit tree create" in decision.reason
        assert "ADR-0014" in decision.reason


def test_worktree_allow_carries_no_reason():
    assert decide_worktree("Bash", "git status").reason == ""
    assert decide_worktree("Bash", "git worktree list").reason == ""
