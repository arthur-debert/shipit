"""Unit tests for the `pr` deep module in ISOLATION (ADR-0024, issue #202).

Exercises the `PrId` identity, the canonical `PR` value object, and their ONE
core boundary with plain dicts — no network, no view. The load-bearing
invariants:

  * `PrId` is the identity half of the PR noun — `(repo, number)`, nothing
    fetched — with construction-is-validation (CLI01-WS02 / ADR-0030): a
    non-int / bool / non-positive number can never mint an identity;
  * `PR` COMPOSES a `PrId` (the way a view composes a PR) and delegates
    `repo` / `number` / `slug` to it — one noun at two granularities, not a
    second snapshot type;
  * the core is built ONCE, off a GitHub `pullRequest` node, via `core_from_node`;
  * `head_sha` is read exactly one way (the same builder for both the gh-pr-view
    and the GraphQL node shapes, which share camelCase keys) and is minted into
    the typed `Sha` identity at that one boundary (COR02, issue #251);
  * a core field can never be defaulted-in — a node missing `isDraft` / `headRefOid`
    fails loud rather than fabricating a `PR` (the killed `is_draft=False` trap);
  * `PR` composes the WS01 `Repo` identity (via its `PrId`) and both are frozen
    value objects.
"""

from __future__ import annotations

import dataclasses

import pytest
from shipit.identity import Owner, Repo, Sha
from shipit.pr import CORE_JSON_FIELDS, PR, PrId, core_from_node

REPO = Repo(owner=Owner(login="octocat"), name="hello-world")
OTHER_REPO = Repo(owner=Owner(login="octocat"), name="other")

HEAD = "deadbeef" * 5  # a full 40-hex sha

# A `gh pr view --json` node and a GraphQL `pullRequest` node share these keys, so
# ONE node fixture stands in for BOTH fetch shapes — the point of the single boundary.
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


# --- PrId: the identity half as its own value object (CLI01-WS02) -------------


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
    # The repo is PART of the identity: same number in another repo is another PR.
    assert PrId(repo=REPO, number=7) != PrId(repo=OTHER_REPO, number=7)


def test_prid_is_hashable_on_its_identity():
    # PR-scoped joins key on (repo, number) — a PrId must be usable as that key.
    assert len({PrId(repo=REPO, number=7), PrId(repo=REPO, number=7)}) == 1


@pytest.mark.parametrize("bad", ["7", None, 7.0, True])
def test_prid_rejects_nonint_number(bad):
    # Construction IS the validation (ADR-0030): a str/None/float/bool number can
    # never mint an identity (`True` covered explicitly — isinstance(True, int)).
    with pytest.raises(ValueError, match="number"):
        PrId(repo=REPO, number=bad)


@pytest.mark.parametrize("bad", [0, -1])
def test_prid_rejects_nonpositive_number(bad):
    with pytest.raises(ValueError, match="number"):
        PrId(repo=REPO, number=bad)


# --- PR: composes the PrId, delegates the identity reads ----------------------


def test_pr_composes_prid_identity():
    pr = _pr()
    assert pr.id == PrId(repo=REPO, number=7)
    # The delegating reads: identity fields live ONCE, on the composed PrId.
    assert pr.repo == REPO
    assert pr.number == 7
    assert pr.slug == "octocat/hello-world"


def test_pr_is_frozen_value_object():
    pr = core_from_node(NODE, REPO)
    # Frozen (ADR-0021): identity + core, immutable.
    with pytest.raises(dataclasses.FrozenInstanceError):
        pr.head_sha = Sha("0" * 40)  # type: ignore[misc]
    # Value equality: same identity + core compares equal.
    assert pr == core_from_node(dict(NODE), REPO)


def test_core_from_node_reads_the_whole_core_once():
    pr = core_from_node(NODE, REPO)
    assert pr == _pr()


def test_core_from_node_is_one_boundary_for_both_node_shapes():
    # The gh-pr-view dict and the GraphQL pullRequest node carry identical keys, so
    # the same builder produces the same core — head_sha fetched exactly one way.
    gh_pr_view_node = dict(NODE)
    graphql_node = dict(NODE)
    assert core_from_node(gh_pr_view_node, REPO) == core_from_node(graphql_node, REPO)


def test_core_json_fields_cover_the_core():
    # The advertised field list is exactly what the builder reads — a fetch path can
    # request `CORE_JSON_FIELDS` and know it satisfies `core_from_node`.
    assert core_from_node({k: NODE[k] for k in CORE_JSON_FIELDS}, REPO).head_sha == (
        Sha(HEAD)
    )


def test_core_from_node_mints_a_typed_normalized_sha():
    # COR02 (#251): `headRefOid` becomes a `Sha` at the ONE wire read — validated
    # and lowercase-normalized, so a case-varying upstream yields ONE identity.
    pr = core_from_node({**NODE, "headRefOid": HEAD.upper()}, REPO)
    assert pr.head_sha == Sha(HEAD)


@pytest.mark.parametrize("bad", ["", "deadbeef", "not-hex!" * 5, None])
def test_malformed_head_sha_fails_loud(bad):
    # COR02 (#251): an empty, abbreviated, or non-hex `headRefOid` raises at the
    # boundary instead of flowing on as a bogus commit identity.
    with pytest.raises(ValueError):
        core_from_node({**NODE, "headRefOid": bad}, REPO)


def test_nullable_core_fields_tolerate_missing():
    # base_ref / merge_state are genuinely nullable (GitHub returns them null), so a
    # node without them still builds — they are NOT the trap.
    pr = core_from_node({"number": 1, "headRefOid": HEAD, "isDraft": False}, REPO)
    assert pr.base_ref is None
    assert pr.merge_state is None
    assert pr.is_draft is False


def test_missing_is_draft_fails_loud_not_defaulted():
    # The killed trap: a path that never fetched is_draft cannot build a PR that
    # silently reads is_draft=False. The required key raises instead.
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
    # A present-but-non-bool `isDraft` (e.g. GitHub returning `null`) must RAISE, not
    # be silently coerced by `bool(...)` — a `null` would become `False` and defeat
    # the fail-loud-core invariant this boundary enforces.
    with pytest.raises(ValueError):
        core_from_node({"number": 1, "headRefOid": HEAD, "isDraft": bad}, REPO)


@pytest.mark.parametrize("bad", ["7", None, 7.0, True])
def test_nonint_number_fails_loud(bad):
    # `number` is the PR's identity field — validated by PrId itself (construction
    # is the validity check) and re-raised with the wire context, so a
    # str/None/float/bool from fixture or API drift dies at the one wire read,
    # never minting a corrupt identity.
    with pytest.raises(ValueError, match="number"):
        core_from_node({**NODE, "number": bad}, REPO)


def test_valid_int_number_parses():
    assert core_from_node({**NODE, "number": 42}, REPO).number == 42
