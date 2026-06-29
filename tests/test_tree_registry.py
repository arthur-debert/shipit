"""Unit tests for ``tree.registry.scan`` — deriving the fleet from on-disk clones.

These assert EXTERNAL behavior (PRD Testing Decisions): given a fixture directory
layout under a central root, ``scan`` returns the right :class:`TreeRecord`s
(branch, base, dirty, ahead/behind, PR label) and IGNORES any directory that is not
itself a git clone. The ``gh`` boundary (branch/dirty/ahead-behind/PR reads) is
patched so no real git/gh runs — the fixture is the disk layout, the patch is the
per-clone state.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shipit import gh
from shipit.tree import registry


def _make_clone(root: Path, rel: str) -> Path:
    """Create ``root/<rel>`` as a fake clone (a dir carrying a ``.git`` marker)."""
    path = root / rel
    (path / ".git").mkdir(parents=True)
    return path


def _make_plain_dir(root: Path, rel: str) -> Path:
    """Create ``root/<rel>`` as a NON-Tree directory (no ``.git`` marker)."""
    path = root / rel
    path.mkdir(parents=True)
    return path


@pytest.fixture
def fleet(tmp_path: Path, monkeypatch):
    """A central root with two Tree clones and a non-Tree dir, with the gh boundary
    patched to report per-clone state keyed by directory path."""
    root = tmp_path / "trees"
    a = _make_clone(root, "acme/widget/issues/123-aaaa")
    b = _make_clone(root, "acme/widget/issues/456-bbbb")
    # A stray directory that is NOT a clone — must be ignored by scan.
    _make_plain_dir(root, "acme/widget/issues/scratch-notatree")

    state = {
        str(a): {
            "branch": "fix/123-thing",
            "base": "origin/main",
            "dirty": " M file.py\n",
            "ahead_behind": (2, 0),
        },
        str(b): {
            "branch": "HAR02/WS02",
            "base": "origin/HAR02/umbrella",
            "dirty": "",
            "ahead_behind": (0, 3),
        },
    }
    pr_by_branch = {
        "fix/123-thing": {"number": 7, "state": "OPEN", "isDraft": True},
        "HAR02/WS02": {"number": 9, "state": "MERGED", "isDraft": False},
    }

    monkeypatch.setattr(gh, "git_current_branch", lambda *, cwd: state[cwd]["branch"])
    monkeypatch.setattr(gh, "git_upstream_ref", lambda *, cwd: state[cwd]["base"])
    monkeypatch.setattr(gh, "git_status_porcelain", lambda *, cwd: state[cwd]["dirty"])
    monkeypatch.setattr(
        gh, "git_ahead_behind", lambda *, cwd: state[cwd]["ahead_behind"]
    )
    monkeypatch.setattr(
        gh, "pr_for_head", lambda branch, *, cwd=None: pr_by_branch.get(branch)
    )
    return root, a, b


def test_scan_returns_one_record_per_clone_ignoring_non_trees(fleet):
    root, a, b = fleet
    records = registry.scan(root)

    # Exactly the two clones — the stray non-Tree dir is ignored.
    assert [r.path for r in records] == sorted([str(a), str(b)])


def test_scan_reads_branch_base_dirty_and_ahead_behind(fleet):
    root, a, b = fleet
    by_path = {r.path: r for r in registry.scan(root)}

    ra = by_path[str(a)]
    assert ra.branch == "fix/123-thing"
    assert ra.base == "origin/main"
    assert ra.dirty is True
    assert (ra.ahead, ra.behind) == (2, 0)

    rb = by_path[str(b)]
    assert rb.branch == "HAR02/WS02"
    assert rb.base == "origin/HAR02/umbrella"
    assert rb.dirty is False
    assert (rb.ahead, rb.behind) == (0, 3)


def test_scan_renders_pr_state_label_with_draft(fleet):
    root, a, b = fleet
    by_path = {r.path: r for r in registry.scan(root)}

    # Draft open PR reads as DRAFT; a merged PR shows its GitHub state verbatim.
    assert by_path[str(a)].pr == "#7 DRAFT"
    assert by_path[str(b)].pr == "#9 MERGED"


def test_scan_branch_without_pr_has_none(tmp_path: Path, monkeypatch):
    root = tmp_path / "trees"
    clone = _make_clone(root, "acme/widget/issues/1-zzzz")
    monkeypatch.setattr(gh, "git_current_branch", lambda *, cwd: "fix/1")
    monkeypatch.setattr(gh, "git_upstream_ref", lambda *, cwd: None)
    monkeypatch.setattr(gh, "git_status_porcelain", lambda *, cwd: "")
    monkeypatch.setattr(gh, "git_ahead_behind", lambda *, cwd: (0, 0))
    monkeypatch.setattr(gh, "pr_for_head", lambda branch, *, cwd=None: None)

    (record,) = registry.scan(root)
    assert record.path == str(clone)
    assert record.pr is None
    assert record.base is None
    assert (record.ahead, record.behind) == (0, 0)


def test_scan_unreadable_pr_renders_unknown_label(tmp_path: Path, monkeypatch):
    # An unreadable PR state (gh.pr_for_head -> UNKNOWN) renders as a bare "UNKNOWN"
    # label, distinct from the None a genuinely-PR-less Tree shows.
    root = tmp_path / "trees"
    clone = _make_clone(root, "acme/widget/issues/1-zzzz")
    monkeypatch.setattr(gh, "git_current_branch", lambda *, cwd: "fix/1")
    monkeypatch.setattr(gh, "git_upstream_ref", lambda *, cwd: "origin/main")
    monkeypatch.setattr(gh, "git_status_porcelain", lambda *, cwd: "")
    monkeypatch.setattr(gh, "git_ahead_behind", lambda *, cwd: (0, 0))
    monkeypatch.setattr(gh, "pr_for_head", lambda branch, *, cwd=None: gh.UNKNOWN)

    (record,) = registry.scan(root)
    assert record.path == str(clone)
    assert record.pr == "UNKNOWN"


def test_scan_missing_root_yields_empty(tmp_path: Path):
    assert registry.scan(tmp_path / "does-not-exist") == []


def test_scan_does_not_descend_into_a_clone(tmp_path: Path, monkeypatch):
    # A path that looks like a clone nested INSIDE another clone must not be
    # reported as a second Tree — scan stops at the first .git it meets.
    root = tmp_path / "trees"
    outer = _make_clone(root, "acme/widget/issues/1-aaaa")
    (outer / "vendor" / "dep" / ".git").mkdir(parents=True)

    monkeypatch.setattr(gh, "git_current_branch", lambda *, cwd: "fix/1")
    monkeypatch.setattr(gh, "git_upstream_ref", lambda *, cwd: "origin/main")
    monkeypatch.setattr(gh, "git_status_porcelain", lambda *, cwd: "")
    monkeypatch.setattr(gh, "git_ahead_behind", lambda *, cwd: (0, 0))
    monkeypatch.setattr(gh, "pr_for_head", lambda branch, *, cwd=None: None)

    records = registry.scan(root)
    assert [r.path for r in records] == [str(outer)]
