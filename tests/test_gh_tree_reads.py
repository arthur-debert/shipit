"""Unit tests for the ``gh`` Tree-registry PR reads.

These pin the PR-snapshot shape ``tree list``/``gc`` rely on by patching only
the Exec seam (``_run_probe``), never the network. The read is a PROBE
(``check=False`` through the Exec runner, ADR-0028): a nonzero exit is a normal
answer for a scan over the fleet, so the fakes return an :class:`ExecResult`
with the rc under test rather than raising. The git-side registry reads
(ahead/behind, unpushed shas, umbrella/branch existence) live with their
adapter in ``test_git_adapter.py`` (PROC02-WS03).
"""

from __future__ import annotations

import json

from shipit import gh
from shipit.execrun import ExecResult


def _ok(stdout: str = "") -> ExecResult:
    return ExecResult(argv=("git",), rc=0, stdout=stdout, stderr="", duration_ms=1)


def _fail(stderr: str = "", rc: int = 1) -> ExecResult:
    return ExecResult(argv=("git",), rc=rc, stdout="", stderr=stderr, duration_ms=1)


def test_pr_for_head_parses_snapshot(monkeypatch):
    payload = json.dumps(
        {"number": 12, "state": "OPEN", "isDraft": True, "baseRefName": "main"}
    )
    monkeypatch.setattr(gh, "_run_probe", lambda args, *, cwd=None: _ok(payload))
    assert gh.pr_for_head("issues/12/work", cwd="/x") == {
        "number": 12,
        "state": "OPEN",
        "isDraft": True,
        "baseRefName": "main",
    }


def test_pr_for_head_none_when_no_pr(monkeypatch):
    # gh's documented "no pull requests found" exit is a PROVABLE absence -> None,
    # distinct from UNKNOWN (an undetermined state).
    monkeypatch.setattr(
        gh,
        "_run_probe",
        lambda args, *, cwd=None: _fail('no pull requests found for branch "x"'),
    )
    assert gh.pr_for_head("issues/12/work", cwd="/x") is None


def test_pr_for_head_unknown_on_loose_no_pr_phrasing(monkeypatch):
    # The no-PR check keys on gh's PRECISE marker ("no pull requests found for
    # branch"), not the loose substring "no pull request": an unrelated failure
    # that merely mentions a pull request must stay UNDETERMINED (UNKNOWN), never
    # collapse to a provable absence (None) that would suppress the gc warning.
    monkeypatch.setattr(
        gh,
        "_run_probe",
        lambda args, *, cwd=None: _fail("could not resolve no pull request here"),
    )
    assert gh.pr_for_head("issues/12/work", cwd="/x") is gh.UNKNOWN


def test_pr_for_head_unknown_on_non_no_pr_error(monkeypatch):
    # Any OTHER gh failure (auth/network/rate-limit) leaves the state UNDETERMINED:
    # it returns the first-class UNKNOWN sentinel, NOT None ("no PR").
    monkeypatch.setattr(
        gh,
        "_run_probe",
        lambda args, *, cwd=None: _fail("HTTP 401: Bad credentials"),
    )
    assert gh.pr_for_head("issues/12/work", cwd="/x") is gh.UNKNOWN


def test_pr_for_head_unknown_when_output_not_json(monkeypatch):
    # A scan/read boundary over the whole fleet: malformed/non-JSON gh output
    # (warnings, prompts, garbage on stdout) must NOT crash `tree list`, but it is an
    # unreadable state -> UNKNOWN (distinct from None / "no PR"), not silently collapsed.
    monkeypatch.setattr(
        gh, "_run_probe", lambda args, *, cwd=None: _ok("not json at all")
    )
    assert gh.pr_for_head("issues/12/work", cwd="/x") is gh.UNKNOWN


def test_pr_for_head_unknown_when_output_empty(monkeypatch):
    # A clean run that yields no stdout is anomalous (a real PR always prints JSON):
    # treat it as an unreadable state, not "no PR".
    monkeypatch.setattr(gh, "_run_probe", lambda args, *, cwd=None: _ok("   "))
    assert gh.pr_for_head("issues/12/work", cwd="/x") is gh.UNKNOWN


def test_pr_for_head_unknown_when_not_a_dict(monkeypatch):
    # Valid JSON but not an object (a list / scalar) is malformed for this read.
    monkeypatch.setattr(gh, "_run_probe", lambda args, *, cwd=None: _ok("[1, 2, 3]"))
    assert gh.pr_for_head("issues/12/work", cwd="/x") is gh.UNKNOWN


def test_pr_for_head_unknown_when_dict_missing_fields(monkeypatch):
    # A JSON object that decoded cleanly but lacks the load-bearing fields (an empty
    # object) is NOT a usable snapshot: returning it would render as `#None None` in
    # `tree list`. Treat it as an unreadable state -> UNKNOWN.
    monkeypatch.setattr(gh, "_run_probe", lambda args, *, cwd=None: _ok("{}"))
    assert gh.pr_for_head("issues/12/work", cwd="/x") is gh.UNKNOWN


def test_pr_for_head_unknown_when_fields_wrong_type(monkeypatch):
    # number/state present but null (or otherwise mistyped) is the same malformed
    # case the `#None None` bug came from: number must be an int and state a str,
    # else the snapshot is undetermined -> UNKNOWN.
    payload = json.dumps({"number": None, "state": None, "isDraft": False})
    monkeypatch.setattr(gh, "_run_probe", lambda args, *, cwd=None: _ok(payload))
    assert gh.pr_for_head("issues/12/work", cwd="/x") is gh.UNKNOWN
