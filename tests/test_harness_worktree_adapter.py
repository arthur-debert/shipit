from __future__ import annotations

import pytest

from shipit.harness import worktree_adapter as wa


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("a5d633b0", "a5d633b0"),
        ("agent-a5d633b0", "a5d633b0"),
        ("AGENT-AbCd", "abcd"),
        ("feature/auth refactor", "feature-auth-refactor"),
        ("  spaced  ", "spaced"),
        ("--trim--", "trim"),
        ("", ""),
        ("agent-", ""),
        ("///", ""),
    ],
)
def test_normalize_agent_id(raw: str, expected: str):
    assert wa.normalize_agent_id(raw) == expected


def test_resolve_branch_with_epic_marker():
    assert wa.resolve_branch("TRE03", "a5d633b0") == "TRE03/agent-a5d633b0"


@pytest.mark.parametrize("epic", [None, "", "   "])
def test_resolve_branch_missing_marker_falls_back(epic):
    assert wa.resolve_branch(epic, "a5d633b0") == "agent-a5d633b0"


@pytest.mark.parametrize(
    "epic",
    [
        "bad/epic",
        "..",
        "epic with space",
        "TRE-03",
    ],
)
def test_resolve_branch_malformed_marker_falls_back(epic):
    assert wa.resolve_branch(epic, "a5d633b0") == "agent-a5d633b0"


def test_resolve_branch_accepts_verbatim_alphanumeric_epic():
    assert wa.resolve_branch("HAR02", "deadbeef") == "HAR02/agent-deadbeef"


@pytest.mark.parametrize(
    "branch,expected",
    [
        ("TRE04/WS01", "TRE04"),
        ("TRE04/umbrella", "TRE04"),
        ("HAR02/agent-deadbeef", "HAR02"),
    ],
)
def test_resolve_epic_infers_prefix_from_branch(branch, expected):
    assert wa.resolve_epic(None, branch) == expected


@pytest.mark.parametrize("branch", [None, "main", "HEAD", "", "   "])
def test_resolve_epic_no_inferable_prefix_is_none(branch):
    assert wa.resolve_epic(None, branch) is None


def test_resolve_epic_override_wins_over_branch():
    assert wa.resolve_epic("HAR02", "TRE04/WS01") == "HAR02"


@pytest.mark.parametrize("override", [None, "", "   "])
def test_resolve_epic_blank_override_falls_through_to_branch(override):
    assert wa.resolve_epic(override, "TRE04/WS01") == "TRE04"


def test_resolve_epic_to_resolve_branch_end_to_end():
    epic = wa.resolve_epic(None, "TRE04/WS01")
    assert wa.resolve_branch(epic, "abc123") == "TRE04/agent-abc123"


@pytest.mark.parametrize(
    "override",
    [
        "bad/epic",
        "A/B/C",
        "epic with space",
        "..",
        "TRE-03",
    ],
)
def test_resolve_epic_malformed_override_degrades_to_epicless_not_cwd(override):
    epic = wa.resolve_epic(override, "TRE04/WS01")
    assert wa.resolve_branch(epic, "abc123") == "agent-abc123"


def test_coordinator_launch_payload_has_no_prompt_id():
    payload = {
        "session_id": "c6010bf9",
        "transcript_path": "/t/c6010bf9.jsonl",
        "cwd": "/repo",
        "hook_event_name": "WorktreeCreate",
        "name": "sess-20260702-121314-4242",
    }
    assert wa.is_coordinator_launch(payload) is True


def test_helper_spawn_payload_carries_prompt_id():
    payload = {
        "session_id": "571d0dfe",
        "transcript_path": "/t/571d0dfe.jsonl",
        "cwd": "/repo",
        "prompt_id": "c2f52d57-6eb7-469b-b8ef-3001e450ecaf",
        "hook_event_name": "WorktreeCreate",
        "name": "agent-ac36b2efb04c97d80",
    }
    assert wa.is_coordinator_launch(payload) is False


@pytest.mark.parametrize("prompt_id", [None, ""])
def test_empty_prompt_id_counts_as_absent(prompt_id):
    assert wa.is_coordinator_launch({"name": "x", "prompt_id": prompt_id}) is True


def test_discriminator_is_the_field_not_the_name_prefix():
    assert wa.is_coordinator_launch({"name": "agent-foo"}) is True
    assert wa.is_coordinator_launch({"name": "sess-foo", "prompt_id": "p1"}) is False
