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


def test_merge_paginated_flattens_concatenated_arrays():
    out = '[{"id": 1}, {"id": 2}]\n[{"id": 3}]\n'
    assert [o["id"] for o in gh._merge_paginated(out)] == [1, 2, 3]


def test_merge_paginated_single_page():
    assert gh._merge_paginated('[{"id": 1}]') == [{"id": 1}]


def test_pagination_helper_exists_exactly_once():
    definitions = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_merge_paginated":
                definitions.append(path.relative_to(_SRC_ROOT.parent))
    assert definitions == [pathlib.Path("shipit/gh.py")]


def _probe_result(monkeypatch, *, rc: int, stderr: str = ""):
    from shipit import execrun

    calls: list[list[str]] = []

    def fake_probe(args, **kwargs):
        calls.append(list(args))
        return execrun.ExecResult(
            argv=tuple(args), rc=rc, stdout="", stderr=stderr, duration_ms=0
        )

    monkeypatch.setattr(gh, "_run_probe", fake_probe)
    return calls


def test_workflow_ref_resolves_true_on_probe_success(monkeypatch):
    calls = _probe_result(monkeypatch, rc=0)
    assert gh.workflow_ref_resolves("o/r", "v1") is True
    assert calls == [["gh", "api", "repos/o/r/commits/v1"]]


def test_workflow_ref_resolves_false_only_on_a_confirmed_404(monkeypatch):
    _probe_result(monkeypatch, rc=1, stderr="gh: Not Found (HTTP 404)")
    assert gh.workflow_ref_resolves("o/r", "v9") is False


def test_workflow_ref_resolves_false_on_no_commit_422(monkeypatch):
    _probe_result(
        monkeypatch,
        rc=1,
        stderr="gh: No commit found for SHA: deadbeef (HTTP 422)",
    )
    assert gh.workflow_ref_resolves("o/r", "deadbeef") is False


def test_workflow_ref_resolves_true_when_404_appears_only_incidentally(monkeypatch):
    _probe_result(
        monkeypatch, rc=1, stderr="gh: API rate limit exceeded (404 of 5000 left)"
    )
    assert gh.workflow_ref_resolves("o/r", "v1") is True


def test_workflow_ref_resolves_true_on_unknown_probe_failure(monkeypatch, caplog):
    _probe_result(monkeypatch, rc=1, stderr="gh: authentication required")
    with caplog.at_level("WARNING", logger="shipit.gh"):
        assert gh.workflow_ref_resolves("o/r", "v1") is True
    assert any("degraded" in rec.message for rec in caplog.records)


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
    calls = _capture_run(monkeypatch, "{}")
    with pytest.raises(ValueError):
        gh.rest("repos/o/r", method="POST", body={"a": 1}, fields={"b": "2"})
    assert calls == []


def test_graphql_variable_encoding(monkeypatch):
    calls = _capture_run(monkeypatch, json.dumps({"data": {"ok": True}}))
    assert gh.graphql("query {}", owner="o", pr=7, after=None) == {"ok": True}
    assert calls == [
        ["gh", "api", "graphql", "-f", "query=query {}", "-f", "owner=o", "-F", "pr=7"]
    ]


def test_graphql_errors_raise_the_semantic_error(monkeypatch):
    payload = {"data": None, "errors": [{"message": "Could not resolve PR"}]}
    _capture_run(monkeypatch, json.dumps(payload))
    with pytest.raises(PrStateError):
        gh.graphql("query {}")


def test_current_repo_returns_the_typed_repo(monkeypatch):
    calls = _capture_run(monkeypatch, "Acme/Widget\n")
    repo = gh.current_repo()
    assert isinstance(repo, Repo)
    assert repo == repo_from_slug("acme/widget")
    assert repo.slug == "acme/widget"
    assert calls == [
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]
    ]


def test_current_repo_raises_on_unusable_output(monkeypatch):
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


def test_repository_dispatch_posts_the_event_and_payload(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = list(args)
        captured["input_text"] = kwargs.get("input_text")
        captured["token"] = kwargs.get("token")
        return ""

    monkeypatch.setattr(gh, "_run", fake_run)
    gh.repository_dispatch(
        "lex-fmt/vscode",
        event_type="upstream-release",
        payload={"tag": "v1.2.3", "repo": "lex-fmt/tree-sitter-lex"},
        token="pat-xyz",
    )
    assert captured["args"] == [
        "gh",
        "api",
        "repos/lex-fmt/vscode/dispatches",
        "--method",
        "POST",
        "--input",
        "-",
    ]
    assert captured["token"] == "pat-xyz"
    assert json.loads(captured["input_text"]) == {
        "event_type": "upstream-release",
        "client_payload": {"tag": "v1.2.3", "repo": "lex-fmt/tree-sitter-lex"},
    }


def test_pr_view_returns_the_parsed_object(monkeypatch):
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
    assert "--repo" in calls[0] and "owner/repo" in calls[0]


def test_the_tuple_returning_repo_slug_is_gone():
    assert not hasattr(gh, "repo_slug")
