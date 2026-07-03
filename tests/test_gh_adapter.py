"""The ONE gh Tool adapter (PROC02-WS01, ADR-0028).

The engine's second boundary (`shipit/prstate/ghapi.py`) merged into
`shipit.gh`: pure-logic tests for the merged surface (no subprocess), plus the
mechanical sweep that pins one of the merge's structural guarantees — the
pagination-merging helper exists exactly once. The other guarantee — gh argv is
built ONLY inside the adapter ("tool argv built outside its Tool adapter" is a
statable defect, ADR-0028) — is the ``gh`` row of the table-driven cross-tool
sweep in ``test_tool_argv_sweep.py``.
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest

import shipit
from shipit import gh
from shipit.identity import Repo, Sha, repo_from_slug
from shipit.pr import PR, PrId
from shipit.prstate.errors import PrStateError

_SRC_ROOT = pathlib.Path(shipit.__file__).parent


# --- pagination merging (defined exactly once, in the adapter) ---------------


def test_merge_paginated_flattens_concatenated_arrays():
    # `gh api --paginate` emits one JSON array per page, concatenated.
    out = '[{"id": 1}, {"id": 2}]\n[{"id": 3}]\n'
    assert [o["id"] for o in gh._merge_paginated(out)] == [1, 2, 3]


def test_merge_paginated_single_page():
    assert gh._merge_paginated('[{"id": 1}]') == [{"id": 1}]


def test_pagination_helper_exists_exactly_once():
    """The duplicated helper was the two-boundaries disease's visible symptom:
    after the merge, exactly one definition survives, in the adapter."""
    definitions = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_merge_paginated":
                definitions.append(path.relative_to(_SRC_ROOT.parent))
    assert definitions == [pathlib.Path("shipit/gh.py")]


# --- the merged REST/GraphQL surface (transport mocked at `_run`) -------------


def _capture_run(monkeypatch, stdout: str):
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return stdout

    monkeypatch.setattr(gh, "_run", fake_run)
    return calls


def test_rest_sends_string_fields_as_dash_f(monkeypatch):
    calls = _capture_run(monkeypatch, "{}")
    gh.rest(
        "repos/o/r/pulls/1/comments/2/replies", method="POST", fields={"body": "hi"}
    )
    assert calls == [
        [
            "gh",
            "api",
            "repos/o/r/pulls/1/comments/2/replies",
            "--method",
            "POST",
            "-f",
            "body=hi",
        ]
    ]


def test_rest_rejects_body_and_fields_together(monkeypatch):
    """`body` and `fields` are alternative payload forms; passing both would
    yield an ambiguous `gh api` invocation, so the adapter fails fast."""
    calls = _capture_run(monkeypatch, "{}")
    with pytest.raises(ValueError):
        gh.rest("repos/o/r", method="POST", body={"a": 1}, fields={"b": "2"})
    assert calls == []


def test_graphql_variable_encoding(monkeypatch):
    """None omitted entirely; int/bool type-infer via -F; str forced via -f
    (ID! variables must never be coerced to a number)."""
    calls = _capture_run(monkeypatch, json.dumps({"data": {"ok": True}}))
    assert gh.graphql("query {}", owner="o", pr=7, after=None) == {"ok": True}
    assert calls == [
        ["gh", "api", "graphql", "-f", "query=query {}", "-f", "owner=o", "-F", "pr=7"]
    ]


def test_graphql_errors_raise_the_semantic_error(monkeypatch):
    """The Exec succeeded (rc 0) but the answer is unusable: the adapter raises
    the engine's user-renderable `PrStateError`, never returns partial data."""
    payload = {"data": None, "errors": [{"message": "Could not resolve PR"}]}
    _capture_run(monkeypatch, json.dumps(payload))
    with pytest.raises(PrStateError):
        gh.graphql("query {}")


# --- typed returns (PROC03, ADR-0028): core value objects off the read surface -


def test_current_repo_returns_the_typed_repo(monkeypatch):
    """The repo read returns the `Repo` identity value object — minted through the
    ONE canonical slug parser, so an API-cased slug lands the case-normalized
    identity (ADR-0024) and no caller re-splits owner/name."""
    calls = _capture_run(monkeypatch, "Acme/Widget\n")
    repo = gh.current_repo()
    assert isinstance(repo, Repo)
    assert repo == repo_from_slug("acme/widget")
    assert repo.slug == "acme/widget"
    assert calls == [
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]
    ]


def test_current_repo_raises_on_unusable_output(monkeypatch):
    """gh exited 0 but produced no usable owner/name: a data-shape `ValueError`
    at the boundary (the transport failure is `ExecError`), never a bogus Repo."""
    _capture_run(monkeypatch, "\n")
    with pytest.raises(ValueError):
        gh.current_repo()


def test_repo_canonical_returns_the_typed_repo(monkeypatch):
    calls = _capture_run(monkeypatch, "New-Owner/New-Name\n")
    repo = gh.repo_canonical("old/alias")
    assert repo == repo_from_slug("new-owner/new-name")
    assert calls == [
        [
            "gh",
            "repo",
            "view",
            "old/alias",
            "--json",
            "nameWithOwner",
            "-q",
            ".nameWithOwner",
        ]
    ]


def test_pr_view_returns_the_parsed_object(monkeypatch):
    """The adapter owns the JSON parse (PROC03): callers receive the object,
    never a raw string to re-parse."""
    calls = _capture_run(monkeypatch, '{"number": 7, "headRefName": "feat"}\n')
    assert gh.pr_view("7", json_fields=["number", "headRefName"]) == {
        "number": 7,
        "headRefName": "feat",
    }
    assert calls == [["gh", "pr", "view", "7", "--json", "number,headRefName"]]


def test_pr_view_raises_on_unparseable_and_non_object_output(monkeypatch):
    _capture_run(monkeypatch, "not json")
    with pytest.raises(ValueError):
        gh.pr_view("7", json_fields=["number"])
    _capture_run(monkeypatch, "[1]")
    with pytest.raises(ValueError):
        gh.pr_view("7", json_fields=["number"])


def test_pr_core_returns_the_typed_pr_with_sha_head(monkeypatch):
    """The typed PR read: exactly the core field list on the wire, routed through
    the one `core_from_node` boundary — a `PR` with a `Sha`-typed, lowercase-
    normalized head comes back, not a dict."""
    head = "CAFE" * 10
    repo = repo_from_slug("owner/repo")
    target = PrId(repo=repo, number=7)
    calls = _capture_run(
        monkeypatch,
        json.dumps(
            {
                "number": 7,
                "headRefOid": head,
                "baseRefName": "main",
                "isDraft": True,
                "mergeStateStatus": "BLOCKED",
            }
        ),
    )
    core = gh.pr_core(target)
    assert isinstance(core, PR)
    assert core.id == target
    assert core.repo == repo
    assert core.head_sha == Sha(head.lower())
    assert (core.number, core.base_ref, core.is_draft, core.merge_state) == (
        7,
        "main",
        True,
        "BLOCKED",
    )
    # Exactly the CORE field list rides the argv — the one wire read (ADR-0024) —
    # scoped to the explicit repo so the read never depends on the cwd checkout.
    assert calls == [
        [
            "gh",
            "pr",
            "view",
            "7",
            "--repo",
            "owner/repo",
            "--json",
            "number,headRefOid,baseRefName,isDraft,mergeStateStatus",
        ]
    ]


def test_pr_core_fails_loud_on_a_malformed_core(monkeypatch):
    """The fail-loud-core discipline at the wire: a missing required key raises
    `KeyError`, a malformed head sha raises `ValueError` — never a defaulted or
    bogus core field flowing on."""
    target = PrId(repo=repo_from_slug("owner/repo"), number=7)
    _capture_run(monkeypatch, json.dumps({"number": 7, "isDraft": False}))
    with pytest.raises(KeyError):
        gh.pr_core(target)
    _capture_run(
        monkeypatch, json.dumps({"number": 7, "headRefOid": "abc", "isDraft": False})
    )
    with pytest.raises(ValueError):
        gh.pr_core(target)


def test_pr_meta_returns_the_raw_node_for_the_view_builder(monkeypatch):
    """`pr_meta` stays the raw-node read (no core noun spans checks+mergeability):
    the readiness view builder consumes it, routing the core through
    `core_from_node` — no parallel snapshot type is minted at the adapter."""
    node = {
        "number": 7,
        "headRefOid": "cafe" * 10,
        "baseRefName": "main",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [],
    }
    calls = _capture_run(monkeypatch, json.dumps(node))
    assert gh.pr_meta(PrId(repo=repo_from_slug("owner/repo"), number=7)) == node
    # The read is PINNED to the PrId's repo (ADR-0030) — never a cwd inference.
    assert "--repo" in calls[0] and "owner/repo" in calls[0]


def test_the_tuple_returning_repo_slug_is_gone():
    """PROC03 review rule: the tuple-shaped repo read is deleted (no alias, no
    fallback) — the typed `current_repo()` is the one repo read."""
    assert not hasattr(gh, "repo_slug")
