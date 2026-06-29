"""Unit tests for the ``gh`` Tree-registry read helpers.

These pin the parsing/mapping the registry relies on — the ahead/behind left-right
order, upstream-absent → ``None`` / ``(0, 0)``, and the PR-snapshot shape — by
patching only the subprocess seam (``_run`` / ``_git``), never the network.
"""

from __future__ import annotations

import json

from shipit import gh


def test_git_ahead_behind_maps_left_right_to_behind_ahead(monkeypatch):
    # `rev-list --left-right --count @{upstream}...HEAD` prints "<behind> <ahead>".
    monkeypatch.setattr(gh, "_git", lambda args, *, cwd: "3\t5\n")
    assert gh.git_ahead_behind(cwd="/x") == (5, 3)


def test_git_ahead_behind_no_upstream_is_level(monkeypatch):
    def boom(args, *, cwd):
        raise gh.GhError("no upstream configured")

    monkeypatch.setattr(gh, "_git", boom)
    assert gh.git_ahead_behind(cwd="/x") == (0, 0)


def test_git_upstream_ref_returns_tracking_ref(monkeypatch):
    monkeypatch.setattr(gh, "_git", lambda args, *, cwd: "origin/main\n")
    assert gh.git_upstream_ref(cwd="/x") == "origin/main"


def test_git_upstream_ref_none_when_absent(monkeypatch):
    def boom(args, *, cwd):
        raise gh.GhError("no upstream")

    monkeypatch.setattr(gh, "_git", boom)
    assert gh.git_upstream_ref(cwd="/x") is None


def test_pr_for_head_parses_snapshot(monkeypatch):
    payload = json.dumps({"number": 12, "state": "OPEN", "isDraft": True})
    monkeypatch.setattr(gh, "_run", lambda args, *, cwd=None: payload)
    assert gh.pr_for_head("fix/12", cwd="/x") == {
        "number": 12,
        "state": "OPEN",
        "isDraft": True,
    }


def test_pr_for_head_none_when_no_pr(monkeypatch):
    def boom(args, *, cwd=None):
        raise gh.GhError("no pull requests found")

    monkeypatch.setattr(gh, "_run", boom)
    assert gh.pr_for_head("fix/12", cwd="/x") is None
