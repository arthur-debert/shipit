"""Local eval store: records append as JSONL, keyed by `Repo` IDENTITY, and land
OUTSIDE the repo tree.

The store is keyed by the repo's origin ``owner/name`` identity (ADR-0024) — NOT
its filesystem path — and rooted under a platformdirs *state* dir, never inside any
working tree, so process telemetry can never dirty product history (the HAR02
"local, never committed" contract). The load-bearing property this file pins is the
scatter-bug fix: two clones of one repo at different paths share ONE store file.
"""

from __future__ import annotations

import json

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


def test_store_path_is_outside_the_repo_tree(tmp_path):
    base = tmp_path / "state"
    path = store.append_record({"x": 1}, _repo(), base_dir=base)
    # The record must live under the injected state root, never a repo working tree.
    assert base in path.parents


def test_default_store_dir_is_under_platformdirs_state(monkeypatch, tmp_path):
    monkeypatch.setattr(
        store.platformdirs, "user_state_dir", lambda *a, **k: str(tmp_path / "ps")
    )
    assert store.store_dir() == tmp_path / "ps" / "eval"


def test_distinct_repos_get_distinct_store_files(tmp_path):
    base = tmp_path / "state"
    a = store.store_path(_repo(name="repo-a"), base_dir=base)
    b = store.store_path(_repo(name="repo-b"), base_dir=base)
    assert a != b
    assert a.suffix == ".jsonl"


def test_repo_key_is_the_nested_owner_name_identity_path():
    # The key is a nested ``<owner>/<name>`` path (the logsetup-proven origin scheme),
    # NOT a flat ``owner-name`` join — the ``/`` is the collision-free separator that
    # neither a GitHub owner login nor a repo name may contain.
    key = store.repo_key(_repo(owner="arthur-debert", name="shipit"))
    assert key == "arthur-debert/shipit"


def test_repo_key_does_not_collide_across_hyphen_ambiguous_repos(tmp_path):
    # REGRESSION for the flat ``owner-name`` collision: owner ``a-b`` + name ``c`` and
    # owner ``a`` + name ``b-c`` both flatten to ``a-b-c`` and would MERGE two distinct
    # repos' records into one file. The nested-path key keeps them distinct.
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
    # Case-fragmentation regression: GitHub owner/repo are case-INSENSITIVE, so a
    # clone whose origin reads `Acme/Widget` and one reading `acme/widget` are ONE
    # repo. `resolve_repo` lowercases to the canonical identity (test_identity), so
    # both resolve to the SAME store key/file here — the store never fragments per
    # origin case.
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
    # THE scatter-bug regression: the store keys by origin identity, not by clone
    # path — so two Trees/clones of the same repo (constructed identically, standing
    # in for two different filesystem checkouts) resolve to ONE store file, and a
    # repo's runs pool instead of orphaning a fresh store per clone.
    base = tmp_path / "state"
    clone_a = _repo()  # e.g. checked out at /trees/x/widget
    clone_b = _repo()  # e.g. checked out at /home/y/widget
    pa = store.append_record({"run": "a"}, clone_a, base_dir=base)
    pb = store.append_record({"run": "b"}, clone_b, base_dir=base)
    assert pa == pb
    records = [json.loads(line) for line in pa.read_text().splitlines()]
    assert records == [{"run": "a"}, {"run": "b"}]


def test_ownerkind_enrichment_does_not_move_the_store_key(tmp_path):
    # OwnerKind is excluded from Repo identity, so enriching it must NOT change the
    # store key — the same repo's records stay in one file before and after the kind
    # is known (ADR-0024: "same store key before and after enrichment").
    base = tmp_path / "state"
    bare = _repo(kind=None)
    enriched = _repo(kind=OwnerKind.ORGANIZATION)
    assert store.repo_key(bare) == store.repo_key(enriched)
    assert store.store_path(bare, base_dir=base) == store.store_path(
        enriched, base_dir=base
    )
