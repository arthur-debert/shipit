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
from shipit.harness.roleprofile import delegates_code_authorship


@pytest.mark.parametrize(
    ("role", "is_code", "break_glass", "expected"),
    [
        (Role.COORDINATOR, True, False, Permission.DENY),
        (Role.COORDINATOR, True, True, Permission.ALLOW),
        (Role.COORDINATOR, False, False, Permission.ALLOW),
        (Role.COORDINATOR, False, True, Permission.ALLOW),
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


def test_edit_guard_is_posture_derived_for_every_role():
    for role in Role:
        denied = (
            decide(role, "src/x.py", is_code=True, break_glass=False).permission
            is Permission.DENY
        )
        assert denied is delegates_code_authorship(role)


def test_only_the_coordinator_delegates_code_authorship_today():
    assert delegates_code_authorship(Role.COORDINATOR)
    for role in (Role.IMPLEMENTER, Role.SHEPHERD, Role.EXPLORER, Role.REVIEWER):
        assert not delegates_code_authorship(role)


def test_coordinator_deny_carries_the_redirect_reason():
    decision = decide(Role.COORDINATOR, "src/shipit/cli.py", True, False)
    assert decision == Decision(Permission.DENY, COORDINATOR_DENY_REASON)
    assert "delegate" in decision.reason
    assert "origin/main" in decision.reason


def test_coordinator_deny_reason_is_the_generated_role_slice():
    reason = decide(Role.COORDINATOR, "src/shipit/cli.py", True, False).reason
    assert "You are the COORDINATOR" in reason
    assert "never implement" in reason
    assert "The roles you delegate to" in reason
    assert "implementer" in reason and "shepherd" in reason


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
        ("edit", True),
        ("  Write  ", True),
        ("Read", False),
        ("Bash", False),
        ("Grep", False),
        ("", False),
    ],
)
def test_is_edit_tool(tool, expected):
    assert is_edit_tool(tool) is expected


@pytest.mark.parametrize(
    ("tool_name", "command", "expected"),
    [
        ("EnterWorktree", "", Permission.DENY),
        ("enterworktree", "", Permission.DENY),
        ("  EnterWorktree  ", "", Permission.DENY),
        ("Bash", "git worktree add ../tree-x my-branch", Permission.DENY),
        ("Bash", "git   worktree   add ../t b", Permission.DENY),
        ("Bash", "cd /repo && git worktree add ../t b", Permission.DENY),
        ("Bash", "git worktree add ../t b; ls", Permission.DENY),
        ("bash", "git worktree add ../t b", Permission.DENY),
        ("Bash", "git -C /repo worktree add ../t b", Permission.DENY),
        ("Bash", "git --no-pager worktree add ../t b", Permission.DENY),
        ("Bash", "git -c core.hooksPath= worktree add ../t b", Permission.DENY),
        ("Bash", "FOO=bar git worktree add ../t b", Permission.DENY),
        ("Bash", "git status", Permission.ALLOW),
        ("Bash", "git checkout -b feature/x", Permission.ALLOW),
        ("Bash", "git fetch origin", Permission.ALLOW),
        ("Bash", "git pull --rebase", Permission.ALLOW),
        ("Bash", "git push -u origin HEAD", Permission.ALLOW),
        ("Bash", "git worktree list", Permission.ALLOW),
        ("Bash", "git worktree prune", Permission.ALLOW),
        ("Bash", "gh pr create --draft", Permission.ALLOW),
        ("Bash", "gh pr ready 123", Permission.ALLOW),
        ("Bash", 'rg "git worktree add"', Permission.ALLOW),
        ("Bash", "printf 'git worktree add'", Permission.ALLOW),
        ("Bash", "echo git worktree add", Permission.ALLOW),
        ("Read", "git worktree add ../t b", Permission.ALLOW),
        ("Bash", "echo worktree add", Permission.ALLOW),
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
        assert "shipit tree create" in decision.reason
        assert "ADR-0014" in decision.reason


def test_worktree_allow_carries_no_reason():
    assert decide_worktree("Bash", "git status").reason == ""
    assert decide_worktree("Bash", "git worktree list").reason == ""
