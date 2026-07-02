"""Tests for `shipit.review.diff` — PR resolution + workdir normalization.

The full `resolve_pr` rides the git adapter + `gh`; here we cover the workdir
normalization seam (`_git_toplevel`) and that `resolve_pr` anchors the agent's
cwd to the repo root even when invoked from a nested subdir, with the
`shipit.git` adapter and gh boundary stubbed (PROC02-WS03: the review-diff
path builds no git argv of its own — it patches the adapter's typed reads).
"""

from __future__ import annotations

import json

import pytest

from shipit.identity import Sha, repo_from_slug
from shipit.review import diff, post

#: The full 40-hex PR head every stubbed `gh pr view` payload carries (COR02).
HEAD = "cafe" * 10
#: The full 40-hex PR base (baseRefOid) the stubbed payloads carry — minted
#: into a `Sha` at the resolve_pr boundary (PROC03), so the stub must be a
#: valid full sha.
BASE = "beef" * 10
#: The merge base the stubbed `git.merge_base` answers — the adapter returns a
#: typed `Sha` (PROC03), so the fakes model that contract.
MERGE_BASE = Sha("ba5e" * 10)


def _present_recording(seen: list):
    """A `git.commit_present` fake that records each cwd and answers True."""

    def fake(sha, *, cwd):
        seen.append(cwd)
        return True

    return fake


def _merge_base_recording(seen: list):
    """A `git.merge_base` fake that records each cwd and answers a fixed `Sha`
    — the adapter's typed return (PROC03)."""

    def fake(a, b, *, cwd):
        seen.append(cwd)
        return MERGE_BASE

    return fake


def test_git_toplevel_returns_repo_root(monkeypatch):
    # Routes through the single `git.repo_root(cwd=...)` boundary (ADR-0024), passing
    # the workdir through as `cwd` and returning its resolved toplevel.
    calls: list[str] = []

    def fake_repo_root(*, cwd):
        calls.append(cwd)
        return "/repo/root"

    monkeypatch.setattr(diff.git, "repo_root", fake_repo_root)
    assert diff._git_toplevel("/repo/root/src/deep") == "/repo/root"
    assert calls == ["/repo/root/src/deep"]


def test_git_toplevel_none_outside_checkout(monkeypatch):
    # The boundary returns None outside a checkout; `_git_toplevel` passes it through.
    monkeypatch.setattr(diff.git, "repo_root", lambda *, cwd: None)
    assert diff._git_toplevel("/tmp/not-a-repo") is None


def test_resolve_pr_normalizes_workdir_to_toplevel(monkeypatch):
    """`resolve_pr` invoked from a nested subdir resolves the diff (and the
    agent's cwd) against the repo ROOT, not the subdir."""
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
    # The ReviewView base is the authoritative base sha (baseRefOid), not a local
    # `origin/<base>` ref — minted into a typed `Sha` at the boundary (PROC03).
    assert ctx.base_sha == Sha(BASE)
    assert isinstance(ctx.base_sha, Sha)
    # The diff endpoint is the MERGE BASE of the authoritative base + head (the PR
    # branch point) — GitHub's three-dot diff — computed explicitly, not the raw
    # base tip. The endpoints flow through as typed `Sha`s (PROC03) — no raw
    # string crosses the review-diff path.
    assert seen_diff_specs == [(MERGE_BASE, Sha(HEAD)), (MERGE_BASE, Sha(HEAD))]
    assert all(isinstance(end, Sha) for spec in seen_diff_specs for end in spec)
    # Every git invocation ran against the toplevel, not the nested subdir.
    assert set(seen) == {"/repo/root"}


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

    # If resolve_pr fell back to the local origin (an alias, here), it would surface
    # a truthy slug and downstream would skip `gh repo view`. Guard against that by
    # making any accidental local-origin resolution loud.
    monkeypatch.setattr(
        diff.gh,
        "current_repo",
        lambda **k: repo_from_slug("alias-owner/alias-repo"),
        raising=False,
    )

    ctx = diff.resolve_pr(5, workdir="/repo/root")  # --repo OMITTED
    # The locally-derived origin is NOT authoritative — repo stays None so the
    # downstream `gh repo view` (307) fallback still runs.
    assert ctx.repo is None

    # Downstream POST path: `gh repo view` canonicalizes the alias origin to the
    # repo's CURRENT slug, and the review posts there — never the alias.
    monkeypatch.setattr(
        post.gh,
        "current_repo",
        lambda **k: repo_from_slug("canonical-owner/canonical-repo"),
    )
    assert post._resolve_repo(ctx) == "canonical-owner/canonical-repo"


def test_resolve_pr_no_common_ancestor_fails_loud(monkeypatch):
    """When the authoritative base and head share no merge base, resolve_pr fails
    loud rather than degrading to a base-tip diff."""
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
    # merge-base finds no common ancestor.
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
    """A `gh pr view` with no baseRefOid fails loud — the resolver never guesses
    a base, so the review can't run against a wrong one."""
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
    """A `baseRefOid` that does not validate as a full sha fails loud at the
    minting boundary (PROC03) — the review never carries a bogus base identity."""
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
    """When the base sha (baseRefOid) can't be made present — a stale/missing
    `origin/<base>` and an unfetchable sha — resolve_pr fails loud instead of
    silently degrading to a local ref or the base tip (no wrong-base diff)."""
    monkeypatch.setattr(diff, "_git_toplevel", lambda wd: "/repo/root")
    monkeypatch.setattr(
        diff.gh,
        "pr_view",
        lambda *a, **k: json.loads(
            f'{{"number": 5, "isDraft": false, "mergeStateStatus": "CLEAN", "headRefName": "feat", "headRefOid": "{HEAD}", '
            f'"baseRefName": "main", "baseRefOid": "{BASE}"}}'
        ),
    )
    # The head is present; the base sha never becomes present (every fetch is a
    # no-op — the classic stale/missing `origin/main`). The adapter probe takes
    # the typed identity (PROC03), so the fake compares Sha-to-Sha.
    monkeypatch.setattr(
        diff.git, "commit_present", lambda sha, *, cwd: sha == Sha(HEAD)
    )
    # `fetch_ref` is the deliberately-str refspec seam: record what crosses it
    # and pin that the caller stringified the typed sha there.
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
    # It failed BEFORE computing any diff — never produced a wrong-base diff.
    assert diff_attempted is False
    # The bare-sha fetch attempts crossed the refspec seam as plain strings —
    # the ONE place the typed shas stringify (base branch first, then the sha).
    assert fetched == ["main", BASE]
    assert all(isinstance(r, str) for r in fetched)


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
        head_sha="ab" * 20,  # a full 40-hex sha (COR02)
        base_ref="main",
        base_sha="ba" * 20,  # a full 40-hex sha — minted into Sha (PROC03)
        diff="",
        is_draft=False,
    )
    assert ctx.repo == "owner/repo"
    # The builder mints the raw base string into the typed identity, mirroring
    # head_sha — the composed view is fully typed however it was built.
    assert ctx.base_sha == Sha("ba" * 20)
    assert isinstance(ctx.base_sha, Sha)


def test_review_view_repo_is_none_for_handbuilt_context():
    """A hand-built view WITHOUT a slug reports `repo is None` — NOT the
    `local/local` placeholder slug — so downstream `_resolve_repo` /
    `_resolve_org_repo` honestly fall back to `gh repo view` instead of silently
    posting/provisioning against a placeholder (ADR-0024 falsey-repo contract)."""
    ctx = diff.review_view(
        number=5,
        repo=None,
        head_sha="ab" * 20,  # a full 40-hex sha (COR02)
        base_ref="main",
        base_sha="ba" * 20,  # a full 40-hex sha — minted into Sha (PROC03)
        diff="",
        is_draft=False,
    )
    assert ctx.repo is None
