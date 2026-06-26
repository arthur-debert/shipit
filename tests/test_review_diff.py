"""Tests for `shipit.review.diff` — PR resolution + workdir normalization.

The full `resolve_pr` shells out to git + `gh`; here we cover the workdir
normalization seam (`_git_toplevel`) and that `resolve_pr` anchors the agent's
cwd to the repo root even when invoked from a nested subdir, with the git/gh
boundary stubbed.
"""

from __future__ import annotations

import pytest

from shipit.review import diff


def test_git_toplevel_returns_repo_root(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)

        class R:
            returncode = 0
            stdout = "/repo/root\n"

        return R()

    monkeypatch.setattr(diff.proc, "run", fake_run)
    assert diff._git_toplevel("/repo/root/src/deep") == "/repo/root"
    assert calls[0][:2] == ["git", "-C"]
    assert "--show-toplevel" in calls[0]


def test_git_toplevel_none_outside_checkout(monkeypatch):
    class R:
        returncode = 128
        stdout = ""

    monkeypatch.setattr(diff.proc, "run", lambda cmd, **kw: R())
    assert diff._git_toplevel("/tmp/not-a-repo") is None


def test_resolve_pr_normalizes_workdir_to_toplevel(monkeypatch):
    """`resolve_pr` invoked from a nested subdir resolves the diff (and the
    agent's cwd) against the repo ROOT, not the subdir."""
    monkeypatch.setattr(diff, "_git_toplevel", lambda wd: "/repo/root")
    monkeypatch.setattr(
        diff.gh,
        "pr_view",
        lambda *a, **k: (
            '{"number": 5, "headRefName": "feat", '
            '"headRefOid": "headsha", "baseRefName": "main", "baseRefOid": "basesha"}'
        ),
    )
    monkeypatch.setattr(diff, "_sha_present", lambda wd, sha: True)

    seen_workdirs: list[str] = []
    seen_diff_specs: list[str] = []

    def fake_git(workdir, args, *, check=True):
        seen_workdirs.append(workdir)
        if args[:1] == ["diff"]:
            seen_diff_specs.append(args[-1])

        class R:
            returncode = 0
            stdout = "the diff\n"

        return R()

    monkeypatch.setattr(diff, "_git", fake_git)

    ctx = diff.resolve_pr(5, workdir="/repo/root/src/deep")
    assert ctx.workdir == "/repo/root"
    # The diff is computed against the authoritative base sha (baseRefOid), not a
    # local `origin/<base>` ref.
    assert ctx.base_sha == "basesha"
    assert seen_diff_specs == ["basesha...headsha", "basesha...headsha"]
    # Every git invocation ran against the toplevel, not the nested subdir.
    assert set(seen_workdirs) == {"/repo/root"}


def test_resolve_pr_missing_base_oid_fails_loud(monkeypatch):
    """A `gh pr view` with no baseRefOid fails loud — the resolver never guesses
    a base, so the review can't run against a wrong one."""
    monkeypatch.setattr(diff, "_git_toplevel", lambda wd: "/repo/root")
    monkeypatch.setattr(
        diff.gh,
        "pr_view",
        lambda *a, **k: (
            '{"number": 5, "headRefName": "feat", '
            '"headRefOid": "headsha", "baseRefName": "main"}'
        ),
    )
    monkeypatch.setattr(diff, "_sha_present", lambda wd, sha: True)
    monkeypatch.setattr(diff, "_git", lambda *a, **k: None)
    with pytest.raises(diff.ReviewError, match="no base sha"):
        diff.resolve_pr(5, workdir="/repo/root")


def test_resolve_pr_stale_base_fetch_fails_loud(monkeypatch):
    """When the base sha (baseRefOid) can't be made present — a stale/missing
    `origin/<base>` and an unfetchable sha — resolve_pr fails loud instead of
    silently degrading to a local ref or the base tip (no wrong-base diff)."""
    monkeypatch.setattr(diff, "_git_toplevel", lambda wd: "/repo/root")
    monkeypatch.setattr(
        diff.gh,
        "pr_view",
        lambda *a, **k: (
            '{"number": 5, "headRefName": "feat", "headRefOid": "headsha", '
            '"baseRefName": "main", "baseRefOid": "basesha"}'
        ),
    )
    # The head is present; the base sha never becomes present (every fetch is a
    # no-op — the classic stale/missing `origin/main`).
    monkeypatch.setattr(diff, "_sha_present", lambda wd, sha: sha == "headsha")

    diff_attempted = False

    def fake_git(workdir, args, *, check=True):
        nonlocal diff_attempted
        if args[:1] == ["diff"]:
            diff_attempted = True

        class R:
            returncode = 0
            stdout = ""

        return R()

    monkeypatch.setattr(diff, "_git", fake_git)

    with pytest.raises(diff.ReviewError, match="base basesha"):
        diff.resolve_pr(5, workdir="/repo/root")
    # It failed BEFORE computing any diff — never produced a wrong-base diff.
    assert diff_attempted is False


def test_resolve_pr_rejects_non_checkout(monkeypatch):
    monkeypatch.setattr(diff, "_git_toplevel", lambda wd: None)
    with pytest.raises(diff.ReviewError, match="not a git checkout"):
        diff.resolve_pr(5, workdir="/tmp/nope")
