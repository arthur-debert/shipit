"""Unit tests for the `pr` deep module in ISOLATION (ADR-0024, issue #202).

Exercises the canonical `PR` value object and its ONE core boundary with plain
dicts — no network, no view. The load-bearing invariants:

  * the core is built ONCE, off a GitHub `pullRequest` node, via `core_from_node`;
  * `head_sha` is read exactly one way (the same builder for both the gh-pr-view
    and the GraphQL node shapes, which share camelCase keys) and is minted into
    the typed `Sha` identity at that one boundary (COR02, issue #251);
  * a core field can never be defaulted-in — a node missing `isDraft` / `headRefOid`
    fails loud rather than fabricating a `PR` (the killed `is_draft=False` trap);
  * `PR` composes the WS01 `Repo` identity and is a frozen value object.
"""

from __future__ import annotations

import dataclasses

import pytest
from shipit.identity import Owner, Repo, Sha
from shipit.pr import CORE_JSON_FIELDS, PR, core_from_node

REPO = Repo(owner=Owner(login="octocat"), name="hello-world")

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


def test_pr_composes_repo_identity():
    pr = PR(
        repo=REPO,
        number=7,
        head_sha=Sha(HEAD),
        base_ref="main",
        is_draft=True,
        merge_state="CLEAN",
    )
    assert pr.repo == REPO
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
    assert pr == PR(
        repo=REPO,
        number=7,
        head_sha=Sha(HEAD),
        base_ref="main",
        is_draft=True,
        merge_state="CLEAN",
    )


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
    # `number` is the PR's identity field: a str/None/float/bool from fixture or
    # API drift must RAISE at the one wire read, never mint a corrupt identity
    # (`True` covered explicitly since `isinstance(True, int)` holds).
    with pytest.raises(ValueError, match="number"):
        core_from_node({**NODE, "number": bad}, REPO)


def test_valid_int_number_parses():
    assert core_from_node({**NODE, "number": 42}, REPO).number == 42
