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
    Decision,
    Permission,
    decide,
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
