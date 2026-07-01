"""Tests for `shipit.review.diff` — PR resolution + workdir normalization.

The full `resolve_pr` shells out to git + `gh`; here we cover the workdir
normalization seam (`_git_toplevel`) and that `resolve_pr` anchors the agent's
cwd to the repo root even when invoked from a nested subdir, with the git/gh
boundary stubbed.
"""

from __future__ import annotations

import pytest

from shipit.review import diff, post


def test_git_toplevel_returns_repo_root(monkeypatch):
    # Routes through the single `gh.repo_root(cwd=...)` boundary (ADR-0024), passing
    # the workdir through as `cwd` and returning its resolved toplevel.
    calls: list[str] = []

    def fake_repo_root(*, cwd):
        calls.append(cwd)
        return "/repo/root"

    monkeypatch.setattr(diff.gh, "repo_root", fake_repo_root)
    assert diff._git_toplevel("/repo/root/src/deep") == "/repo/root"
    assert calls == ["/repo/root/src/deep"]


def test_git_toplevel_none_outside_checkout(monkeypatch):
    # The boundary returns None outside a checkout; `_git_toplevel` passes it through.
    monkeypatch.setattr(diff.gh, "repo_root", lambda *, cwd: None)
    assert diff._git_toplevel("/tmp/not-a-repo") is None


def test_resolve_pr_normalizes_workdir_to_toplevel(monkeypatch):
    """`resolve_pr` invoked from a nested subdir resolves the diff (and the
    agent's cwd) against the repo ROOT, not the subdir."""
    monkeypatch.setattr(diff, "_git_toplevel", lambda wd: "/repo/root")
    monkeypatch.setattr(
        diff.gh,
        "pr_view",
        lambda *a, **k: (
            '{"number": 5, "isDraft": false, "mergeStateStatus": "CLEAN", "headRefName": "feat", '
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
            stdout = "mergebasesha\n" if args[:1] == ["merge-base"] else "the diff\n"

        return R()

    monkeypatch.setattr(diff, "_git", fake_git)

    ctx = diff.resolve_pr(5, workdir="/repo/root/src/deep")
    assert ctx.workdir == "/repo/root"
    # The ReviewView base is the authoritative base sha (baseRefOid), not a local
    # `origin/<base>` ref.
    assert ctx.base_sha == "basesha"
    # The diff endpoint is the MERGE BASE of the authoritative base + head (the PR
    # branch point) — GitHub's three-dot diff — computed explicitly, not the raw
    # base tip.
    assert seen_diff_specs == ["mergebasesha...headsha", "mergebasesha...headsha"]
    # Every git invocation ran against the toplevel, not the nested subdir.
    assert set(seen_workdirs) == {"/repo/root"}


def test_resolve_pr_omitted_repo_canonicalizes_via_gh_not_alias_origin(monkeypatch):
    """Regression (codex ERROR): with `--repo` OMITTED, `resolve_pr` must NOT adopt
    the checkout's (possibly stale/alias) origin slug as the authoritative
    ``ctx.repo``. `identity.resolve_repo` is deliberately offline/Tree-safe and does
    NOT follow GitHub's 307, so a checkout whose ``origin`` still points at an
    old/transferred slug would make downstream POST reviews / mint app-auth against
    the ALIAS (which 307s on write). The fix keeps the locally-derived slug
    non-authoritative: ``ctx.repo`` stays the honest-None placeholder so
    ``post._resolve_repo`` falls back to ``gh repo view`` and canonicalizes exactly as
    before this epic."""
    monkeypatch.setattr(diff, "_git_toplevel", lambda wd: "/repo/root")
    monkeypatch.setattr(
        diff.gh,
        "pr_view",
        lambda *a, **k: (
            '{"number": 5, "isDraft": false, "mergeStateStatus": "CLEAN", '
            '"headRefName": "feat", "headRefOid": "headsha", '
            '"baseRefName": "main", "baseRefOid": "basesha"}'
        ),
    )
    monkeypatch.setattr(diff, "_sha_present", lambda wd, sha: True)

    def fake_git(workdir, args, *, check=True):
        class R:
            returncode = 0
            stdout = "mergebasesha\n" if args[:1] == ["merge-base"] else "the diff\n"

        return R()

    monkeypatch.setattr(diff, "_git", fake_git)

    # If resolve_pr fell back to the local origin (an alias, here), it would surface
    # a truthy slug and downstream would skip `gh repo view`. Guard against that by
    # making any accidental local-origin resolution loud.
    monkeypatch.setattr(
        diff.gh,
        "current_repo",
        lambda **k: "alias-owner/alias-repo",
        raising=False,
    )

    ctx = diff.resolve_pr(5, workdir="/repo/root")  # --repo OMITTED
    # The locally-derived origin is NOT authoritative — repo stays None so the
    # downstream `gh repo view` (307) fallback still runs.
    assert ctx.repo is None

    # Downstream POST path: `gh repo view` canonicalizes the alias origin to the
    # repo's CURRENT slug, and the review posts there — never the alias.
    monkeypatch.setattr(
        post.gh, "current_repo", lambda **k: "canonical-owner/canonical-repo"
    )
    assert post._resolve_repo(ctx) == "canonical-owner/canonical-repo"


def test_resolve_pr_no_common_ancestor_fails_loud(monkeypatch):
    """When the authoritative base and head share no merge base, resolve_pr fails
    loud rather than degrading to a base-tip diff."""
    monkeypatch.setattr(diff, "_git_toplevel", lambda wd: "/repo/root")
    monkeypatch.setattr(
        diff.gh,
        "pr_view",
        lambda *a, **k: (
            '{"number": 5, "isDraft": false, "mergeStateStatus": "CLEAN", "headRefName": "feat", "headRefOid": "headsha", '
            '"baseRefName": "main", "baseRefOid": "basesha"}'
        ),
    )
    monkeypatch.setattr(diff, "_sha_present", lambda wd, sha: True)

    diff_attempted = False

    def fake_git(workdir, args, *, check=True):
        nonlocal diff_attempted
        if args[:1] == ["diff"]:
            diff_attempted = True

        class R:
            # merge-base finds no common ancestor (rc=1, empty stdout).
            returncode = 1 if args[:1] == ["merge-base"] else 0
            stdout = ""

        return R()

    monkeypatch.setattr(diff, "_git", fake_git)

    with pytest.raises(diff.ReviewError, match="no common ancestor"):
        diff.resolve_pr(5, workdir="/repo/root")
    assert diff_attempted is False


def test_resolve_pr_missing_base_oid_fails_loud(monkeypatch):
    """A `gh pr view` with no baseRefOid fails loud — the resolver never guesses
    a base, so the review can't run against a wrong one."""
    monkeypatch.setattr(diff, "_git_toplevel", lambda wd: "/repo/root")
    monkeypatch.setattr(
        diff.gh,
        "pr_view",
        lambda *a, **k: (
            '{"number": 5, "isDraft": false, "mergeStateStatus": "CLEAN", "headRefName": "feat", '
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
            '{"number": 5, "isDraft": false, "mergeStateStatus": "CLEAN", "headRefName": "feat", "headRefOid": "headsha", '
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


def test_review_view_repo_is_slug_when_known():
    """A view built with an explicit slug reports it — the resolved-PR source of
    truth downstream posters/producers post to."""
    ctx = diff.review_view(
        number=5,
        repo="owner/repo",
        head_sha="h",
        base_ref="main",
        base_sha="b",
        diff="",
        is_draft=False,
    )
    assert ctx.repo == "owner/repo"


def test_review_view_repo_is_none_for_handbuilt_context():
    """A hand-built view WITHOUT a slug reports `repo is None` — NOT the
    `local/local` placeholder slug — so downstream `_resolve_repo` /
    `_resolve_org_repo` honestly fall back to `gh repo view` instead of silently
    posting/provisioning against a placeholder (ADR-0024 falsey-repo contract)."""
    ctx = diff.review_view(
        number=5,
        repo=None,
        head_sha="h",
        base_ref="main",
        base_sha="b",
        diff="",
        is_draft=False,
    )
    assert ctx.repo is None
