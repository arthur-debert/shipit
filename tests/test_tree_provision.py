"""Unit tests for ``tree.provision`` — the provisioning-commit record (#232).

The record is the identity the ephemeral gc floor excludes, so its I/O contract is
asymmetric by design: the WRITE fails loud (the caller degrades it to
not-recorded), while the READ degrades EVERY failure to the empty exclusion set —
nothing excluded means the floor keeps the Tree, the conservative direction. A
corrupt record must be able to narrow nothing and delete nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shipit.tree import provision

SHA_A = "a" * 40
SHA_B = "b" * 40


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A directory that looks like a clone (has a ``.git`` dir)."""
    clone = tmp_path / "ephemeral" / "sess-1"
    (clone / ".git").mkdir(parents=True)
    return clone


def test_record_round_trips(tree: Path):
    provision.write_record(tree, [SHA_A, SHA_B])
    assert provision.read_provision_shas(tree) == frozenset({SHA_A, SHA_B})


def test_record_lives_inside_git_dir_never_the_working_tree(tree: Path):
    # Inside `.git` deliberately: an untracked working-tree file would make every
    # session Tree permanently DIRTY — tripping the very floor the record refines.
    provision.write_record(tree, [SHA_A])
    assert provision.record_path(tree) == tree / ".git" / "shipit-provision.json"
    assert provision.record_path(tree).is_file()
    assert [p.name for p in tree.iterdir()] == [".git"]


def test_write_refuses_a_non_clone(tmp_path: Path):
    # No `.git` dir -> nowhere safe to record; the caller treats the record as
    # additive and swallows this into "not recorded" (which reads as KEEP).
    with pytest.raises(OSError, match="not a git clone"):
        provision.write_record(tmp_path / "plain", [SHA_A])


def test_missing_record_is_the_empty_exclusion_set(tree: Path):
    # The steady-state norm: provisioning was a no-op, wrote nothing — nothing
    # excluded, the floor keeps on any local-only commit.
    assert provision.read_provision_shas(tree) == frozenset()


def test_malformed_json_reads_as_empty(tree: Path):
    provision.record_path(tree).write_text("{not json", encoding="utf-8")
    assert provision.read_provision_shas(tree) == frozenset()


@pytest.mark.parametrize(
    "payload",
    [
        {},  # missing key
        {"commits": "not-a-list"},
        {"commits": [123]},
        {"commits": [""]},  # an empty sha could never match; treat as mis-typed
        {"commits": None},
        [SHA_A],  # not even an object
    ],
    ids=["no-key", "not-a-list", "non-str", "empty-str", "null", "not-an-object"],
)
def test_mistyped_record_reads_as_empty(tree: Path, payload):
    provision.record_path(tree).write_text(json.dumps(payload), encoding="utf-8")
    assert provision.read_provision_shas(tree) == frozenset()
