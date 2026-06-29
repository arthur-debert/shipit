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
