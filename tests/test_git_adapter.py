"""Unit tests for the git Tool adapter's parsed reads (PROC02-WS03).

These pin the parsing/mapping the registry, hooks, and Tree planner rely on —
the ahead/behind left-right order, upstream-absent → ``None`` / ``(0, 0)``, the
exact-ref ``ls-remote`` equality, and the porcelain line parse — by patching
only the Exec seam (``_git`` / ``_probe``), never a real subprocess. The one
deliberate exception is
``test_hooks_dir_resolves_the_shared_common_dir_in_a_real_worktree``, which
shells out to real ``git`` end-to-end: a linked worktree's ``.git``-file →
shared-common-dir resolution (#914) is exactly the behaviour a fake seam can't
prove, so it drives an actual ``git worktree`` checkout. Most
registry reads are PROBES (``check=False`` through the Exec runner, ADR-0028):
a nonzero exit is a normal answer for a scan over the fleet, so the fakes
return an :class:`ExecResult` with the rc under test rather than raising.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from shipit import git
from shipit.execrun import (
    CAUSE_EXIT,
    CAUSE_MISSING_BINARY,
    CAUSE_TIMEOUT,
    ExecError,
    ExecResult,
)
from shipit.identity import Sha


def _ok(stdout: str = "") -> ExecResult:
    return ExecResult(argv=("git",), rc=0, stdout=stdout, stderr="", duration_ms=1)


def _fail(stderr: str = "", rc: int = 1) -> ExecResult:
    return ExecResult(argv=("git",), rc=rc, stdout="", stderr=stderr, duration_ms=1)


def test_ahead_behind_maps_left_right_to_behind_ahead(monkeypatch):
    # `rev-list --left-right --count @{upstream}...HEAD` prints "<behind> <ahead>".
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _ok("3\t5\n"))
    assert git.ahead_behind(cwd="/x") == (5, 3)


def test_ahead_behind_no_upstream_is_level(monkeypatch):
    monkeypatch.setattr(
        git, "_probe", lambda args, *, cwd: _fail("no upstream configured")
    )
    assert git.ahead_behind(cwd="/x") == (0, 0)


def test_unpushed_shas_lists_the_local_only_commits(monkeypatch):
    # `rev-list HEAD --not --remotes`: commits on NO remote at all — the
    # upstream-independent "unpushed" the ephemeral gc ladder is defined over. The
    # SHAs (not a bare count) are what lets the ladder exclude exactly the recorded
    # provisioning commit (#232) — and they come back as Sha VALUE OBJECTS
    # (PROC03), so the exclusion compares identities through the type.
    seen = {}

    def fake(args, *, cwd):
        seen["args"] = args
        return _ok(f"{'a' * 40}\n{'b' * 40}\n")

    monkeypatch.setattr(git, "_probe", fake)
    assert git.unpushed_shas(cwd="/x") == (Sha("a" * 40), Sha("b" * 40))
    assert seen["args"] == ["rev-list", "HEAD", "--not", "--remotes"]


def test_unpushed_shas_empty_when_everything_is_on_a_remote(monkeypatch):
    # Empty output = every commit reachable from HEAD is on some remote: the
    # provably-safe reading, distinct from None (unreadable).
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _ok(""))
    assert git.unpushed_shas(cwd="/x") == ()


def test_unpushed_shas_unreadable_is_none_not_empty(monkeypatch):
    # None (unknown) — NEVER () (provably pushed): the caller keeps on unknown, so
    # a git failure must not read as "nothing to lose". Malformed output (a line
    # that is not a SHA) is the same unreadable case.
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _fail("unborn HEAD"))
    assert git.unpushed_shas(cwd="/x") is None
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _ok("not-a-sha\n"))
    assert git.unpushed_shas(cwd="/x") is None


def test_push_no_verify_bypasses_the_pre_push_hook(monkeypatch):
    # #477: install's own push must carry `--no-verify` — the freshly-armed
    # pre-push hook runs the WHOLE-TREE lint gate, which a virgin consumer's
    # pre-existing debt fails, killing the very push that delivers the env to
    # clear it (the tripwire armed by the run that trips it). The flag sits
    # before the remote/branch operands, where git parses options.
    seen = {}

    def fake(args, *, cwd, timeout=None):
        seen["args"] = args
        return ""

    monkeypatch.setattr(git, "_git", fake)
    git.push("shipit/install", cwd="/x", force=True, no_verify=True)
    assert seen["args"] == [
        "push",
        "--force",
        "--no-verify",
        "origin",
        "shipit/install",
    ]


def test_clean_non_committed_removes_untracked_and_ignored_forcing_nested(monkeypatch):
    # #942: repo creation strips every non-committed artifact before the atomic
    # publish so the tree is relocatable. `-x` includes ignored paths (target/,
    # .pixi/), `-d` recurses untracked dirs, and `-ff` forces through nested
    # working trees so nothing regenerable survives the rename.
    seen = {}

    def fake(args, *, cwd, timeout=None):
        seen["args"] = args
        seen["timeout"] = timeout
        return ""

    monkeypatch.setattr(git, "_git", fake)
    git.clean_non_committed(cwd="/x")
    assert seen["args"] == ["clean", "-ffdx"]
    # Unlinking a materialized .pixi env + build cache is bulk-filesystem work,
    # not near-instant plumbing, so it carries the generous strip bound — the
    # tight local-plumbing timeout would spuriously fail a still-progressing
    # strip on slower disks.
    assert seen["timeout"] == git._STRIP_TIMEOUT


def test_push_default_does_not_bypass_hooks(monkeypatch):
    # The bypass is install's deliberate opt-in, never the adapter's default.
    seen = {}

    def fake(args, *, cwd, timeout=None):
        seen["args"] = args
        return ""

    monkeypatch.setattr(git, "_git", fake)
    git.push("main", cwd="/x")
    assert seen["args"] == ["push", "origin", "main"]


def test_switch_moves_to_an_existing_branch_without_force(monkeypatch):
    # #777 mode 1: the MODE_PR caller-branch restore switches to a branch that
    # already exists (the caller's own) — a plain `git switch`, never `-C`, so it
    # only ever moves HEAD and never creates a ref.
    seen = {}

    def fake(args, *, cwd, timeout=None):
        seen["args"] = args
        return ""

    monkeypatch.setattr(git, "_git", fake)
    git.switch("main", cwd="/x")
    assert seen["args"] == ["switch", "main"]


def test_default_branch_strips_the_remote_prefix_from_the_symref(monkeypatch):
    # #852: the MODE_PR staging-branch base is read from `<remote>/HEAD` and the
    # `<remote>/` prefix is stripped, so `origin/HEAD -> origin/main` resolves to
    # the bare branch name `main`.
    seen = {}

    def fake(args, *, cwd, timeout=None):
        seen["args"] = args
        return _ok("origin/main\n")

    monkeypatch.setattr(git, "_probe", fake)
    assert git.default_branch(cwd="/x") == "main"
    assert seen["args"] == ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"]


def test_default_branch_honors_a_non_main_default(monkeypatch):
    # A consumer whose default branch is `trunk` resolves to `trunk`, never a
    # hardcoded `main` — the reset base and the PR base both follow the symref.
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _ok("origin/trunk\n"))
    assert git.default_branch(cwd="/x") == "trunk"


def test_default_branch_falls_back_to_main_when_the_symref_is_absent(monkeypatch):
    # Some reference-borrow clones never set `origin/HEAD`: an absent symref is a
    # NORMAL probe answer (nonzero rc). With no `main`/`master`/`trunk`
    # remote-tracking ref to confirm either, the resolver falls back to the
    # portfolio default `main` rather than raising.
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _fail("not a symref"))
    assert git.default_branch(cwd="/x") == "main"


def test_default_branch_probes_common_names_when_the_symref_is_absent(monkeypatch):
    # #852 review: an absent `<remote>/HEAD` symref must NOT blindly resolve to
    # `main` — that would crash the MODE_PR reset onto a non-existent
    # `origin/main` on a `master`/`trunk` remote. The fallback probes the common
    # default-branch names against the remote-tracking refs a fetch populated and
    # returns the one that exists.
    def fake(args, *, cwd):
        if args[0] == "symbolic-ref":
            return _fail("no symref")
        # rev-parse --verify --quiet refs/remotes/origin/<candidate>
        return _ok("deadbeef\n") if args[-1].endswith("/master") else _fail()

    monkeypatch.setattr(git, "_probe", fake)
    assert git.default_branch(cwd="/x") == "master"


def test_staged_paths_scopes_the_cached_diff_and_parses_the_names(monkeypatch):
    # #984 review: the MODE_PR commit pathspec reads a scoped
    # `git diff --cached --name-only`, parsed to the staged names. A path the
    # diff omits (matched nothing, or matches HEAD) simply never appears.
    seen = {}

    def fake(args, *, cwd):
        seen["args"] = args
        return "a\n\nb/c\n"

    monkeypatch.setattr(git, "_git", fake)
    assert git.staged_paths(["a", "b/c", "gone"], cwd="/x") == ["a", "b/c"]
    assert seen["args"] == ["diff", "--cached", "--name-only", "--", "a", "b/c", "gone"]


def test_staged_paths_is_empty_on_a_clean_index(monkeypatch):
    # No staged diff for the named pathspecs — the MODE_PR "nothing to publish"
    # case, which skips the commit rather than crashing an empty pathspec commit.
    monkeypatch.setattr(git, "_git", lambda args, *, cwd: "")
    assert git.staged_paths(["a"], cwd="/x") == []


def test_staged_paths_surfaces_a_git_failure_rather_than_masking_it(monkeypatch):
    # #984 review: unlike `--quiet` (rc 1 = diff present, rc >1 = failure),
    # `--name-only` exits 0 on any successful diff and nonzero ONLY on a genuine
    # failure (bad pathspec magic, unreadable index) — which `_git` raises. A
    # real git failure can never be masked as a staged path.
    def boom(args, *, cwd):
        raise ExecError(args, rc=128, stdout="", stderr="fatal", duration_ms=1)

    monkeypatch.setattr(git, "_git", boom)
    with pytest.raises(ExecError):
        git.staged_paths(["a"], cwd="/x")


def test_staged_paths_on_empty_paths_never_probes(monkeypatch):
    # An empty pathspec is a vacuous "nothing staged" — never a bare unscoped
    # `git diff --cached --name-only` answering for the whole index.
    def boom(*a, **k):
        raise AssertionError("must not probe on an empty pathspec")

    monkeypatch.setattr(git, "_git", boom)
    assert git.staged_paths([], cwd="/x") == []


def test_reset_index_unstages_everything_to_head(monkeypatch):
    # #852 review: the MODE_PR caller-restore unstages the soft-reset index when
    # the operator started on the scratch branch — a bare `git reset` (mixed to
    # HEAD), leaving HEAD and the working tree untouched.
    seen = {}

    def fake(args, *, cwd, timeout=None):
        seen["args"] = args
        return ""

    monkeypatch.setattr(git, "_git", fake)
    git.reset_index(cwd="/x")
    assert seen["args"] == ["reset"]


def test_reset_soft_moves_only_the_branch_pointer(monkeypatch):
    # #852: the staging-branch rebase resets shipit/install onto origin/<default>
    # with `--soft` so the rendered managed files stay in the working tree for
    # the following pathspec commit.
    seen = {}

    def fake(args, *, cwd, timeout=None):
        seen["args"] = args
        return ""

    monkeypatch.setattr(git, "_git", fake)
    git.reset_soft("origin/main", cwd="/x")
    assert seen["args"] == ["reset", "--soft", "origin/main"]


def test_submodule_update_init_syncs_then_recursively_inits_on_the_network_bound(
    monkeypatch,
):
    # #485/#486: the Tree-provisioning seam that populates a dissociated clone's empty
    # submodule gitlinks. It `sync --recursive` FIRST (so a reuse-refresh onto an
    # advanced head picks up a moved submodule URL from .gitmodules — #486), THEN the
    # recursive init CI does (`submodules: recursive`). Both carry the remote-facing
    # timeout (submodule work hits the network), not the local-plumbing bound.
    calls = []

    def fake(args, *, cwd, timeout=None):
        calls.append({"args": args, "cwd": cwd, "timeout": timeout})
        return ""

    monkeypatch.setattr(git, "_git", fake)
    git.submodule_update_init(cwd="/tree")
    assert [c["args"] for c in calls] == [
        ["submodule", "sync", "--recursive"],
        ["submodule", "update", "--init", "--recursive"],
    ]
    assert all(c["cwd"] == "/tree" for c in calls)
    assert all(c["timeout"] == git._NETWORK_TIMEOUT for c in calls)


def test_commits_between_lists_the_range(monkeypatch):
    # `rev-list <base>..<head>`: exactly what provisioning committed (#232) — the
    # SHAs recorded into .git/shipit-provision.json at Tree birth. Typed at both
    # ends (PROC03): Sha endpoints in, Sha values out — the argv still carries
    # the plain string form.
    seen = {}

    def fake(args, *, cwd):
        seen["args"] = args
        return _ok(f"{'c' * 40}\n")

    monkeypatch.setattr(git, "_probe", fake)
    assert git.commits_between(Sha("a" * 40), Sha("c" * 40), cwd="/x") == [
        Sha("c" * 40)
    ]
    assert seen["args"] == ["rev-list", f"{'a' * 40}..{'c' * 40}"]


def test_commits_between_unreadable_is_none(monkeypatch):
    # A failed or malformed rev-list -> None, so the caller records NOTHING rather
    # than something wrong (an unrecorded provisioning commit only KEEPS the Tree).
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _fail("bad ref"))
    assert git.commits_between(Sha("a" * 40), Sha("b" * 40), cwd="/x") is None
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _ok("garbage\n"))
    assert git.commits_between(Sha("a" * 40), Sha("b" * 40), cwd="/x") is None


def test_head_commit_returns_a_sha_value_object(monkeypatch):
    # `rev-parse HEAD` is a commit-IDENTITY read (PROC03): the adapter returns
    # the validated Sha value object — lowercase-normalized by the type — never
    # a raw string.
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _ok(f"{'AB' * 20}\n"))
    head = git.head_commit(cwd="/x")
    assert head == Sha("ab" * 20)
    assert isinstance(head, Sha)


def test_head_commit_unresolvable_or_malformed_is_none(monkeypatch):
    # Best-effort contract: a failed rev-parse (detached/unborn HEAD) AND output
    # that does not validate as a full sha both degrade to None, never raise.
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _fail("unborn HEAD"))
    assert git.head_commit(cwd="/x") is None
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _ok("not-a-sha\n"))
    assert git.head_commit(cwd="/x") is None


def test_hooks_dir_resolves_a_relative_answer_against_cwd(monkeypatch):
    # A normal checkout: `rev-parse --git-path hooks` prints the path RELATIVE to
    # the queried checkout, so the adapter joins it onto cwd to an absolute path.
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _ok(".git/hooks\n"))
    assert git.hooks_dir(cwd="/repo") == Path("/repo/.git/hooks")


def test_hooks_dir_keeps_an_absolute_worktree_answer_verbatim(monkeypatch):
    # A linked worktree: git prints the ABSOLUTE shared common-dir hooks path, so
    # os.path.join keeps it as-is rather than prefixing cwd (#914 — the whole
    # point: a worktree's hooks live outside its own `.git` file).
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _ok("/main/.git/hooks\n"))
    assert git.hooks_dir(cwd="/main/wt") == Path("/main/.git/hooks")


def test_hooks_dir_none_when_not_a_repo(monkeypatch):
    # A probe: a not-a-repo nonzero answer AND a launch-level ExecError both
    # degrade to None so a best-effort caller no-ops rather than crashing.
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _fail("not a git repo"))
    assert git.hooks_dir(cwd="/x") is None

    def _raise(args, *, cwd):
        raise ExecError(["git"], rc=None, cause=CAUSE_MISSING_BINARY)

    monkeypatch.setattr(git, "_probe", _raise)
    assert git.hooks_dir(cwd="/x") is None


def test_hooks_dir_resolves_the_shared_common_dir_in_a_real_worktree(tmp_path):
    # End-to-end over REAL git (no seam patch): a linked worktree's `.git` is a
    # FILE and its hooks live in the main checkout's shared common dir, so the
    # adapter must resolve to `<main>/.git/hooks` — exactly what the hardcoded
    # `<worktree>/.git/hooks` (a nonexistent dir) missed before #914.
    main = tmp_path / "main"
    main.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=main, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.co"], cwd=main, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=main, check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-qm", "init"], cwd=main, check=True
    )
    wt = tmp_path / "wt"
    subprocess.run(["git", "worktree", "add", "-q", str(wt)], cwd=main, check=True)
    assert (wt / ".git").is_file()  # a linked worktree points at the common dir

    shared_hooks = (main / ".git" / "hooks").resolve()
    assert git.hooks_dir(cwd=str(main)).resolve() == shared_hooks
    assert git.hooks_dir(cwd=str(wt)).resolve() == shared_hooks


def test_upstream_ref_returns_tracking_ref(monkeypatch):
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _ok("origin/main\n"))
    assert git.upstream_ref(cwd="/x") == "origin/main"


def test_upstream_ref_none_when_absent(monkeypatch):
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _fail("no upstream"))
    assert git.upstream_ref(cwd="/x") is None


def test_status_porcelain_parses_to_nonempty_lines(monkeypatch):
    # The centralized porcelain read: the adapter returns the PARSED lines (one
    # per dirty entry, blanks dropped), so callers ask truthiness/len of it
    # instead of re-splitting raw text at each site.
    monkeypatch.setattr(
        git, "_git", lambda args, *, cwd: " M src/a.py\n?? notes.txt\n\n"
    )
    assert git.status_porcelain(cwd="/x") == [" M src/a.py", "?? notes.txt"]
    monkeypatch.setattr(git, "_git", lambda args, *, cwd: "")
    assert git.status_porcelain(cwd="/x") == []


def test_epic_umbrella_exists_checks_remote_tracking_ref_first(monkeypatch):
    # The semantic epic test: `<epic>/umbrella` present as the remote-tracking ref
    # (the usual shape in a clone) -> True, via an EXACT `show-ref --verify` (never a
    # pattern), and the remote ref is tried before any local head.
    seen: list = []

    def fake_git(args, *, cwd):
        seen.append(args)
        return _ok()  # `show-ref --verify --quiet` exits 0 when the ref resolves

    monkeypatch.setattr(git, "_probe", fake_git)
    assert git.epic_umbrella_exists("TRE04", cwd="/x") is True
    assert seen[0] == [
        "show-ref",
        "--verify",
        "--quiet",
        "refs/remotes/origin/TRE04/umbrella",
    ]


def test_epic_umbrella_exists_falls_back_to_local_head(monkeypatch):
    # No remote-tracking ref but a local `refs/heads/<epic>/umbrella` -> still True.
    def fake_git(args, *, cwd):
        if args[-1] == "refs/heads/TRE04/umbrella":
            return _ok()
        return _fail()

    monkeypatch.setattr(git, "_probe", fake_git)
    assert git.epic_umbrella_exists("TRE04", cwd="/x") is True


def test_epic_umbrella_exists_false_when_no_umbrella(monkeypatch):
    # Neither ref resolves (an ordinary `feature/foo` -> no `feature/umbrella`): the
    # probe reads the nonzero exit as "not an epic" rather than raising.
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _fail())
    assert git.epic_umbrella_exists("feature", cwd="/x") is False


def test_epic_umbrella_exists_launch_failure_raises_not_false(monkeypatch):
    # WS03-FX01 alignment (#297): an absent ref is a normal answer (ok=False ->
    # False, above), but a launch-level failure (missing git, timeout) must NOT
    # read as "not an epic" — it propagates, and the fail-CLOSED WorktreeCreate
    # hook aborts the spawn loudly instead of silently minting a mis-based
    # epic-less holding branch.
    def boom(args, *, cwd):
        raise ExecError(["git", "show-ref"], rc=None, cause=CAUSE_MISSING_BINARY)

    monkeypatch.setattr(git, "_probe", boom)
    with pytest.raises(ExecError):
        git.epic_umbrella_exists("TRE04", cwd="/x")


# --- review-diff reads: typed commit-identity plumbing (PROC03-WS03) --------
#
# The review-diff endpoints are commit identities, so the adapter takes/returns
# `Sha` value objects (`commit_present` / `merge_base` / `diff_range` /
# `diff_name_only`) — the argv still carries the plain string form, and
# `fetch_ref` stays the one deliberately-str refspec seam. Pinned through the
# injected runner seam (`_git` / `_probe`), never a real subprocess.


def test_commit_present_takes_sha_and_probes_cat_file(monkeypatch):
    seen = {}

    def fake(args, *, cwd):
        seen["args"] = args
        return _ok()

    monkeypatch.setattr(git, "_probe", fake)
    assert git.commit_present(Sha("a" * 40), cwd="/x") is True
    # The typed identity stringifies only INTO the argv.
    assert seen["args"] == ["cat-file", "-e", f"{'a' * 40}^{{commit}}"]
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _fail())
    assert git.commit_present(Sha("a" * 40), cwd="/x") is False


def test_merge_base_returns_a_sha_value_object(monkeypatch):
    # The merge base IS a commit identity, so it leaves the adapter typed
    # (PROC03) — lowercase-normalized by the Sha constructor.
    seen = {}

    def fake(args, *, cwd):
        seen["args"] = args
        return _ok(f"{'AB' * 20}\n")

    monkeypatch.setattr(git, "_probe", fake)
    base = git.merge_base(Sha("a" * 40), Sha("b" * 40), cwd="/x")
    assert base == Sha("ab" * 20)
    assert isinstance(base, Sha)
    assert seen["args"] == ["merge-base", "a" * 40, "b" * 40]


def test_merge_base_none_on_no_ancestor_or_malformed_output(monkeypatch):
    # Nonzero exit = no common ancestor (the caller fails loud); malformed
    # output degrades to the same None — nothing rather than something wrong.
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _fail("no ancestor"))
    assert git.merge_base(Sha("a" * 40), Sha("b" * 40), cwd="/x") is None
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _ok("not-a-sha\n"))
    assert git.merge_base(Sha("a" * 40), Sha("b" * 40), cwd="/x") is None


def test_diff_range_takes_sha_endpoints(monkeypatch):
    seen = {}

    def fake(args, *, cwd):
        seen["args"] = args
        return "the diff\n"

    monkeypatch.setattr(git, "_git", fake)
    out = git.diff_range(Sha("a" * 40), Sha("b" * 40), cwd="/x")
    assert out == "the diff\n"
    assert seen["args"] == ["diff", f"{'a' * 40}..{'b' * 40}"]


def test_is_ancestor_true_on_exit_zero(monkeypatch):
    # `git merge-base --is-ancestor A B` exits 0 when A is an ancestor of B — the
    # incremental-round convergence gate (RVW02-WS06).
    seen = {}

    def fake(args, *, cwd):
        seen["args"] = args
        return _ok()

    monkeypatch.setattr(git, "_probe", fake)
    assert git.is_ancestor(Sha("a" * 40), Sha("b" * 40), cwd="/x") is True
    assert seen["args"] == ["merge-base", "--is-ancestor", "a" * 40, "b" * 40]


def test_is_ancestor_false_on_nonancestor_and_on_error(monkeypatch):
    # Exit 1 = a genuine non-ancestor (rebase/force-push); any other nonzero =
    # error (a commit not present). BOTH return False so the caller falls back to
    # a full round — fail toward over-reviewing, never a wrongly-narrowed one.
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _fail(rc=1))
    assert git.is_ancestor(Sha("a" * 40), Sha("b" * 40), cwd="/x") is False
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _fail("bad object", rc=128))
    assert git.is_ancestor(Sha("a" * 40), Sha("b" * 40), cwd="/x") is False


def test_diff_name_only_takes_sha_endpoints_and_parses_lines(monkeypatch):
    seen = {}

    def fake(args, *, cwd):
        seen["args"] = args
        return "a.py\n\nb/c.py\n"

    monkeypatch.setattr(git, "_git", fake)
    assert git.diff_name_only(Sha("a" * 40), Sha("b" * 40), cwd="/x") == [
        "a.py",
        "b/c.py",
    ]
    assert seen["args"] == ["diff", "--name-only", f"{'a' * 40}..{'b' * 40}"]


# --- remote_branch_exists: exact-ref equality (codex finding, gh.py:451) ---
#
# `git ls-remote` treats its final arg as a ref *pattern*, so the old
# `bool(non-empty output)` test could false-positive. These pin the helper to
# exact `refs/heads/<branch>` equality — the fail-closed precondition before
# Tree creation depends on it.


def _ls_remote_line(sha: str, refname: str) -> str:
    return f"{sha}\t{refname}\n"


def test_remote_branch_exists_true_when_exact_ref_present(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(args, *, cwd=None, timeout=None):
        calls.append(args)
        return _ls_remote_line("a" * 40, "refs/heads/TRE04/umbrella")

    monkeypatch.setattr(git, "_git", fake_run)
    assert git.remote_branch_exists("TRE04/umbrella", cwd="/x") is True
    # The query is for the FULLY-QUALIFIED ref, not the bare branch name.
    assert calls[0][-1] == "refs/heads/TRE04/umbrella"


def test_remote_branch_exists_false_when_absent(monkeypatch):
    # Empty ls-remote output (no matching head) -> absent.
    monkeypatch.setattr(git, "_git", lambda args, *, cwd=None, timeout=None: "")
    assert git.remote_branch_exists("TRE04/umbrella", cwd="/x") is False


def test_remote_branch_exists_false_for_glob_metachar_branch(monkeypatch):
    # A glob-ish name can never name a real git ref, so it must short-circuit to
    # False WITHOUT ever being sent to git as a pattern (which could expand to a
    # different head and false-positive).
    def boom(args, *, cwd=None, timeout=None):
        raise AssertionError("glob-ish branch name must not reach git ls-remote")

    monkeypatch.setattr(git, "_git", boom)
    assert git.remote_branch_exists("TRE04/*", cwd="/x") is False
    assert git.remote_branch_exists("feat[01]", cwd="/x") is False
    assert git.remote_branch_exists("feat?", cwd="/x") is False


def test_remote_branch_exists_false_when_only_a_different_ref_matches(monkeypatch):
    # Non-empty output but the refname is a DIFFERENT head than the one queried:
    # exact-equality parsing (not any-output) must reject it.
    def fake_run(args, *, cwd=None, timeout=None):
        return _ls_remote_line("b" * 40, "refs/heads/TRE04/umbrella-extra")

    monkeypatch.setattr(git, "_git", fake_run)
    assert git.remote_branch_exists("TRE04/umbrella", cwd="/x") is False


def test_remote_branch_exists_true_when_exact_ref_among_several(monkeypatch):
    # Several lines back; True iff one refname column equals the queried ref exactly.
    def fake_run(args, *, cwd=None, timeout=None):
        return _ls_remote_line(
            "c" * 40, "refs/heads/TRE04/umbrella-extra"
        ) + _ls_remote_line("d" * 40, "refs/heads/TRE04/umbrella")

    monkeypatch.setattr(git, "_git", fake_run)
    assert git.remote_branch_exists("TRE04/umbrella", cwd="/x") is True


# --------------------------------------------------------------------------
# #353 — clone_dissociated fails open on a poisoned --reference donor.
# The real failure (git 2.54, reference with a split commit-graph chain) is
# pinned at the seam: the first Exec raises the exact ExecError shape the live
# diagnosis captured, and the adapter must retry ONCE without --reference.
# --------------------------------------------------------------------------


def _poisoned_clone_error(argv: list[str]) -> ExecError:
    # The live #353 signature: rc=128, "unable to parse commit" + git's
    # "Clone succeeded, but checkout failed." epilogue.
    return ExecError(
        argv,
        rc=128,
        stderr=(
            "fatal: unable to parse commit " + "a" * 40 + "\n"
            "warning: Clone succeeded, but checkout failed.\n"
            "You can inspect what was checked out with 'git status'\n"
        ),
        cause=CAUSE_EXIT,
    )


def test_clone_dissociated_retries_full_clone_on_poisoned_reference(
    monkeypatch, caplog
):
    calls: list[list[str]] = []

    def fake(args, *, cwd=None, timeout=None):
        calls.append(args)
        if "--reference" in args:
            raise _poisoned_clone_error(["git", *args])
        return ""

    monkeypatch.setattr(git, "_git", fake)
    with caplog.at_level(logging.WARNING, logger="shipit.git"):
        git.clone_dissociated("https://x/r.git", "/trees/leaf", reference="/ref")

    # Exactly two clones: the referenced attempt (with commit-graph READING off
    # for the clone process — the #372 fix, `-c` BEFORE the subcommand so nothing
    # persists in the new repo), then the bare full clone — no --reference (the
    # poison) and no --dissociate (meaningless without it).
    assert calls == [
        [
            "-c",
            "core.commitGraph=false",
            "clone",
            "--reference",
            "/ref",
            "--dissociate",
            "https://x/r.git",
            "/trees/leaf",
        ],
        ["clone", "https://x/r.git", "/trees/leaf"],
    ]
    # The degradation is narrated at WARNING with the poisoned reference path,
    # so the trail shows WHY this Tree birth was slow.
    warning = next(r for r in caplog.records if r.levelno == logging.WARNING)
    assert "/ref" in warning.getMessage()
    assert "#353" in warning.getMessage()


def test_clone_dissociated_removes_leftover_dest_before_retry(monkeypatch, tmp_path):
    # git leaves the cloned-but-not-checked-out dest behind on the #353 failure;
    # the retry must not trip over those leftovers ("destination path already
    # exists and is not an empty directory").
    dest = tmp_path / "leaf"

    def fake(args, *, cwd=None, timeout=None):
        if "--reference" in args:
            (dest / ".git").mkdir(parents=True)
            raise _poisoned_clone_error(["git", *args])
        assert not dest.exists(), "retry must start from a clean dest"
        return ""

    monkeypatch.setattr(git, "_git", fake)
    git.clone_dissociated("https://x/r.git", str(dest), reference="/ref")


def test_clone_dissociated_propagates_any_other_failure_without_retry(monkeypatch):
    # A genuinely failed clone (bad URL, auth, no space) is NOT the poisoned-
    # reference shape: it must propagate untouched, with no second clone attempt.
    calls: list[list[str]] = []

    def fake(args, *, cwd=None, timeout=None):
        calls.append(args)
        raise ExecError(
            ["git", *args],
            rc=128,
            stderr="fatal: repository not found",
            cause=CAUSE_EXIT,
        )

    monkeypatch.setattr(git, "_git", fake)
    with pytest.raises(ExecError):
        git.clone_dissociated("https://x/nope.git", "/trees/leaf", reference="/ref")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "stderr",
    [
        # Checkout killed for a non-#353 reason (disk space, bad filename):
        # git prints the epilogue without the parse error.
        "warning: Clone succeeded, but checkout failed.",
        # Genuine object corruption without the checkout epilogue.
        "fatal: unable to parse commit " + "b" * 40,
    ],
)
def test_clone_dissociated_requires_both_markers_no_single_marker_retry(
    monkeypatch, stderr
):
    # The #353 signature is BOTH fragments together; either alone has innocent
    # causes and must propagate untouched — no dest removal, no full re-clone.
    calls: list[list[str]] = []

    def fake(args, *, cwd=None, timeout=None):
        calls.append(args)
        raise ExecError(["git", *args], rc=128, stderr=stderr, cause=CAUSE_EXIT)

    monkeypatch.setattr(git, "_git", fake)
    with pytest.raises(ExecError):
        git.clone_dissociated("https://x/r.git", "/trees/leaf", reference="/ref")
    assert len(calls) == 1


def test_clone_dissociated_never_retries_a_timeout(monkeypatch):
    # Even marker-looking partial output does not qualify when the child never
    # exited: retrying a full clone after a 10-minute hang would double the hang.
    calls: list[list[str]] = []

    def fake(args, *, cwd=None, timeout=None):
        calls.append(args)
        raise ExecError(
            ["git", *args],
            rc=None,
            stderr="warning: Clone succeeded, but checkout failed.",
            cause=CAUSE_TIMEOUT,
        )

    monkeypatch.setattr(git, "_git", fake)
    with pytest.raises(ExecError):
        git.clone_dissociated("https://x/r.git", "/trees/leaf", reference="/ref")
    assert len(calls) == 1


def test_configure_safe_reference_donor_writes_the_four_writer_knobs(monkeypatch):
    # The suspenders half of #353: BOTH commit-graph write flags AND the auto-
    # gc/auto-maintenance knobs (the live diagnosis proved the write flags alone
    # are not enough — `git gc --auto` regenerated the chain regardless).
    calls: list[tuple[list[str], str | None]] = []

    def fake(args, *, cwd=None, timeout=None):
        calls.append((args, cwd))
        return ""

    monkeypatch.setattr(git, "_git", fake)
    git.configure_safe_reference_donor(cwd="/trees/leaf")

    assert calls == [
        (["config", "--local", "fetch.writeCommitGraph", "false"], "/trees/leaf"),
        (["config", "--local", "gc.writeCommitGraph", "false"], "/trees/leaf"),
        (["config", "--local", "gc.auto", "0"], "/trees/leaf"),
        (["config", "--local", "maintenance.auto", "false"], "/trees/leaf"),
    ]


def test_changed_paths_since_probes_the_three_dot_merge_base_diff(monkeypatch):
    # The lane planner's path-diff (TOL01-WS05): a REF-named three-dot diff —
    # the merge-base file set GitHub's "Files changed" shows for a PR.
    seen = {}

    def fake(args, *, cwd):
        seen["args"], seen["cwd"] = args, cwd
        return _ok("src/a.py\n\ncrates/wasm/src/lib.rs\n")

    monkeypatch.setattr(git, "_probe", fake)
    paths = git.changed_paths_since("origin/main", cwd="/tree")
    assert seen["args"] == ["diff", "--name-only", "origin/main...HEAD"]
    assert seen["cwd"] == "/tree"
    assert paths == ["src/a.py", "crates/wasm/src/lib.rs"]


def test_changed_paths_since_answers_none_when_git_cannot(monkeypatch):
    # A probe: unknown ref / shallow clone / not a checkout is None — the
    # caller's fail-safe (full scope) decision, never an exception.
    monkeypatch.setattr(
        git,
        "_probe",
        lambda args, *, cwd: _fail("fatal: bad revision 'origin/gone...HEAD'", rc=128),
    )
    assert git.changed_paths_since("origin/gone", cwd="/tree") is None
