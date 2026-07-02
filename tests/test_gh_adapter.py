"""The ONE gh Tool adapter (PROC02-WS01, ADR-0028).

The engine's second boundary (`shipit/prstate/ghapi.py`) merged into
`shipit.gh`: pure-logic tests for the merged surface (no subprocess), plus the
mechanical sweeps that pin the merge's two structural guarantees —

- gh argv is built ONLY inside the adapter ("tool argv built outside its Tool
  adapter" is a statable defect, ADR-0028); and
- the pagination-merging helper exists exactly once.
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest

import shipit
from shipit import gh
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


# --- gh argv is built only inside the adapter --------------------------------


def test_no_gh_argv_outside_the_adapter():
    """ADR-0028: any list/tuple argv literal starting with "gh" outside
    `shipit/gh.py` is a review defect — the grep-clean criterion, pinned."""
    offenders = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if path == _SRC_ROOT / "gh.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.List, ast.Tuple))
                and node.elts
                and isinstance(node.elts[0], ast.Constant)
                and node.elts[0].value == "gh"
            ):
                offenders.append(f"{path.relative_to(_SRC_ROOT.parent)}:{node.lineno}")
    assert not offenders, "gh argv built outside the adapter:\n" + "\n".join(offenders)


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
