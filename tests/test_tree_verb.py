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
