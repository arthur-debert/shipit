from __future__ import annotations

import json

import pytest

from shipit.identity import Sha, repo_from_slug
from shipit.review import diff, post

HEAD = "cafe" * 10
BASE = "beef" * 10
MERGE_BASE = Sha("ba5e" * 10)


def _present_recording(seen: list):

    def fake(sha, *, cwd):
        seen.append(cwd)
        return True

    return fake


def _merge_base_recording(seen: list):

    def fake(a, b, *, cwd):
        seen.append(cwd)
        return MERGE_BASE

    return fake


def test_git_toplevel_returns_repo_root(monkeypatch):
    calls: list[str] = []

    def fake_repo_root(*, cwd):
        calls.append(cwd)
        return "/repo/root"

    monkeypatch.setattr(diff.git, "repo_root", fake_repo_root)
    assert diff._git_toplevel("/repo/root/src/deep") == "/repo/root"
    assert calls == ["/repo/root/src/deep"]


def test_git_toplevel_none_outside_checkout(monkeypatch):
    monkeypatch.setattr(diff.git, "repo_root", lambda *, cwd: None)
    assert diff._git_toplevel("/tmp/not-a-repo") is None


def test_resolve_pr_normalizes_workdir_to_toplevel(monkeypatch):
    monkeypatch.setattr(diff, "_git_toplevel", lambda wd: "/repo/root")
    monkeypatch.setattr(
        diff.gh,
        "pr_view",
        lambda *a, **k: json.loads(
            '{"number": 5, "isDraft": false, "mergeStateStatus": "CLEAN", "headRefName": "feat", '
            f'"headRefOid": "{HEAD}", "baseRefName": "main", "baseRefOid": "{BASE}"}}'
        ),
    )
    monkeypatch.setattr(diff.git, "commit_present", _present_recording(seen := []))

    seen_diff_specs: list[tuple[Sha, Sha]] = []

    def fake_diff_range(base, head, *, cwd):
        seen.append(cwd)
        seen_diff_specs.append((base, head))
        return "the diff\n"

    def fake_diff_names(base, head, *, cwd):
        seen.append(cwd)
        seen_diff_specs.append((base, head))
        return ["a.py"]

    monkeypatch.setattr(diff.git, "merge_base", _merge_base_recording(seen))
    monkeypatch.setattr(diff.git, "diff_range", fake_diff_range)
    monkeypatch.setattr(diff.git, "diff_name_only", fake_diff_names)

    ctx = diff.resolve_pr(5, workdir="/repo/root/src/deep")
    assert ctx.workdir == "/repo/root"
    assert ctx.base_sha == Sha(BASE)
    assert isinstance(ctx.base_sha, Sha)
    assert seen_diff_specs == [(MERGE_BASE, Sha(HEAD)), (MERGE_BASE, Sha(HEAD))]
    assert all(isinstance(end, Sha) for spec in seen_diff_specs for end in spec)
    assert set(seen) == {"/repo/root"}


def test_resolve_pr_omitted_repo_canonicalizes_via_gh_not_alias_origin(monkeypatch):
    monkeypatch.setattr(diff, "_git_toplevel", lambda wd: "/repo/root")
    monkeypatch.setattr(
        diff.gh,
        "pr_view",
        lambda *a, **k: json.loads(
            '{"number": 5, "isDraft": false, "mergeStateStatus": "CLEAN", '
            f'"headRefName": "feat", "headRefOid": "{HEAD}", '
            f'"baseRefName": "main", "baseRefOid": "{BASE}"}}'
        ),
    )
    monkeypatch.setattr(diff.git, "commit_present", lambda sha, *, cwd: True)
    monkeypatch.setattr(diff.git, "merge_base", lambda a, b, *, cwd: MERGE_BASE)
    monkeypatch.setattr(diff.git, "diff_range", lambda base, head, *, cwd: "the diff\n")
    monkeypatch.setattr(diff.git, "diff_name_only", lambda base, head, *, cwd: [])

    monkeypatch.setattr(
        diff.gh,
        "current_repo",
        lambda **k: repo_from_slug("alias-owner/alias-repo"),
        raising=False,
    )

    ctx = diff.resolve_pr(5, workdir="/repo/root")
    assert ctx.repo is None

    monkeypatch.setattr(
        post.gh,
        "current_repo",
        lambda **k: repo_from_slug("canonical-owner/canonical-repo"),
    )
    assert post._resolve_repo(ctx) == "canonical-owner/canonical-repo"


def test_resolve_pr_no_common_ancestor_fails_loud(monkeypatch):
    monkeypatch.setattr(diff, "_git_toplevel", lambda wd: "/repo/root")
    monkeypatch.setattr(
        diff.gh,
        "pr_view",
        lambda *a, **k: json.loads(
            f'{{"number": 5, "isDraft": false, "mergeStateStatus": "CLEAN", "headRefName": "feat", "headRefOid": "{HEAD}", '
            f'"baseRefName": "main", "baseRefOid": "{BASE}"}}'
        ),
    )
    monkeypatch.setattr(diff.git, "commit_present", lambda sha, *, cwd: True)
    monkeypatch.setattr(diff.git, "merge_base", lambda a, b, *, cwd: None)

    diff_attempted = False

    def fake_diff_range(base, head, *, cwd):
        nonlocal diff_attempted
        diff_attempted = True
        return ""

    monkeypatch.setattr(diff.git, "diff_range", fake_diff_range)
    monkeypatch.setattr(diff.git, "diff_name_only", lambda base, head, *, cwd: [])

    with pytest.raises(diff.ReviewError, match="no common ancestor"):
        diff.resolve_pr(5, workdir="/repo/root")
    assert diff_attempted is False


def test_resolve_pr_missing_base_oid_fails_loud(monkeypatch):
    monkeypatch.setattr(diff, "_git_toplevel", lambda wd: "/repo/root")
    monkeypatch.setattr(
        diff.gh,
        "pr_view",
        lambda *a, **k: json.loads(
            '{"number": 5, "isDraft": false, "mergeStateStatus": "CLEAN", "headRefName": "feat", '
            f'"headRefOid": "{HEAD}", "baseRefName": "main"}}'
        ),
    )
    monkeypatch.setattr(diff.git, "commit_present", lambda sha, *, cwd: True)
    with pytest.raises(diff.ReviewError, match="no base sha"):
        diff.resolve_pr(5, workdir="/repo/root")


def test_resolve_pr_malformed_base_oid_fails_loud(monkeypatch):
    monkeypatch.setattr(diff, "_git_toplevel", lambda wd: "/repo/root")
    monkeypatch.setattr(
        diff.gh,
        "pr_view",
        lambda *a, **k: json.loads(
            '{"number": 5, "isDraft": false, "mergeStateStatus": "CLEAN", "headRefName": "feat", '
            f'"headRefOid": "{HEAD}", "baseRefName": "main", "baseRefOid": "not-a-sha"}}'
        ),
    )
    monkeypatch.setattr(diff.git, "commit_present", lambda sha, *, cwd: True)
    with pytest.raises(diff.ReviewError, match="unusable base sha"):
        diff.resolve_pr(5, workdir="/repo/root")


def test_resolve_pr_stale_base_fetch_fails_loud(monkeypatch):
    monkeypatch.setattr(diff, "_git_toplevel", lambda wd: "/repo/root")
    monkeypatch.setattr(
        diff.gh,
        "pr_view",
        lambda *a, **k: json.loads(
            f'{{"number": 5, "isDraft": false, "mergeStateStatus": "CLEAN", "headRefName": "feat", "headRefOid": "{HEAD}", '
            f'"baseRefName": "main", "baseRefOid": "{BASE}"}}'
        ),
    )
    monkeypatch.setattr(
        diff.git, "commit_present", lambda sha, *, cwd: sha == Sha(HEAD)
    )
    fetched: list = []

    def fake_fetch_ref(refspec, *, cwd):
        fetched.append(refspec)
        return False

    monkeypatch.setattr(diff.git, "fetch_ref", fake_fetch_ref)

    diff_attempted = False

    def fake_diff_range(base, head, *, cwd):
        nonlocal diff_attempted
        diff_attempted = True
        return ""

    monkeypatch.setattr(diff.git, "diff_range", fake_diff_range)
    monkeypatch.setattr(diff.git, "merge_base", lambda a, b, *, cwd: MERGE_BASE)
    monkeypatch.setattr(diff.git, "diff_name_only", lambda base, head, *, cwd: [])

    with pytest.raises(diff.ReviewError, match=f"base {BASE}"):
        diff.resolve_pr(5, workdir="/repo/root")
    assert diff_attempted is False
    assert fetched == ["main", BASE]
    assert all(isinstance(r, str) for r in fetched)


def test_resolve_pr_rejects_non_checkout(monkeypatch):
    monkeypatch.setattr(diff, "_git_toplevel", lambda wd: None)
    with pytest.raises(diff.ReviewError, match="not a git checkout"):
        diff.resolve_pr(5, workdir="/tmp/nope")


def test_review_view_repo_is_slug_when_known():
    ctx = diff.review_view(
        number=5,
        repo="owner/repo",
        head_sha="ab" * 20,
        base_ref="main",
        base_sha="ba" * 20,
        diff="",
        is_draft=False,
    )
    assert ctx.repo == "owner/repo"
    assert ctx.base_sha == Sha("ba" * 20)
    assert isinstance(ctx.base_sha, Sha)


def test_review_view_repo_is_none_for_handbuilt_context():
    ctx = diff.review_view(
        number=5,
        repo=None,
        head_sha="ab" * 20,
        base_ref="main",
        base_sha="ba" * 20,
        diff="",
        is_draft=False,
    )
    assert ctx.repo is None


def test_rescoped_view_rediffs_over_the_fix_range(monkeypatch):
    view = diff.review_view(
        number=5,
        repo="owner/repo",
        head_sha="c" * 40,
        base_ref="main",
        base_sha="a" * 40,
        diff="full pr diff",
        is_draft=False,
        changed_files=["a.py", "b.py"],
        workdir="/wd",
        head_ref="feature/x",
    )
    seen = {}

    def fake_diff_range(base, head, *, cwd):
        seen["range"] = (str(base), str(head), cwd)
        return "fix range diff"

    monkeypatch.setattr(diff.git, "diff_range", fake_diff_range)
    monkeypatch.setattr(diff.git, "diff_name_only", lambda base, head, *, cwd: ["b.py"])

    rescoped = diff.rescoped_view(view, "b" * 40)
    assert rescoped.base_sha == Sha("b" * 40)
    assert rescoped.diff == "fix range diff"
    assert rescoped.changed_files == ["b.py"]
    assert seen["range"] == ("b" * 40, "c" * 40, "/wd")
    assert rescoped.head_sha == Sha("c" * 40)
    assert rescoped.number == 5
    assert rescoped.repo == "owner/repo"
    assert rescoped.head_ref == "feature/x"


def test_rescoped_view_wraps_a_git_failure_in_review_error(monkeypatch):
    view = diff.review_view(
        number=5,
        repo="owner/repo",
        head_sha="c" * 40,
        base_ref="main",
        base_sha="a" * 40,
        diff="full pr diff",
        is_draft=False,
        changed_files=["a.py"],
        workdir="/wd",
        head_ref="feature/x",
    )

    def boom(base, head, *, cwd):
        raise diff.execrun.ExecError(["git", "diff"], rc=1, stderr="fatal: bad object")

    monkeypatch.setattr(diff.git, "diff_range", boom)
    with pytest.raises(diff.ReviewError) as excinfo:
        diff.rescoped_view(view, "b" * 40)
    message = str(excinfo.value)
    assert "failed to compute the incremental fix-range diff" in message
    assert "#5" in message
    assert f"{'b' * 40}..{'c' * 40}" in message
