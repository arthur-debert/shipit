"""Unit tests for ``harness.worktree_adapter`` — the demoted WorktreeCreate
adapter's PURE branch resolution (ADR-0017).

Pins the load-bearing truth table: the session-stable epic marker yields
``<epic>/agent-<id>``; a missing OR malformed marker falls back safely to an
epic-less ``agent-<id>`` (so the spawn still lands in a real Tree); and a raw agent
id is normalized into one safe ``agent-<id>`` ref component.
"""

from __future__ import annotations

import pytest

from shipit.harness import worktree_adapter as wa


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("a5d633b0", "a5d633b0"),  # already clean
        ("agent-a5d633b0", "a5d633b0"),  # native `agent-` stem stripped (not doubled)
        ("AGENT-AbCd", "abcd"),  # case-insensitive stem + lowercased
        ("feature/auth refactor", "feature-auth-refactor"),  # separators collapse
        ("  spaced  ", "spaced"),
        ("--trim--", "trim"),
        ("", ""),  # nothing usable
        ("agent-", ""),  # bare stem normalizes to empty
        ("///", ""),  # all separators
    ],
)
def test_normalize_agent_id(raw: str, expected: str):
    assert wa.normalize_agent_id(raw) == expected


def test_resolve_branch_with_epic_marker():
    # The session-stable epic marker namespaces the holding branch.
    assert wa.resolve_branch("TRE03", "a5d633b0") == "TRE03/agent-a5d633b0"


@pytest.mark.parametrize("epic", [None, "", "   "])
def test_resolve_branch_missing_marker_falls_back(epic):
    # No marker → a safe, epic-less holding branch (still a real Tree, no worktree).
    assert wa.resolve_branch(epic, "a5d633b0") == "agent-a5d633b0"


@pytest.mark.parametrize(
    "epic",
    [
        "bad/epic",  # a ref separator would mangle the branch
        "..",  # path traversal
        "epic with space",
        "TRE-03",  # hyphen is not part of the alphanumeric token grammar
    ],
)
def test_resolve_branch_malformed_marker_falls_back(epic):
    # A garbage marker must NOT produce a broken branch — it falls back safely.
    assert wa.resolve_branch(epic, "a5d633b0") == "agent-a5d633b0"


def test_resolve_branch_accepts_verbatim_alphanumeric_epic():
    # The epic code is kept verbatim (naming.lex §3 THEME+NN), not lowercased.
    assert wa.resolve_branch("HAR02", "deadbeef") == "HAR02/agent-deadbeef"
