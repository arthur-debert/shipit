from __future__ import annotations

import pytest

from shipit import gh
from shipit.execrun import ExecError, ExecResult
from shipit.gh import pr_number_probe, resolve_pr
from shipit.identity import repo_from_slug
from shipit.pr import PrId
from shipit.prstate.errors import PrStateError

REPO = repo_from_slug("owner/repo")
BRANCH = "feature/x"


def test_resolver_explicit_pr_mints_the_typed_target():
    assert resolve_pr(7, REPO, BRANCH) == PrId(repo=REPO, number=7)


def test_resolver_rejects_a_corrupt_explicit_number():
    with pytest.raises(ValueError, match="number"):
        resolve_pr(0, REPO, BRANCH)


def test_resolver_none_branch_is_no_pr():
    assert resolve_pr(None, REPO, None) is None


def test_probe_pins_the_repo_and_branch_into_the_argv(monkeypatch):
    seen: dict = {}

    def fake_run_probe(argv, **kwargs):
        seen["argv"] = argv
        return ExecResult(
            argv=tuple(argv), rc=0, stdout='{"number": 5}', stderr="", duration_ms=1
        )

    monkeypatch.setattr(gh, "_run_probe", fake_run_probe)
    pr_number_probe(REPO, BRANCH)
    assert seen["argv"] == [
        "gh",
        "pr",
        "view",
        BRANCH,
        "--repo",
        "owner/repo",
        "--json",
        "number",
    ]


def _probe_result(rc: int, stdout: str = "", stderr: str = "") -> ExecResult:
    return ExecResult(argv=("gh",), rc=rc, stdout=stdout, stderr=stderr, duration_ms=1)


def test_resolver_no_pr_marker_maps_to_none(monkeypatch):
    monkeypatch.setattr(
        gh,
        "pr_number_probe",
        lambda repo, branch: _probe_result(
            1, stderr='no pull requests found for branch "x"'
        ),
    )
    assert resolve_pr(None, REPO, BRANCH) is None


def test_resolver_real_gh_error_propagates(monkeypatch):
    monkeypatch.setattr(
        gh,
        "pr_number_probe",
        lambda repo, branch: _probe_result(1, stderr="could not authenticate"),
    )
    with pytest.raises(ExecError):
        resolve_pr(None, REPO, BRANCH)


def test_resolver_parses_number_into_the_typed_target(monkeypatch):
    monkeypatch.setattr(
        gh,
        "pr_number_probe",
        lambda repo, branch: _probe_result(0, stdout='{"number": 99}'),
    )
    assert resolve_pr(None, REPO, BRANCH) == PrId(repo=REPO, number=99)


def test_resolver_empty_body_is_no_pr(monkeypatch):
    monkeypatch.setattr(
        gh, "pr_number_probe", lambda repo, branch: _probe_result(0, stdout="  ")
    )
    assert resolve_pr(None, REPO, BRANCH) is None


@pytest.mark.parametrize("wire_number", ['"99"', "7.0", "true"])
def test_resolver_rejects_a_malformed_wire_number(monkeypatch, wire_number):
    monkeypatch.setattr(
        gh,
        "pr_number_probe",
        lambda repo, branch: _probe_result(0, stdout=f'{{"number": {wire_number}}}'),
    )
    with pytest.raises(PrStateError, match="number"):
        resolve_pr(None, REPO, BRANCH)


def test_resolver_unparseable_json_is_a_prstate_error(monkeypatch):
    monkeypatch.setattr(
        gh, "pr_number_probe", lambda repo, branch: _probe_result(0, stdout="not-json")
    )
    with pytest.raises(PrStateError, match="unparseable"):
        resolve_pr(None, REPO, BRANCH)
