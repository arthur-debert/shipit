from __future__ import annotations

import json
import logging
import subprocess
import sys

from shipit.harness.eval import store
from shipit.identity import Owner, OwnerKind, Repo


def _repo(owner="acme", name="widget", kind=None):
    return Repo(owner=Owner(login=owner, kind=kind), name=name)


def test_append_writes_jsonl_lines_keyed_by_repo(tmp_path):
    base = tmp_path / "state"
    repo = _repo()
    path = store.append_record({"a": 1}, repo, base_dir=base)
    store.append_record({"a": 2}, repo, base_dir=base)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [{"a": 1}, {"a": 2}]


def test_read_records_round_trips_append_order(tmp_path):
    base = tmp_path / "state"
    repo = _repo()
    store.append_record({"a": 1}, repo, base_dir=base)
    store.append_record({"a": 2}, repo, base_dir=base)
    assert store.read_records(repo, base_dir=base) == [{"a": 1}, {"a": 2}]


def test_read_records_missing_store_is_empty(tmp_path):
    assert store.read_records(_repo(), base_dir=tmp_path / "state") == []


def test_read_records_skips_a_corrupt_line_loudly(tmp_path, caplog):
    base = tmp_path / "state"
    repo = _repo()
    path = store.append_record({"a": 1}, repo, base_dir=base)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    store.append_record({"a": 2}, repo, base_dir=base)
    with caplog.at_level(logging.WARNING, logger="shipit.harness"):
        assert store.read_records(repo, base_dir=base) == [{"a": 1}, {"a": 2}]
    warning = "\n".join(r.getMessage() for r in caplog.records)
    assert str(path) in warning
    assert "line 2" in warning


def test_read_records_warns_on_a_non_object_line(tmp_path, caplog):
    base = tmp_path / "state"
    repo = _repo()
    path = store.append_record({"a": 1}, repo, base_dir=base)
    with path.open("a", encoding="utf-8") as fh:
        fh.write('["not", "a", "record"]\n')
    with caplog.at_level(logging.WARNING, logger="shipit.harness"):
        assert store.read_records(repo, base_dir=base) == [{"a": 1}]
    warning = "\n".join(r.getMessage() for r in caplog.records)
    assert str(path) in warning
    assert "line 2" in warning
    assert "list" in warning


def test_intact_reads_emit_no_malformed_warning(tmp_path, caplog):
    base = tmp_path / "state"
    repo = _repo()
    store.append_record({"a": 1}, repo, base_dir=base)
    with caplog.at_level(logging.WARNING, logger="shipit.harness"):
        assert store.read_records(repo, base_dir=base) == [{"a": 1}]
    assert not caplog.records


_APPENDER = """
import sys
from pathlib import Path

from shipit.harness.eval import store
from shipit.identity import Owner, Repo

base, who, count = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
repo = Repo(owner=Owner(login="acme"), name="widget")
payload = who * 65536
for i in range(count):
    store.append_record({"who": who, "i": i, "payload": payload}, repo, base_dir=base)
"""


def test_concurrent_appends_from_separate_processes_do_not_corrupt(tmp_path):
    base = tmp_path / "state"
    writers, appends_each = 4, 8
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _APPENDER, str(base), who, str(appends_each)],
        )
        for who in ("a", "b", "c", "d")
    ]
    for proc in procs:
        assert proc.wait(timeout=120) == 0

    records = store.read_records(_repo(), base_dir=base)
    assert len(records) == writers * appends_each
    seen = set()
    for record in records:
        assert record["payload"] == record["who"] * 65536
        seen.add((record["who"], record["i"]))
    assert seen == {
        (who, i) for who in ("a", "b", "c", "d") for i in range(appends_each)
    }


def test_store_path_is_outside_the_repo_tree(tmp_path):
    base = tmp_path / "state"
    path = store.append_record({"x": 1}, _repo(), base_dir=base)
    assert base in path.parents


def test_default_store_dir_is_under_platformdirs_state(monkeypatch, tmp_path):
    monkeypatch.setattr(
        store.platformdirs, "user_state_dir", lambda *a, **k: str(tmp_path / "ps")
    )
    assert store.store_dir() == tmp_path / "ps" / "eval"


def test_kinds_are_sibling_subdirs_of_one_family_root(monkeypatch, tmp_path):
    monkeypatch.setattr(
        store.platformdirs, "user_state_dir", lambda *a, **k: str(tmp_path / "ps")
    )
    assert store.store_dir(kind=store.EVAL_KIND) == tmp_path / "ps" / "eval"
    assert (
        store.store_dir(kind=store.REVIEW_ROUNDS_KIND)
        == tmp_path / "ps" / "review-rounds"
    )


def test_kinds_never_share_a_store_file(tmp_path):
    base = tmp_path / "state"
    repo = _repo()
    eval_path = store.append_record({"kind": "eval"}, repo, base_dir=base)
    round_path = store.append_record(
        {"kind": "round"}, repo, base_dir=base, kind=store.REVIEW_ROUNDS_KIND
    )
    assert eval_path != round_path
    assert base in eval_path.parents
    assert base in round_path.parents
    assert json.loads(eval_path.read_text()) == {"kind": "eval"}
    assert json.loads(round_path.read_text()) == {"kind": "round"}


def test_distinct_repos_get_distinct_store_files(tmp_path):
    base = tmp_path / "state"
    a = store.store_path(_repo(name="repo-a"), base_dir=base)
    b = store.store_path(_repo(name="repo-b"), base_dir=base)
    assert a != b
    assert a.suffix == ".jsonl"


def test_repo_key_is_the_nested_owner_name_identity_path():
    key = store.repo_key(_repo(owner="arthur-debert", name="shipit"))
    assert key == "arthur-debert/shipit"


def test_repo_key_does_not_collide_across_hyphen_ambiguous_repos(tmp_path):
    base = tmp_path / "state"
    left = _repo(owner="a-b", name="c")
    right = _repo(owner="a", name="b-c")
    assert store.repo_key(left) != store.repo_key(right)
    lp = store.append_record({"who": "left"}, left, base_dir=base)
    rp = store.append_record({"who": "right"}, right, base_dir=base)
    assert lp != rp
    assert [json.loads(x) for x in lp.read_text().splitlines()] == [{"who": "left"}]
    assert [json.loads(x) for x in rp.read_text().splitlines()] == [{"who": "right"}]


def test_case_varying_origins_of_one_repo_share_one_store_file(tmp_path):
    from shipit.identity import Owner, Repo, resolve_repo

    class _FakeGit:
        def __init__(self, url):
            self._url = url

        def remote_url(self, *, cwd, remote="origin"):
            return self._url

    base = tmp_path / "state"
    mixed = resolve_repo(".", boundary=_FakeGit("git@github.com:Acme/Widget.git"))
    lower = resolve_repo(".", boundary=_FakeGit("https://github.com/acme/widget"))
    assert mixed == lower == Repo(owner=Owner("acme"), name="widget")
    assert store.repo_key(mixed) == store.repo_key(lower)
    pa = store.append_record({"run": "mixed"}, mixed, base_dir=base)
    pb = store.append_record({"run": "lower"}, lower, base_dir=base)
    assert pa == pb
    records = [json.loads(line) for line in pa.read_text().splitlines()]
    assert records == [{"run": "mixed"}, {"run": "lower"}]


def test_two_clone_paths_of_one_repo_share_one_store_file(tmp_path):
    base = tmp_path / "state"
    clone_a = _repo()
    clone_b = _repo()
    pa = store.append_record({"run": "a"}, clone_a, base_dir=base)
    pb = store.append_record({"run": "b"}, clone_b, base_dir=base)
    assert pa == pb
    records = [json.loads(line) for line in pa.read_text().splitlines()]
    assert records == [{"run": "a"}, {"run": "b"}]


def test_ownerkind_enrichment_does_not_move_the_store_key(tmp_path):
    base = tmp_path / "state"
    bare = _repo(kind=None)
    enriched = _repo(kind=OwnerKind.ORGANIZATION)
    assert store.repo_key(bare) == store.repo_key(enriched)
    assert store.store_path(bare, base_dir=base) == store.store_path(
        enriched, base_dir=base
    )
