"""Unit tests for ``harness.worktree_adapter`` — the WorktreeCreate adapter's PURE
resolution (ADR-0017, elevated by ADR-0027).

Pins the load-bearing truth tables: the coordinator-vs-helper discriminator
(``prompt_id`` absent ⇒ coordinator launch — the SES02-WS01 spike); the
session-stable epic marker yields ``<epic>/agent-<id>``; a missing OR malformed
marker falls back safely to an epic-less ``agent-<id>`` (so the spawn still lands
in a real Tree); and a raw agent id is normalized into one safe ``agent-<id>`` ref
component.
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


@pytest.mark.parametrize(
    "override",
    [
        "bad/epic",  # a ref separator — would mangle the branch
        "A/B/C",  # nested separators
        "epic with space",  # whitespace inside the token
        "..",  # path traversal
        "TRE-03",  # hyphen is outside the alphanumeric token grammar
    ],
)
def test_resolve_epic_malformed_override_degrades_to_epicless_not_cwd(override):
    # PRECEDENCE CONTRACT (#173): an explicit-but-MALFORMED override is a user error
    # that must degrade to the epic-less `agent-<id>` fallback — it must NOT silently
    # fall through to cwd-branch inference. `resolve_epic` returns the non-empty
    # override verbatim (never reaching the branch-prefix path, so the live
    # `TRE04/WS01` branch is ignored), and the composed `resolve_branch` then rejects
    # the malformed token. The load-bearing assertion is the negative one: the result
    # is `agent-abc123`, NOT `TRE04/agent-abc123`.
    epic = wa.resolve_epic(override, "TRE04/WS01")
    assert wa.resolve_branch(epic, "abc123") == "agent-abc123"


# --- is_coordinator_launch: the ADR-0027 discriminator --------------------------
#
# The SES02-WS01 spike (docs/dev/ses02-worktreecreate-discriminator-spike.md, CC
# 2.1.198): a top-level `claude --worktree` launch fires the hook WITHOUT a
# `prompt_id` (no prompt exists at process startup); an in-CC
# Agent(isolation:"worktree") spawn always carries one.


def test_coordinator_launch_payload_has_no_prompt_id():
    # The verbatim field set the spike captured for a top-level --worktree launch.
    payload = {
        "session_id": "c6010bf9",
        "transcript_path": "/t/c6010bf9.jsonl",
        "cwd": "/repo",
        "hook_event_name": "WorktreeCreate",
        "name": "sess-20260702-121314-4242",
    }
    assert wa.is_coordinator_launch(payload) is True


def test_helper_spawn_payload_carries_prompt_id():
    # The verbatim field set the spike captured for an in-CC worktree spawn.
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
    # A null/empty prompt_id is no prompt — classified as the coordinator launch.
    assert wa.is_coordinator_launch({"name": "x", "prompt_id": prompt_id}) is True


def test_discriminator_is_the_field_not_the_name_prefix():
    # The `agent-`/`sess-` name conventions are corroborating evidence only; the
    # payload FIELD decides. A pathological `-w agent-foo` launch (no prompt_id) is
    # still the coordinator; a helper spawn is a helper whatever its name says.
    assert wa.is_coordinator_launch({"name": "agent-foo"}) is True
    assert wa.is_coordinator_launch({"name": "sess-foo", "prompt_id": "p1"}) is False
