from __future__ import annotations

import pytest

from shipit.branchid import NOTHING, BranchIdentity, derive


@pytest.mark.parametrize(
    ("branch", "epic", "ws"),
    [
        ("LOG04/WS01", "LOG04", 1),
        ("RVW01/WS12", "RVW01", 12),
        ("HAR02/WS100", "HAR02", 100),
        ("LOG04/umbrella", "LOG04", None),
    ],
)
def test_namespaced_branches_derive_their_identity(branch, epic, ws):
    assert derive(branch) == BranchIdentity(epic=epic, ws=ws)


@pytest.mark.parametrize(
    "branch",
    [
        "issues/375/work",
        "issues/375/onboard",
        "ephemeral/sess-20260703-1234",
        "main",
        "docs/log04-dev-cycle",
        "feature/foo",
        "LOG04/WS1",
        "LOG04/WS00",
        "LOG04/WSxx",
        "LOG04/WS01/extra",
        "/WS01",
        "LOG-04/WS01",
        "LOG04/",
        "umbrella",
        "",
    ],
)
def test_out_of_grammar_branches_derive_nothing(branch):
    assert derive(branch) is NOTHING


def test_non_string_wire_data_derives_nothing_never_raises():
    for value in (None, 42, ["LOG04/WS01"]):
        assert derive(value) is NOTHING


def test_identity_halves_feed_bind_directly():
    identity = derive("RVW01/WS02")
    assert identity.epic == "RVW01"
    assert identity.ws == 2
    assert isinstance(identity.ws, int)
    umbrella = derive("RVW01/umbrella")
    assert umbrella.epic == "RVW01"
    assert umbrella.ws is None
