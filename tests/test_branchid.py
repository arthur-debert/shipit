"""Unit tests for branch-identity derivation (`shipit.branchid`, LOG04-WS01).

The full matrix from the PRD's Testing Decisions: work-stream branches, the
epic umbrella, standalone-issue branches, ephemeral branches, and garbage —
each to ``(epic, ws)`` or NOTHING. Pure-core tests, no git: the parser is a
total function over wire data (a PR's ``headRefName``), so nothing raises.
"""

from __future__ import annotations

import pytest

from shipit.branchid import NOTHING, BranchIdentity, derive


@pytest.mark.parametrize(
    ("branch", "epic", "ws"),
    [
        # The work-stream form E/WSnn → both halves; the index is an INT
        # (WS01 is a display form, never data — ADR-0032).
        ("LOG04/WS01", "LOG04", 1),
        ("RVW01/WS12", "RVW01", 12),
        # An index past 99 legitimately widens (the writer formats %02d).
        ("HAR02/WS100", "HAR02", 100),
        # The epic umbrella carries the epic identity, no work stream.
        ("LOG04/umbrella", "LOG04", None),
    ],
)
def test_namespaced_branches_derive_their_identity(branch, epic, ws):
    assert derive(branch) == BranchIdentity(epic=epic, ws=ws)


@pytest.mark.parametrize(
    "branch",
    [
        # The other grammar shapes carry no epic/ws identity.
        "issues/375/work",  # standalone issue (ADR-0026)
        "issues/375/onboard",
        "ephemeral/sess-20260703-1234",  # coordinator session Tree (ADR-0027)
        "main",  # no namespace at all
        "docs/log04-dev-cycle",  # freeform slash branch, non-WS leaf
        "feature/foo",
        # Out-of-grammar / degenerate inputs.
        "LOG04/WS1",  # display form is zero-padded WSnn — two digits minimum
        "LOG04/WS00",  # the writer rejects a non-positive index
        "LOG04/WSxx",
        "LOG04/WS01/extra",  # three segments
        "/WS01",  # empty epic
        "LOG-04/WS01",  # epic code is a single alphanumeric token
        "LOG04/",
        "umbrella",
        "",
    ],
)
def test_out_of_grammar_branches_derive_nothing(branch):
    assert derive(branch) is NOTHING


def test_non_string_wire_data_derives_nothing_never_raises():
    """`headRefName` is wire data: a missing key's None (or API drift) must
    degrade to NOTHING at this logging seam, never crash the fetch."""
    for value in (None, 42, ["LOG04/WS01"]):
        assert derive(value) is NOTHING


def test_identity_halves_feed_bind_directly():
    """Both halves are bind-ready: absent halves are None (dropped by
    logcontext.bind), present halves are the typed values."""
    identity = derive("RVW01/WS02")
    assert identity.epic == "RVW01"
    assert identity.ws == 2
    assert isinstance(identity.ws, int)
    umbrella = derive("RVW01/umbrella")
    assert umbrella.epic == "RVW01"
    assert umbrella.ws is None
