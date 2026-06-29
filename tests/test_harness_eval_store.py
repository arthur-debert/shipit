"""Local eval store: records append as JSONL and land OUTSIDE the repo tree.

The store is keyed by repo and rooted under a platformdirs *state* dir — never
inside any working tree — so process telemetry can never dirty product history
(the HAR02 "local, never committed" contract).
"""

from __future__ import annotations

import json

from shipit.harness.eval import store


def test_append_writes_jsonl_lines_keyed_by_repo(tmp_path):
    base = tmp_path / "state"
    repo = tmp_path / "repo"
    path = store.append_record({"a": 1}, repo, base_dir=base)
    store.append_record({"a": 2}, repo, base_dir=base)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [{"a": 1}, {"a": 2}]


def test_store_path_is_outside_the_repo_tree(tmp_path):
    base = tmp_path / "state"
    repo = tmp_path / "repo"
    path = store.append_record({"x": 1}, repo, base_dir=base)
    # The record must NOT live anywhere under the repo working tree.
    assert base in path.parents
    assert repo not in path.parents


def test_default_store_dir_is_under_platformdirs_state(monkeypatch, tmp_path):
    monkeypatch.setattr(
        store.platformdirs, "user_state_dir", lambda *a, **k: str(tmp_path / "ps")
    )
    assert store.store_dir() == tmp_path / "ps" / "eval"


def test_distinct_repos_get_distinct_store_files(tmp_path):
    base = tmp_path / "state"
    a = store.store_path(tmp_path / "repo-a", base_dir=base)
    b = store.store_path(tmp_path / "repo-b", base_dir=base)
    assert a != b
    assert a.suffix == ".jsonl"


def test_repo_key_is_a_path_slug():
    key = store.repo_key("/Users/x/h/shipit")
    assert "/" not in key
    assert key.endswith("shipit")
