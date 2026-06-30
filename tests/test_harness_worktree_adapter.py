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


# --- resolve_epic: the #173 inference truth table -------------------------------


@pytest.mark.parametrize(
    "branch,expected",
    [
        ("TRE04/WS01", "TRE04"),  # ADR-0016 grammar → epic is the prefix
        ("TRE04/umbrella", "TRE04"),
        ("HAR02/agent-deadbeef", "HAR02"),  # nested slashes → only the first prefix
    ],
)
def test_resolve_epic_infers_prefix_from_branch(branch, expected):
    # No override → the epic is the spawning branch's prefix before the first '/'.
    assert wa.resolve_epic(None, branch) == expected


@pytest.mark.parametrize("branch", [None, "main", "HEAD", "", "   "])
def test_resolve_epic_no_inferable_prefix_is_none(branch):
    # Detached/unreadable (None) or a bare branch with no '/' → no epic; the caller
    # then lands on the epic-less fallback branch.
    assert wa.resolve_epic(None, branch) is None


def test_resolve_epic_override_wins_over_branch():
    # The explicit SHIPIT_EPIC override takes precedence over the inferred prefix
    # (the rare cross-epic spawn).
    assert wa.resolve_epic("HAR02", "TRE04/WS01") == "HAR02"


@pytest.mark.parametrize("override", [None, "", "   "])
def test_resolve_epic_blank_override_falls_through_to_branch(override):
    # An unset/blank override is no override — inference still applies.
    assert wa.resolve_epic(override, "TRE04/WS01") == "TRE04"


def test_resolve_epic_to_resolve_branch_end_to_end():
    # The composed path the boundary takes: infer from the cwd branch, then build
    # the holding branch.
    epic = wa.resolve_epic(None, "TRE04/WS01")
    assert wa.resolve_branch(epic, "abc123") == "TRE04/agent-abc123"
