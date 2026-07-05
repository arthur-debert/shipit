"""Unit tests for ``tree.provision`` — the provisioning-commit record's READ side.

ADR-0033 retired the record's WRITER outright (Tree provisioning no longer
commits anything, so there is no provisioning commit to record); only the
reader remains, for records that Trees born BEFORE the pin still carry on disk
(#232). Its contract: the READ degrades EVERY failure to the empty exclusion
set — nothing excluded means the gc floor keeps the Tree, the conservative
direction. A corrupt record must be able to narrow nothing and delete nothing.
Records here are hand-written JSON, exactly the on-disk legacy artifact.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shipit.identity import Sha
from shipit.tree import provision

SHA_A = Sha("a" * 40)
SHA_B = Sha("b" * 40)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A directory that looks like a clone (has a ``.git`` dir)."""
    clone = tmp_path / "ephemeral" / "sess-1"
    (clone / ".git").mkdir(parents=True)
    return clone


def _write_legacy_record(tree: Path, shas: list[Sha]) -> None:
    """Plant the pre-ADR-0033 record a drift-window provisioning once wrote."""
    provision.record_path(tree).write_text(
        json.dumps({"commits": [str(sha) for sha in shas]}), encoding="utf-8"
    )


def test_legacy_record_reads_back_as_the_exclusion_set(tree: Path):
    _write_legacy_record(tree, [SHA_A, SHA_B])
    assert provision.read_provision_shas(tree) == frozenset({SHA_A, SHA_B})


def test_record_lives_inside_git_dir_never_the_working_tree(tree: Path):
    # Inside `.git` deliberately: an untracked working-tree file would make every
    # session Tree permanently DIRTY — tripping the very floor the record refines.
    _write_legacy_record(tree, [SHA_A])
    assert provision.record_path(tree) == tree / ".git" / "shipit-provision.json"
    assert provision.record_path(tree).is_file()
    assert [p.name for p in tree.iterdir()] == [".git"]


def test_missing_record_is_the_empty_exclusion_set(tree: Path):
    # The universal steady state since ADR-0033 (provisioning never commits, so
    # nothing ever writes a record): nothing excluded, the floor keeps on any
    # local-only commit.
    assert provision.read_provision_shas(tree) == frozenset()


def test_the_writer_is_retired(tree: Path):
    # ADR-0033: Tree provisioning mutates nothing managed, so nothing may mint
    # NEW provision records — the module deliberately exposes no write path.
    assert not hasattr(provision, "write_record")


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
        {
            "commits": ["not-a-sha"]
        },  # not a full sha: an invalid IDENTITY excludes nothing
        {"commits": None},
        [str(SHA_A)],  # not even an object
    ],
    ids=[
        "no-key",
        "not-a-list",
        "non-str",
        "empty-str",
        "not-a-sha",
        "null",
        "not-an-object",
    ],
)
def test_mistyped_record_reads_as_empty(tree: Path, payload):
    provision.record_path(tree).write_text(json.dumps(payload), encoding="utf-8")
    assert provision.read_provision_shas(tree) == frozenset()
