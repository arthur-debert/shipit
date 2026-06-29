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
    # gh's documented "no pull requests found" exit is a PROVABLE absence -> None,
    # distinct from UNKNOWN (an undetermined state).
    def boom(args, *, cwd=None):
        raise gh.GhError('gh pr view exited 1: no pull requests found for branch "x"')

    monkeypatch.setattr(gh, "_run", boom)
    assert gh.pr_for_head("fix/12", cwd="/x") is None


def test_pr_for_head_unknown_on_loose_no_pr_phrasing(monkeypatch):
    # The no-PR check keys on gh's PRECISE marker ("no pull requests found for
    # branch"), not the loose substring "no pull request": an unrelated failure
    # that merely mentions a pull request must stay UNDETERMINED (UNKNOWN), never
    # collapse to a provable absence (None) that would suppress the gc warning.
    def boom(args, *, cwd=None):
        raise gh.GhError("gh pr view exited 1: could not resolve no pull request here")

    monkeypatch.setattr(gh, "_run", boom)
    assert gh.pr_for_head("fix/12", cwd="/x") is gh.UNKNOWN


def test_pr_for_head_unknown_on_non_no_pr_error(monkeypatch):
    # Any OTHER gh failure (auth/network/rate-limit) leaves the state UNDETERMINED:
    # it returns the first-class UNKNOWN sentinel, NOT None ("no PR").
    def boom(args, *, cwd=None):
        raise gh.GhError("gh pr view exited 1: HTTP 401: Bad credentials")

    monkeypatch.setattr(gh, "_run", boom)
    assert gh.pr_for_head("fix/12", cwd="/x") is gh.UNKNOWN


def test_pr_for_head_unknown_when_output_not_json(monkeypatch):
    # A scan/read boundary over the whole fleet: malformed/non-JSON gh output
    # (warnings, prompts, garbage on stdout) must NOT crash `tree list`, but it is an
    # unreadable state -> UNKNOWN (distinct from None / "no PR"), not silently collapsed.
    monkeypatch.setattr(gh, "_run", lambda args, *, cwd=None: "not json at all")
    assert gh.pr_for_head("fix/12", cwd="/x") is gh.UNKNOWN


def test_pr_for_head_unknown_when_output_empty(monkeypatch):
    # A clean run that yields no stdout is anomalous (a real PR always prints JSON):
    # treat it as an unreadable state, not "no PR".
    monkeypatch.setattr(gh, "_run", lambda args, *, cwd=None: "   ")
    assert gh.pr_for_head("fix/12", cwd="/x") is gh.UNKNOWN


def test_pr_for_head_unknown_when_not_a_dict(monkeypatch):
    # Valid JSON but not an object (a list / scalar) is malformed for this read.
    monkeypatch.setattr(gh, "_run", lambda args, *, cwd=None: "[1, 2, 3]")
    assert gh.pr_for_head("fix/12", cwd="/x") is gh.UNKNOWN
