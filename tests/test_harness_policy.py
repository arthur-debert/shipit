"""Edit-enforcement decision: role x tool x path -> allow/deny verdict.

The security-critical matrix (ADR-0012): a coordinator `edit` on a code path is
denied; everything else allowed. Asserts external behavior (the verdict + that a
deny carries a reason), never internal call shapes.
"""

from __future__ import annotations

import pytest
from shipit.harness.policy import (
    COORDINATOR_DENY_REASON,
    Decision,
    Permission,
    decide,
    is_code_path,
)
from shipit.harness.role import Role


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("src/shipit/cli.py", True),
        ("/Users/x/h/shipit/src/shipit/harness/policy.py", True),
        ("tests/test_harness_policy.py", False),
        ("docs/prd/har01.md", False),
        ("docs/adr/0012-enforcement.lex", False),
        (".shipit.toml", False),
        ("README.md", False),
        ("", False),
    ],
)
def test_is_code_path(path, expected):
    assert is_code_path(path) is expected


@pytest.mark.parametrize(
    ("role", "tool", "path", "expected"),
    [
        # The one intended deny: coordinator edits code.
        (Role.COORDINATOR, "Edit", "src/shipit/cli.py", Permission.DENY),
        (Role.COORDINATOR, "Write", "src/shipit/cli.py", Permission.DENY),
        (Role.COORDINATOR, "MultiEdit", "src/shipit/cli.py", Permission.DENY),
        (Role.COORDINATOR, "edit", "src/shipit/cli.py", Permission.DENY),
        # A subagent editing the same code path is allowed.
        (Role.IMPLEMENTER, "Edit", "src/shipit/cli.py", Permission.ALLOW),
        (Role.SHEPHERD, "Edit", "src/shipit/cli.py", Permission.ALLOW),
        (Role.EXPLORER, "Edit", "src/shipit/cli.py", Permission.ALLOW),
        # A coordinator editing a non-code path is allowed (planning/authoring).
        (Role.COORDINATOR, "Edit", "docs/prd/har01.md", Permission.ALLOW),
        (Role.COORDINATOR, "Write", ".shipit.toml", Permission.ALLOW),
        # A coordinator using a non-edit tool is allowed.
        (Role.COORDINATOR, "Read", "src/shipit/cli.py", Permission.ALLOW),
        (Role.COORDINATOR, "Bash", "src/shipit/cli.py", Permission.ALLOW),
    ],
)
def test_decide(role, tool, path, expected):
    assert decide(role, tool, path).permission is expected


def test_coordinator_deny_carries_the_redirect_reason():
    decision = decide(Role.COORDINATOR, "Edit", "src/shipit/cli.py")
    assert decision == Decision(Permission.DENY, COORDINATOR_DENY_REASON)
    assert "delegate" in decision.reason
    assert "origin/main" in decision.reason


def test_allow_carries_no_reason():
    assert decide(Role.IMPLEMENTER, "Edit", "src/shipit/cli.py").reason == ""
