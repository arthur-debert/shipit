from __future__ import annotations

import dataclasses

import pytest

from shipit.identity import Owner, Repo, Sha
from shipit.pr import CORE_JSON_FIELDS, PR, PrId, core_from_node

REPO = Repo(owner=Owner(login="octocat"), name="hello-world")
OTHER_REPO = Repo(owner=Owner(login="octocat"), name="other")

HEAD = "deadbeef" * 5

NODE = {
    "number": 7,
    "headRefOid": HEAD,
    "baseRefName": "main",
    "isDraft": True,
    "mergeStateStatus": "CLEAN",
}


def _pr(number: int = 7) -> PR:
    return PR(
        id=PrId(repo=REPO, number=number),
        head_sha=Sha(HEAD),
        base_ref="main",
        is_draft=True,
        merge_state="CLEAN",
    )


def test_prid_composes_repo_identity():
    pr_id = PrId(repo=REPO, number=7)
    assert pr_id.repo == REPO
    assert pr_id.number == 7
    assert pr_id.slug == "octocat/hello-world"


def test_prid_is_a_frozen_value_object():
    pr_id = PrId(repo=REPO, number=7)
    with pytest.raises(dataclasses.FrozenInstanceError):
        pr_id.number = 8  # type: ignore[misc]


def test_prid_value_equality():
    assert PrId(repo=REPO, number=7) == PrId(repo=REPO, number=7)
    assert PrId(repo=REPO, number=7) != PrId(repo=REPO, number=8)
    assert PrId(repo=REPO, number=7) != PrId(repo=OTHER_REPO, number=7)


def test_prid_is_hashable_on_its_identity():
    assert len({PrId(repo=REPO, number=7), PrId(repo=REPO, number=7)}) == 1


@pytest.mark.parametrize("bad", ["7", None, 7.0, True])
def test_prid_rejects_nonint_number(bad):
    with pytest.raises(ValueError, match="number"):
        PrId(repo=REPO, number=bad)


@pytest.mark.parametrize("bad", [0, -1])
def test_prid_rejects_nonpositive_number(bad):
    with pytest.raises(ValueError, match="number"):
        PrId(repo=REPO, number=bad)


def test_pr_composes_prid_identity():
    pr = _pr()
    assert pr.id == PrId(repo=REPO, number=7)
    assert pr.repo == REPO
    assert pr.number == 7
    assert pr.slug == "octocat/hello-world"


def test_pr_is_frozen_value_object():
    pr = core_from_node(NODE, REPO)
    with pytest.raises(dataclasses.FrozenInstanceError):
        pr.head_sha = Sha("0" * 40)  # type: ignore[misc]
    assert pr == core_from_node(dict(NODE), REPO)


def test_core_from_node_reads_the_whole_core_once():
    pr = core_from_node(NODE, REPO)
    assert pr == _pr()


def test_core_from_node_is_one_boundary_for_both_node_shapes():
    gh_pr_view_node = dict(NODE)
    graphql_node = dict(NODE)
    assert core_from_node(gh_pr_view_node, REPO) == core_from_node(graphql_node, REPO)


def test_core_json_fields_cover_the_core():
    assert core_from_node({k: NODE[k] for k in CORE_JSON_FIELDS}, REPO).head_sha == (
        Sha(HEAD)
    )


def test_core_from_node_mints_a_typed_normalized_sha():
    pr = core_from_node({**NODE, "headRefOid": HEAD.upper()}, REPO)
    assert pr.head_sha == Sha(HEAD)


@pytest.mark.parametrize("bad", ["", "deadbeef", "not-hex!" * 5, None])
def test_malformed_head_sha_fails_loud(bad):
    with pytest.raises(ValueError):
        core_from_node({**NODE, "headRefOid": bad}, REPO)


def test_nullable_core_fields_tolerate_missing():
    pr = core_from_node({"number": 1, "headRefOid": HEAD, "isDraft": False}, REPO)
    assert pr.base_ref is None
    assert pr.merge_state is None
    assert pr.is_draft is False


def test_missing_is_draft_fails_loud_not_defaulted():
    with pytest.raises(KeyError):
        core_from_node({"number": 1, "headRefOid": HEAD}, REPO)


def test_missing_head_sha_fails_loud():
    with pytest.raises(KeyError):
        core_from_node({"number": 1, "isDraft": False}, REPO)


def test_missing_number_fails_loud():
    with pytest.raises(KeyError):
        core_from_node({"headRefOid": HEAD, "isDraft": False}, REPO)


@pytest.mark.parametrize("bad", [None, "true", 1, 0])
def test_nonbool_is_draft_fails_loud_not_coerced(bad):
    with pytest.raises(ValueError):
        core_from_node({"number": 1, "headRefOid": HEAD, "isDraft": bad}, REPO)


@pytest.mark.parametrize("bad", ["7", None, 7.0, True])
def test_nonint_number_fails_loud(bad):
    with pytest.raises(ValueError, match="number"):
        core_from_node({**NODE, "number": bad}, REPO)


def test_valid_int_number_parses():
    assert core_from_node({**NODE, "number": 42}, REPO).number == 42
