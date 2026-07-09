"""Role resolver: fixture hook payload -> resolved Role.

The empty/absent-`agent_type`⇒`coordinator` case is the load-bearing one (the
human-facing session is always governed); a named subagent resolves to its role.
"""

from __future__ import annotations

import pytest

from shipit.harness.role import Role, resolve_role


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        # The load-bearing case: empty / absent agent_type is the coordinator.
        ({}, Role.COORDINATOR),
        ({"agent_type": ""}, Role.COORDINATOR),
        ({"agent_type": None}, Role.COORDINATOR),
        ({"agent_type": "   "}, Role.COORDINATOR),
        # Named subagents resolve to their own role (case-insensitive).
        ({"agent_type": "implementer"}, Role.IMPLEMENTER),
        ({"agent_type": "Implementer"}, Role.IMPLEMENTER),
        ({"agent_type": "shepherd"}, Role.SHEPHERD),
        ({"agent_type": "explorer"}, Role.EXPLORER),
        ({"agent_type": "reviewer"}, Role.REVIEWER),
        ({"agent_type": "coordinator"}, Role.COORDINATOR),
        # An unrecognized named subagent is NOT the coordinator.
        ({"agent_type": "general-purpose"}, Role.IMPLEMENTER),
    ],
)
def test_resolve_role(payload, expected):
    assert resolve_role(payload) is expected


def test_resolve_role_uses_fallback_role_only_when_agent_type_is_absent():
    assert (
        resolve_role({"agent_type": ""}, fallback_role="implementer")
        is Role.IMPLEMENTER
    )
    assert (
        resolve_role({"agent_type": "reviewer"}, fallback_role="implementer")
        is Role.REVIEWER
    )
    assert resolve_role({"agent_type": ""}, fallback_role=None) is Role.COORDINATOR


def test_unrecognized_is_never_coordinator():
    """The fail-open property: only an empty agent_type yields coordinator."""
    assert resolve_role({"agent_type": "something-new"}) is not Role.COORDINATOR
