"""Unit tests for :func:`shipit.gh.resolve_pr` — the shared PR-target resolver.

Promoted to the gh adapter (CLI01-WS03): the three-way branching over
`pr_number_probe`'s answer is per-tool knowledge, so it lives with the tool.
The tests pin the discrimination the whole pr family leans on: an explicit
number mints the typed target, a genuinely PR-less branch is ``None`` (a
normal state), and a real gh/auth failure raises — never collapsed into None.
"""

from __future__ import annotations

import pytest

from shipit import gh
from shipit.execrun import ExecError, ExecResult
from shipit.gh import resolve_pr
from shipit.identity import repo_from_slug
from shipit.pr import PrId
from shipit.prstate.errors import PrStateError

REPO = repo_from_slug("owner/repo")


def test_resolver_explicit_pr_mints_the_typed_target():
    # The resolver is where the PrId is MINTED (ADR-0030): explicit number +
    # the root context's repo become the one typed target the services take.
    assert resolve_pr(7, REPO) == PrId(repo=REPO, number=7)


def test_resolver_rejects_a_corrupt_explicit_number():
    # Construction-is-validation rides the mint: a non-positive number can
    # never become a PR target.
    with pytest.raises(ValueError, match="number"):
        resolve_pr(0, REPO)


def _probe_result(rc: int, stdout: str = "", stderr: str = "") -> ExecResult:
    return ExecResult(argv=("gh",), rc=rc, stdout=stdout, stderr=stderr, duration_ms=1)


def test_resolver_no_pr_marker_maps_to_none(monkeypatch):
    """gh's "no pull requests found for branch" exit is a normal no-PR state -> None."""
    monkeypatch.setattr(
        gh,
        "pr_number_probe",
        lambda: _probe_result(1, stderr='no pull requests found for branch "x"'),
    )
    assert resolve_pr(None, REPO) is None


def test_resolver_real_gh_error_propagates(monkeypatch):
    """Any other gh failure becomes an ExecError — never collapsed into None."""
    monkeypatch.setattr(
        gh,
        "pr_number_probe",
        lambda: _probe_result(1, stderr="could not authenticate"),
    )
    with pytest.raises(ExecError):
        resolve_pr(None, REPO)


def test_resolver_parses_number_into_the_typed_target(monkeypatch):
    monkeypatch.setattr(
        gh, "pr_number_probe", lambda: _probe_result(0, stdout='{"number": 99}')
    )
    assert resolve_pr(None, REPO) == PrId(repo=REPO, number=99)


def test_resolver_empty_body_is_no_pr(monkeypatch):
    """Defensive: a non-erroring empty body also means no PR."""
    monkeypatch.setattr(gh, "pr_number_probe", lambda: _probe_result(0, stdout="  "))
    assert resolve_pr(None, REPO) is None


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
        lambda: _probe_result(0, stdout=f'{{"number": {wire_number}}}'),
    )
    with pytest.raises(PrStateError, match="number"):
        resolve_pr(None, REPO)


def test_resolver_unparseable_json_is_a_prstate_error(monkeypatch):
    monkeypatch.setattr(
        gh, "pr_number_probe", lambda: _probe_result(0, stdout="not-json")
    )
    with pytest.raises(PrStateError, match="unparseable"):
        resolve_pr(None, REPO)
