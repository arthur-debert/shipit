"""Unit tests for :func:`shipit.gh.resolve_pr` — the shared PR-target resolver.

Promoted to the gh adapter (CLI01-WS03): the three-way branching over
`pr_number_probe`'s answer is per-tool knowledge, so it lives with the tool.
The tests pin the discrimination the whole pr family leans on: an explicit
number mints the typed target, a genuinely PR-less branch is ``None`` (a
normal state), and a real gh/auth failure raises — never collapsed into None.
The branch probe is PINNED to the caller's repo (``--repo <slug>``), never
gh's ambient inference, so a number discovered in one repo can never be minted
under another.
"""

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
    # The resolver is where the PrId is MINTED (ADR-0030): explicit number +
    # the root context's repo become the one typed target the services take.
    # An explicit number short-circuits the branch probe entirely.
    assert resolve_pr(7, REPO, BRANCH) == PrId(repo=REPO, number=7)


def test_resolver_rejects_a_corrupt_explicit_number():
    # Construction-is-validation rides the mint: a non-positive number can
    # never become a PR target.
    with pytest.raises(ValueError, match="number"):
        resolve_pr(0, REPO, BRANCH)


def test_resolver_none_branch_is_no_pr():
    # Detached / unborn HEAD: there is no current branch, hence no branch PR.
    # A normal no-PR state resolved WITHOUT touching gh at all.
    assert resolve_pr(None, REPO, None) is None


def test_probe_pins_the_repo_and_branch_into_the_argv(monkeypatch):
    # The load-bearing guard against a mismatched ambient repo: the probe pins
    # BOTH the branch (positional selector) and `--repo <slug>` (the caller's
    # origin-derived identity), so gh's ambient inference (GH_REPO / a
    # set-default / a non-origin remote) can never resolve the number against a
    # DIFFERENT repo than the one the PrId is minted under.
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
    """gh's "no pull requests found for branch" exit is a normal no-PR state -> None."""
    monkeypatch.setattr(
        gh,
        "pr_number_probe",
        lambda repo, branch: _probe_result(
            1, stderr='no pull requests found for branch "x"'
        ),
    )
    assert resolve_pr(None, REPO, BRANCH) is None


def test_resolver_real_gh_error_propagates(monkeypatch):
    """Any other gh failure becomes an ExecError — never collapsed into None."""
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
    """Defensive: a non-erroring empty body also means no PR."""
    monkeypatch.setattr(
        gh, "pr_number_probe", lambda repo, branch: _probe_result(0, stdout="  ")
    )
    assert resolve_pr(None, REPO, BRANCH) is None


@pytest.mark.parametrize("wire_number", ['"99"', "7.0", "true"])
def test_resolver_rejects_a_malformed_wire_number(monkeypatch, wire_number):
    # The wire read mints through PrId with NO coercion: a stringy/float/bool
    # `number` from unexpected `gh` output would slip past a silent `int(...)`
    # and mint the wrong target, so construction-is-validation (ADR-0030) must
    # reject it here at the one wire read — surfaced as a PrStateError like the
    # unparseable-JSON case.
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
