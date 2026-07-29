from __future__ import annotations

import json

import pytest

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
    assert gh.pr_for_head("issues/12/work", cwd="/x") == gh.HeadPr(
        number=12, state="OPEN", is_draft=True, base_ref="main"
    )


def test_pr_for_head_normalizes_state_case(monkeypatch):
    payload = json.dumps(
        {"number": 12, "state": "merged", "isDraft": False, "baseRefName": "main"}
    )
    monkeypatch.setattr(gh, "_run_probe", lambda args, *, cwd=None: _ok(payload))
    pr = gh.pr_for_head("issues/12/work", cwd="/x")
    assert pr == gh.HeadPr(number=12, state="MERGED", is_draft=False, base_ref="main")


def test_pr_for_head_strips_whitespace_from_str_fields(monkeypatch):
    payload = json.dumps(
        {"number": 12, "state": "open ", "isDraft": False, "baseRefName": " main\n"}
    )
    monkeypatch.setattr(gh, "_run_probe", lambda args, *, cwd=None: _ok(payload))
    pr = gh.pr_for_head("issues/12/work", cwd="/x")
    assert pr == gh.HeadPr(number=12, state="OPEN", is_draft=False, base_ref="main")


def test_pr_for_number_parses_attachment_snapshot(monkeypatch):
    payload = json.dumps(
        {
            "number": 12,
            "state": "open",
            "isDraft": True,
            "baseRefName": " main ",
            "headRefName": " feature/x\n",
            "isCrossRepository": True,
            "maintainerCanModify": True,
        }
    )
    calls = []

    def fake_run(args, *, cwd=None, token=None, timeout=None):
        calls.append(args)
        return payload

    monkeypatch.setattr(gh, "_run", fake_run)

    assert gh.pr_for_number(12, repo="owner/repo") == gh.PrAttachment(
        number=12,
        state="OPEN",
        is_draft=True,
        base_ref="main",
        head_ref="feature/x",
        is_cross_repository=True,
        maintainer_can_modify=True,
    )
    assert calls == [
        [
            "gh",
            "pr",
            "view",
            "12",
            "--repo",
            "owner/repo",
            "--json",
            "number,state,isDraft,baseRefName,headRefName,isCrossRepository,maintainerCanModify",
        ]
    ]


def test_pr_for_number_fails_loud_on_missing_head_ref(monkeypatch):
    monkeypatch.setattr(
        gh,
        "_run",
        lambda args, *, cwd=None, token=None, timeout=None: json.dumps(
            {"number": 12, "state": "OPEN", "isDraft": True, "baseRefName": "main"}
        ),
    )

    with pytest.raises(ValueError, match="headRefName"):
        gh.pr_for_number(12)


def test_pr_for_head_none_when_no_pr(monkeypatch):
    monkeypatch.setattr(
        gh,
        "_run_probe",
        lambda args, *, cwd=None: _fail('no pull requests found for branch "x"'),
    )
    assert gh.pr_for_head("issues/12/work", cwd="/x") is None


def test_pr_for_head_unknown_on_loose_no_pr_phrasing(monkeypatch):
    monkeypatch.setattr(
        gh,
        "_run_probe",
        lambda args, *, cwd=None: _fail("could not resolve no pull request here"),
    )
    assert gh.pr_for_head("issues/12/work", cwd="/x") is gh.UNKNOWN


def test_pr_for_head_unknown_on_non_no_pr_error(monkeypatch):
    monkeypatch.setattr(
        gh,
        "_run_probe",
        lambda args, *, cwd=None: _fail("HTTP 401: Bad credentials"),
    )
    assert gh.pr_for_head("issues/12/work", cwd="/x") is gh.UNKNOWN


def test_pr_for_head_unknown_when_output_not_json(monkeypatch):
    monkeypatch.setattr(
        gh, "_run_probe", lambda args, *, cwd=None: _ok("not json at all")
    )
    assert gh.pr_for_head("issues/12/work", cwd="/x") is gh.UNKNOWN


def test_pr_for_head_unknown_when_output_empty(monkeypatch):
    monkeypatch.setattr(gh, "_run_probe", lambda args, *, cwd=None: _ok("   "))
    assert gh.pr_for_head("issues/12/work", cwd="/x") is gh.UNKNOWN


def test_pr_for_head_unknown_when_not_a_dict(monkeypatch):
    monkeypatch.setattr(gh, "_run_probe", lambda args, *, cwd=None: _ok("[1, 2, 3]"))
    assert gh.pr_for_head("issues/12/work", cwd="/x") is gh.UNKNOWN


def test_pr_for_head_unknown_when_dict_missing_fields(monkeypatch):
    monkeypatch.setattr(gh, "_run_probe", lambda args, *, cwd=None: _ok("{}"))
    assert gh.pr_for_head("issues/12/work", cwd="/x") is gh.UNKNOWN


def test_pr_for_head_unknown_when_fields_wrong_type(monkeypatch):
    payload = json.dumps({"number": None, "state": None, "isDraft": False})
    monkeypatch.setattr(gh, "_run_probe", lambda args, *, cwd=None: _ok(payload))
    assert gh.pr_for_head("issues/12/work", cwd="/x") is gh.UNKNOWN


def test_pr_for_head_unknown_when_isdraft_or_base_malformed(monkeypatch):
    for payload in (
        {"number": 7, "state": "OPEN", "isDraft": None, "baseRefName": "main"},
        {"number": 7, "state": "OPEN", "isDraft": True, "baseRefName": None},
        {"number": 7, "state": "OPEN", "isDraft": True},
    ):
        monkeypatch.setattr(
            gh,
            "_run_probe",
            lambda args, *, cwd=None, payload=payload: _ok(json.dumps(payload)),
        )
        assert gh.pr_for_head("issues/12/work", cwd="/x") is gh.UNKNOWN


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({}, "number"),
        ({"number": "12"}, "number"),
        ({"number": True}, "number"),
        ({"number": 12}, "state"),
        ({"number": 12, "state": ""}, "state"),
        ({"number": 12, "state": "OPEN"}, "isDraft"),
        ({"number": 12, "state": "OPEN", "isDraft": "yes"}, "isDraft"),
        ({"number": 12, "state": "OPEN", "isDraft": False}, "baseRefName"),
        (
            {"number": 12, "state": "OPEN", "isDraft": False, "baseRefName": 3},
            "baseRefName",
        ),
    ],
)
def test_head_pr_construction_raises_naming_the_field(payload, field):
    with pytest.raises(ValueError, match=field):
        gh._head_pr_from_json(payload)


def test_head_pr_display_state_normalizes_draft():
    draft = gh.HeadPr(number=1, state="OPEN", is_draft=True, base_ref="main")
    open_pr = gh.HeadPr(number=2, state="OPEN", is_draft=False, base_ref="main")
    merged = gh.HeadPr(number=3, state="MERGED", is_draft=True, base_ref="main")
    assert draft.display_state == "DRAFT"
    assert open_pr.display_state == "OPEN"
    assert merged.display_state == "MERGED"
