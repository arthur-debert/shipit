"""Unit tests for the ``shipit tree create`` verb handler (``run_create``).

The verb is thin glue: resolve repo identity at the gh boundary, hand a typed
:class:`TreeSpec` to the planner+orchestrator, and print READY. These tests mock
the ``gh``/``create`` boundary so they pin the glue — exit codes, the spec it
builds, and the error paths — without touching real git.
"""

from __future__ import annotations

import json

from shipit import gh
from shipit.tree.create import Tree
from shipit.tree.registry import TreeRecord
from shipit.verbs import tree as tree_verb


def test_run_create_happy_path(monkeypatch, capsys):
    monkeypatch.setattr(gh, "repo_root", lambda: "/repo")
    monkeypatch.setattr(gh, "current_repo", lambda: "acme/widget")
    monkeypatch.setattr(gh, "git_remote_url", lambda *, cwd: "git@example:acme/widget")

    captured: dict = {}

    def fake_create(spec, *, source_repo, github_url):
        captured["spec"] = spec
        captured["source_repo"] = source_repo
        captured["github_url"] = github_url
        return Tree(path="/repo/trees/x", branch="fix/7-thing", base="origin/main")

    monkeypatch.setattr(tree_verb, "create", fake_create)

    rc = tree_verb.run_create(issue=7, slug="Thing")

    assert rc == 0
    # The verb resolved identity into the spec it handed the orchestrator.
    assert captured["spec"].org == "acme"
    assert captured["spec"].repo == "widget"
    assert captured["spec"].issue == 7
    assert captured["spec"].slug == "Thing"
    assert captured["source_repo"] == "/repo"
    assert captured["github_url"] == "git@example:acme/widget"
    # READY summary is the orchestrator's result, as a READY line + JSON.
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "READY"
    payload = json.loads("\n".join(out.splitlines()[1:]))
    assert payload == {
        "path": "/repo/trees/x",
        "branch": "fix/7-thing",
        "base": "origin/main",
    }


def test_run_create_not_inside_checkout(monkeypatch, capsys):
    monkeypatch.setattr(gh, "repo_root", lambda: None)

    rc = tree_verb.run_create(issue=7)

    assert rc == 1
    assert "not inside a git checkout" in capsys.readouterr().err


def test_run_create_reports_gh_error_cleanly(monkeypatch, capsys):
    monkeypatch.setattr(gh, "repo_root", lambda: "/repo")

    def boom():
        raise gh.GhError("could not resolve repo")

    monkeypatch.setattr(gh, "current_repo", boom)

    rc = tree_verb.run_create(issue=7)

    assert rc == 1
    assert "tree create:" in capsys.readouterr().err


def _record(**over) -> TreeRecord:
    base = dict(
        path="/trees/acme/widget/issues/7-aaaa",
        branch="fix/7-thing",
        base="origin/main",
        dirty=False,
        ahead=0,
        behind=0,
        pr="#7 DRAFT",
        mtime=1000.0,
    )
    base.update(over)
    return TreeRecord(**base)


def test_run_list_renders_fleet_table(monkeypatch, capsys):
    records = [
        _record(),
        _record(
            path="/trees/acme/widget/epics/HAR02/WS02-bbbb",
            branch="HAR02/WS02",
            base="origin/HAR02/umbrella",
            dirty=True,
            ahead=2,
            behind=1,
            pr="#9 OPEN",
            mtime=500.0,
        ),
    ]
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: "/trees")
    monkeypatch.setattr(tree_verb.registry, "scan", lambda root: records)

    rc = tree_verb.run_list()

    assert rc == 0
    out = capsys.readouterr().out
    # Header + both Trees render, with branch, base, dirty state, and PR label.
    assert "BRANCH" in out and "BASE" in out and "PR" in out
    assert "fix/7-thing" in out
    assert "HAR02/WS02" in out
    assert "clean" in out and "dirty" in out
    assert "#7 DRAFT" in out and "#9 OPEN" in out
    # Divergence is annotated on the BASE cell.
    assert "origin/HAR02/umbrella (+2/-1)" in out


def test_run_list_empty_root_is_not_an_error(monkeypatch, capsys):
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: "/trees")
    monkeypatch.setattr(tree_verb.registry, "scan", lambda root: [])

    rc = tree_verb.run_list()

    assert rc == 0
    assert "No Trees" in capsys.readouterr().out


def test_run_list_over_a_fixture_root_renders(tmp_path, monkeypatch, capsys):
    # End to end: a real fixture central root + a real scan, only the gh boundary
    # patched. `shipit tree list` must render the clone without error.
    root = tmp_path / "trees"
    clone = root / "acme" / "widget" / "issues" / "7-aaaa"
    (clone / ".git").mkdir(parents=True)
    monkeypatch.setenv("SHIPIT_TREES_ROOT", str(root))
    monkeypatch.setattr(gh, "git_current_branch", lambda *, cwd: "fix/7-thing")
    monkeypatch.setattr(gh, "git_upstream_ref", lambda *, cwd: "origin/main")
    monkeypatch.setattr(gh, "git_status_porcelain", lambda *, cwd: "")
    monkeypatch.setattr(gh, "git_ahead_behind", lambda *, cwd: (0, 0))
    monkeypatch.setattr(gh, "pr_for_head", lambda branch, *, cwd=None: None)

    rc = tree_verb.run_list()

    assert rc == 0
    out = capsys.readouterr().out
    assert "fix/7-thing" in out
    assert str(clone) in out


def test_run_list_scans_the_central_root(monkeypatch, capsys):
    seen: dict = {}
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: "/central/trees")

    def fake_scan(root):
        seen["root"] = root
        return []

    monkeypatch.setattr(tree_verb.registry, "scan", fake_scan)

    tree_verb.run_list()

    assert seen["root"] == "/central/trees"


def test_run_create_maps_create_failure_to_exit_1(monkeypatch, capsys):
    monkeypatch.setattr(gh, "repo_root", lambda: "/repo")
    monkeypatch.setattr(gh, "current_repo", lambda: "acme/widget")
    monkeypatch.setattr(gh, "git_remote_url", lambda *, cwd: "git@example:acme/widget")

    def boom(spec, *, source_repo, github_url):
        raise gh.GhError("clone failed")

    monkeypatch.setattr(tree_verb, "create", boom)

    rc = tree_verb.run_create(issue=7)

    assert rc == 1
    assert "tree create:" in capsys.readouterr().err


# --- tree remove ---------------------------------------------------------------


def _make_tree_dir(root, rel: str):
    """Create ``root/<rel>`` as a fake Tree clone (a dir carrying a ``.git`` marker)."""
    path = root / rel
    (path / ".git").mkdir(parents=True)
    return path


def test_run_remove_deletes_exactly_one_tree(tmp_path, monkeypatch, capsys):
    root = tmp_path / "trees"
    target = _make_tree_dir(root, "acme/widget/issues/7-aaaa")
    other = _make_tree_dir(root, "acme/widget/issues/9-bbbb")
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))
    monkeypatch.setattr(
        tree_verb.registry,
        "scan",
        lambda r: [_record(path=str(target)), _record(path=str(other))],
    )

    rc = tree_verb.run_remove(str(target))

    assert rc == 0
    assert not target.exists()  # the matched Tree is gone
    assert other.exists()  # the sibling is untouched
    assert "REMOVED" in capsys.readouterr().out


def test_run_remove_matches_by_dir_name(tmp_path, monkeypatch, capsys):
    root = tmp_path / "trees"
    target = _make_tree_dir(root, "acme/widget/issues/7-aaaa")
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))
    monkeypatch.setattr(
        tree_verb.registry, "scan", lambda r: [_record(path=str(target))]
    )

    rc = tree_verb.run_remove("7-aaaa")  # short id, not the full path

    assert rc == 0
    assert not target.exists()


def test_run_remove_no_match_is_an_error(tmp_path, monkeypatch, capsys):
    root = tmp_path / "trees"
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))
    monkeypatch.setattr(tree_verb.registry, "scan", lambda r: [])

    rc = tree_verb.run_remove("does-not-exist")

    assert rc == 1
    assert "no Tree matching" in capsys.readouterr().err


def test_run_remove_ambiguous_match_refuses(tmp_path, monkeypatch, capsys):
    root = tmp_path / "trees"
    a = _make_tree_dir(root, "acme/widget/issues/7-aaaa")
    b = _make_tree_dir(root, "acme/gadget/issues/7-aaaa")  # same dir name, two repos
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))
    monkeypatch.setattr(
        tree_verb.registry,
        "scan",
        lambda r: [_record(path=str(a)), _record(path=str(b))],
    )

    rc = tree_verb.run_remove("7-aaaa")

    assert rc == 1
    assert "ambiguous" in capsys.readouterr().err
    assert a.exists() and b.exists()  # nothing deleted on an ambiguous match


def test_run_remove_reports_rmtree_failure_cleanly(tmp_path, monkeypatch, capsys):
    # A failed delete (read-only file, lock, vanished dir) must surface as a clean
    # exit-1 + stderr message, never an unhandled traceback that breaks the contract.
    target = _make_tree_dir(tmp_path / "trees", "acme/widget/issues/7-aaaa")
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        tree_verb.registry, "scan", lambda r: [_record(path=str(target))]
    )

    def boom(_path):
        raise OSError("permission denied")

    monkeypatch.setattr(tree_verb.shutil, "rmtree", boom)

    rc = tree_verb.run_remove(str(target))

    assert rc == 1
    assert "could not remove" in capsys.readouterr().err


# --- tree gc -------------------------------------------------------------------


def test_run_gc_removes_only_removable_lists_stale_keeps_rest(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "trees"
    # Four Trees: one removable (merged+aged), one stale (no PR+aged), one kept dirty,
    # one kept in-flight (open PR). gc must delete ONLY the removable one.
    removable = _make_tree_dir(root, "acme/widget/issues/1-merged")
    stale = _make_tree_dir(root, "acme/widget/issues/2-orphan")
    keep_dirty = _make_tree_dir(root, "acme/widget/issues/3-dirty")
    keep_open = _make_tree_dir(root, "acme/widget/issues/4-open")

    aged = 0.0  # mtime far in the past -> always aged vs time.time()
    records = [
        _record(path=str(removable), branch="b1", dirty=False, ahead=0, mtime=aged),
        _record(path=str(stale), branch="b2", dirty=False, ahead=0, mtime=aged),
        _record(path=str(keep_dirty), branch="b3", dirty=True, ahead=0, mtime=aged),
        _record(path=str(keep_open), branch="b4", dirty=False, ahead=0, mtime=aged),
    ]
    pr_by_branch = {
        "b1": {"number": 1, "state": "MERGED", "isDraft": False},
        "b2": None,
        "b3": {"number": 3, "state": "MERGED", "isDraft": False},
        "b4": {"number": 4, "state": "OPEN", "isDraft": False},
    }
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))
    monkeypatch.setattr(tree_verb.registry, "scan", lambda r: records)
    monkeypatch.setattr(
        gh, "pr_for_head", lambda branch, *, cwd=None: pr_by_branch.get(branch)
    )

    rc = tree_verb.run_gc()

    assert rc == 0
    assert not removable.exists()  # only the removable Tree is deleted
    assert stale.exists()  # ambiguous -> listed, never removed
    assert keep_dirty.exists()  # local work protected
    assert keep_open.exists()  # in-flight PR protected
    out = capsys.readouterr().out
    assert f"REMOVED {removable}" in out
    assert f"STALE   {stale}" in out
    assert "removed 1, stale 1, kept 2" in out


def test_run_gc_empty_root_is_not_an_error(tmp_path, monkeypatch, capsys):
    root = tmp_path / "trees"
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))
    monkeypatch.setattr(tree_verb.registry, "scan", lambda r: [])

    rc = tree_verb.run_gc()

    assert rc == 0
    assert "removed 0, stale 0, kept 0" in capsys.readouterr().out


def test_run_gc_continues_past_a_failed_delete(tmp_path, monkeypatch, capsys):
    # Two removable Trees; the first delete fails. The sweep must continue to the
    # second, report the failure on stderr, and count only the delete that landed.
    root = tmp_path / "trees"
    bad = _make_tree_dir(root, "acme/widget/issues/1-bad")
    good = _make_tree_dir(root, "acme/widget/issues/2-good")
    aged = 0.0
    records = [
        _record(path=str(bad), branch="b1", dirty=False, ahead=0, mtime=aged),
        _record(path=str(good), branch="b2", dirty=False, ahead=0, mtime=aged),
    ]
    monkeypatch.setattr(tree_verb.layout, "central_root", lambda: str(root))
    monkeypatch.setattr(tree_verb.registry, "scan", lambda r: records)
    monkeypatch.setattr(
        gh,
        "pr_for_head",
        lambda branch, *, cwd=None: {"number": 1, "state": "MERGED", "isDraft": False},
    )

    real_rmtree = tree_verb.shutil.rmtree

    def flaky(path, *args, **kwargs):
        if path == str(bad):
            raise OSError("read-only file")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(tree_verb.shutil, "rmtree", flaky)

    rc = tree_verb.run_gc()

    assert rc == 0
    assert bad.exists()  # the failed delete left it on disk
    assert not good.exists()  # the sweep continued and reclaimed the next one
    captured = capsys.readouterr()
    assert f"FAILED  {bad}" in captured.err
    assert "removed 1, stale 0, kept 0" in captured.out  # count reflects disk reality


def test_pr_state_normalizes_draft(monkeypatch):
    # A draft open PR reads as "DRAFT" (one fleet-wide vocabulary, mirroring the
    # registry label), not the raw "OPEN" GitHub state.
    monkeypatch.setattr(
        gh,
        "pr_for_head",
        lambda branch, *, cwd=None: {"number": 7, "state": "OPEN", "isDraft": True},
    )
    record = _record(path="/trees/x", branch="b1")

    assert tree_verb._pr_state(record) == "DRAFT"
