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
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _ok("3\t5\n"))
    assert git.ahead_behind(cwd="/x") == (5, 3)


def test_ahead_behind_no_upstream_is_level(monkeypatch):
    monkeypatch.setattr(
        git, "_probe", lambda args, *, cwd: _fail("no upstream configured")
    )
    assert git.ahead_behind(cwd="/x") == (0, 0)


def test_unpushed_shas_lists_the_local_only_commits(monkeypatch):
    seen = {}

    def fake(args, *, cwd):
        seen["args"] = args
        return _ok(f"{'a' * 40}\n{'b' * 40}\n")

    monkeypatch.setattr(git, "_probe", fake)
    assert git.unpushed_shas(cwd="/x") == (Sha("a" * 40), Sha("b" * 40))
    assert seen["args"] == ["rev-list", "HEAD", "--not", "--remotes"]


def test_unpushed_shas_empty_when_everything_is_on_a_remote(monkeypatch):
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _ok(""))
    assert git.unpushed_shas(cwd="/x") == ()


def test_unpushed_shas_unreadable_is_none_not_empty(monkeypatch):
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _fail("unborn HEAD"))
    assert git.unpushed_shas(cwd="/x") is None
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _ok("not-a-sha\n"))
    assert git.unpushed_shas(cwd="/x") is None


def test_push_no_verify_bypasses_the_pre_push_hook(monkeypatch):
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
    seen = {}

    def fake(args, *, cwd, timeout=None):
        seen["args"] = args
        seen["timeout"] = timeout
        return ""

    monkeypatch.setattr(git, "_git", fake)
    git.clean_non_committed(cwd="/x")
    assert seen["args"] == ["clean", "-ffdx"]
    assert seen["timeout"] == git._STRIP_TIMEOUT


def test_push_default_does_not_bypass_hooks(monkeypatch):
    seen = {}

    def fake(args, *, cwd, timeout=None):
        seen["args"] = args
        return ""

    monkeypatch.setattr(git, "_git", fake)
    git.push("main", cwd="/x")
    assert seen["args"] == ["push", "origin", "main"]


def test_checkout_create_or_reset_uses_dash_big_b(monkeypatch):
    seen = {}

    def fake(args, *, cwd, timeout=None):
        seen["args"] = args
        seen["cwd"] = cwd
        return ""

    monkeypatch.setattr(git, "_git", fake)
    git.checkout_create_or_reset("main", "origin/main", cwd="/tree")
    assert seen["args"] == ["checkout", "-B", "main", "origin/main"]
    assert seen["cwd"] == "/tree"


def test_switch_moves_to_an_existing_branch_without_force(monkeypatch):
    seen = {}

    def fake(args, *, cwd, timeout=None):
        seen["args"] = args
        return ""

    monkeypatch.setattr(git, "_git", fake)
    git.switch("main", cwd="/x")
    assert seen["args"] == ["switch", "main"]


def test_default_branch_strips_the_remote_prefix_from_the_symref(monkeypatch):
    seen = {}

    def fake(args, *, cwd, timeout=None):
        seen["args"] = args
        return _ok("origin/main\n")

    monkeypatch.setattr(git, "_probe", fake)
    assert git.default_branch(cwd="/x") == "main"
    assert seen["args"] == ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"]


def test_default_branch_honors_a_non_main_default(monkeypatch):
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _ok("origin/trunk\n"))
    assert git.default_branch(cwd="/x") == "trunk"


def test_default_branch_falls_back_to_main_when_the_symref_is_absent(monkeypatch):
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _fail("not a symref"))
    assert git.default_branch(cwd="/x") == "main"


def test_default_branch_probes_common_names_when_the_symref_is_absent(monkeypatch):
    def fake(args, *, cwd):
        if args[0] == "symbolic-ref":
            return _fail("no symref")
        return _ok("deadbeef\n") if args[-1].endswith("/master") else _fail()

    monkeypatch.setattr(git, "_probe", fake)
    assert git.default_branch(cwd="/x") == "master"


def test_default_branch_probes_develop_when_the_symref_is_absent(monkeypatch):
    def fake(args, *, cwd):
        if args[0] == "symbolic-ref":
            return _fail("no symref")
        return _ok("deadbeef\n") if args[-1].endswith("/develop") else _fail()

    monkeypatch.setattr(git, "_probe", fake)
    assert git.default_branch(cwd="/x") == "develop"


def test_staged_paths_scopes_the_cached_diff_and_parses_the_names(monkeypatch):
    seen = {}

    def fake(args, *, cwd, env=None):
        seen["args"] = args
        seen["env"] = env
        return "a\n\nb/c\n"

    monkeypatch.setattr(git, "_git", fake)
    assert git.staged_paths(["a", "b/c", "gone"], cwd="/x") == ["a", "b/c"]
    assert seen["args"] == ["diff", "--cached", "--name-only", "--", "a", "b/c", "gone"]
    assert seen["env"] is None


def test_staged_paths_is_empty_on_a_clean_index(monkeypatch):
    monkeypatch.setattr(git, "_git", lambda args, *, cwd, env=None: "")
    assert git.staged_paths(["a"], cwd="/x") == []


def test_staged_paths_surfaces_a_git_failure_rather_than_masking_it(monkeypatch):
    def boom(args, *, cwd, env=None):
        raise ExecError(args, rc=128, stdout="", stderr="fatal", duration_ms=1)

    monkeypatch.setattr(git, "_git", boom)
    with pytest.raises(ExecError):
        git.staged_paths(["a"], cwd="/x")


def test_staged_paths_on_empty_paths_never_probes(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not probe on an empty pathspec")

    monkeypatch.setattr(git, "_git", boom)
    assert git.staged_paths([], cwd="/x") == []


def test_reset_index_unstages_everything_to_head(monkeypatch):
    seen = {}

    def fake(args, *, cwd, timeout=None):
        seen["args"] = args
        return ""

    monkeypatch.setattr(git, "_git", fake)
    git.reset_index(cwd="/x")
    assert seen["args"] == ["reset"]


def test_reset_soft_moves_only_the_branch_pointer(monkeypatch):
    seen = {}

    def fake(args, *, cwd, timeout=None):
        seen["args"] = args
        return ""

    monkeypatch.setattr(git, "_git", fake)
    git.reset_soft("origin/main", cwd="/x")
    assert seen["args"] == ["reset", "--soft", "origin/main"]


def test_rm_cached_purges_the_index_without_touching_the_working_tree(monkeypatch):
    seen = {}

    def fake(args, *, cwd, timeout=None, env=None):
        seen["args"] = args
        seen["env"] = env
        return ""

    monkeypatch.setattr(git, "_git", fake)
    git.rm_cached(["a.txt", "b/c.txt"], cwd="/x")
    assert seen["args"] == [
        "rm",
        "--cached",
        "--ignore-unmatch",
        "--",
        "a.txt",
        "b/c.txt",
    ]
    assert seen["env"] is None


def test_read_tree_seeds_the_scratch_index_via_git_index_file(monkeypatch):
    seen = {}

    def fake(args, *, cwd, timeout=None, env=None):
        seen["args"] = args
        seen["env"] = env
        return ""

    monkeypatch.setattr(git, "_git", fake)
    git.read_tree("origin/main", cwd="/x", index_file="/tmp/scratch")
    assert seen["args"] == ["read-tree", "origin/main"]
    assert seen["env"] == {"GIT_INDEX_FILE": "/tmp/scratch"}


def test_index_file_binds_git_index_file_across_the_staging_calls(monkeypatch):
    envs = []

    def fake(args, *, cwd, timeout=None, env=None):
        envs.append(env)
        return ""

    monkeypatch.setattr(git, "_git", fake)
    scratch = "/tmp/scratch-index"
    git.add(["a"], cwd="/x", index_file=scratch)
    git.rm_cached(["b"], cwd="/x", index_file=scratch)
    git.staged_paths(["a", "b"], cwd="/x", index_file=scratch)
    git.commit_all("msg", cwd="/x", no_verify=True, index_file=scratch)
    assert envs == [{"GIT_INDEX_FILE": scratch}] * 4


def test_rm_cached_on_empty_paths_never_shells(monkeypatch):
    called = False

    def fake(args, *, cwd, timeout=None):
        nonlocal called
        called = True
        return ""

    monkeypatch.setattr(git, "_git", fake)
    git.rm_cached([], cwd="/x")
    assert called is False


def test_commit_all_publishes_index_deletions_a_pathspec_commit_would_drop(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.co"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "skills").mkdir()
    (repo / "skills" / "foo.md").write_text("old shipit skill\n")
    (repo / "notes.txt").write_text("consumer\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)

    (repo / ".shipit-skills").mkdir()
    (repo / ".shipit-skills" / "foo.md").write_text("new shipit skill\n")
    (repo / "notes.txt").write_text("consumer — locally edited\n")
    git.add([".shipit-skills/foo.md"], cwd=str(repo))
    git.rm_cached(["skills/foo.md"], cwd=str(repo))
    git.commit_all("reconcile", cwd=str(repo), no_verify=True)

    tree = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert "skills/foo.md" not in tree
    assert ".shipit-skills/foo.md" in tree
    assert "notes.txt" in tree
    committed = subprocess.run(
        ["git", "show", "HEAD:notes.txt"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert committed == "consumer\n"


def test_mode_pr_scratch_index_commit_excludes_the_callers_branch_and_staged_state(
    tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.co"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "skills").mkdir()
    (repo / "skills" / "foo.md").write_text("old shipit skill\n")
    (repo / "notes.txt").write_text("consumer\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    base_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    # NOT leak and must SURVIVE the flow).
    (repo / "leak.py").write_text("caller's local branch work\n")
    subprocess.run(["git", "add", "leak.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "caller local work"], cwd=repo, check=True)
    (repo / "staged.txt").write_text("operator staged this\n")
    subprocess.run(["git", "add", "staged.txt"], cwd=repo, check=True)

    git.switch_create("shipit/install", cwd=str(repo))
    git.reset_soft(base_sha, cwd=str(repo))
    (repo / ".shipit-skills").mkdir()
    (repo / ".shipit-skills" / "foo.md").write_text("new shipit skill\n")
    index_file = str(tmp_path / "scratch-index")
    git.read_tree(base_sha, cwd=str(repo), index_file=index_file)
    git.add([".shipit-skills/foo.md"], cwd=str(repo), index_file=index_file)
    git.rm_cached(["skills/foo.md"], cwd=str(repo), index_file=index_file)
    git.commit_all("reconcile", cwd=str(repo), no_verify=True, index_file=index_file)

    tree = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert ".shipit-skills/foo.md" in tree
    assert "skills/foo.md" not in tree
    assert "notes.txt" in tree
    assert "leak.py" not in tree
    assert "staged.txt" not in tree
    parent = subprocess.run(
        ["git", "rev-parse", "HEAD^"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert parent == base_sha

    real_index = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert "staged.txt" in real_index


def test_submodule_update_init_syncs_then_recursively_inits_on_the_network_bound(
    monkeypatch,
):
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
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _fail("bad ref"))
    assert git.commits_between(Sha("a" * 40), Sha("b" * 40), cwd="/x") is None
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _ok("garbage\n"))
    assert git.commits_between(Sha("a" * 40), Sha("b" * 40), cwd="/x") is None


def test_head_commit_returns_a_sha_value_object(monkeypatch):
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _ok(f"{'AB' * 20}\n"))
    head = git.head_commit(cwd="/x")
    assert head == Sha("ab" * 20)
    assert isinstance(head, Sha)


def test_head_commit_unresolvable_or_malformed_is_none(monkeypatch):
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _fail("unborn HEAD"))
    assert git.head_commit(cwd="/x") is None
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _ok("not-a-sha\n"))
    assert git.head_commit(cwd="/x") is None


def test_hooks_dir_resolves_a_relative_answer_against_cwd(monkeypatch):
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _ok(".git/hooks\n"))
    assert git.hooks_dir(cwd="/repo") == Path("/repo/.git/hooks")


def test_hooks_dir_keeps_an_absolute_worktree_answer_verbatim(monkeypatch):
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _ok("/main/.git/hooks\n"))
    assert git.hooks_dir(cwd="/main/wt") == Path("/main/.git/hooks")


def test_hooks_dir_none_when_not_a_repo(monkeypatch):
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _fail("not a git repo"))
    assert git.hooks_dir(cwd="/x") is None

    def _raise(args, *, cwd):
        raise ExecError(["git"], rc=None, cause=CAUSE_MISSING_BINARY)

    monkeypatch.setattr(git, "_probe", _raise)
    assert git.hooks_dir(cwd="/x") is None


def test_hooks_dir_resolves_the_shared_common_dir_in_a_real_worktree(tmp_path):
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
    assert (wt / ".git").is_file()

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
    monkeypatch.setattr(
        git, "_git", lambda args, *, cwd: " M src/a.py\n?? notes.txt\n\n"
    )
    assert git.status_porcelain(cwd="/x") == [" M src/a.py", "?? notes.txt"]
    monkeypatch.setattr(git, "_git", lambda args, *, cwd: "")
    assert git.status_porcelain(cwd="/x") == []


def test_epic_umbrella_exists_checks_remote_tracking_ref_first(monkeypatch):
    seen: list = []

    def fake_git(args, *, cwd):
        seen.append(args)
        return _ok()

    monkeypatch.setattr(git, "_probe", fake_git)
    assert git.epic_umbrella_exists("TRE04", cwd="/x") is True
    assert seen[0] == [
        "show-ref",
        "--verify",
        "--quiet",
        "refs/remotes/origin/TRE04/umbrella",
    ]


def test_epic_umbrella_exists_falls_back_to_local_head(monkeypatch):
    def fake_git(args, *, cwd):
        if args[-1] == "refs/heads/TRE04/umbrella":
            return _ok()
        return _fail()

    monkeypatch.setattr(git, "_probe", fake_git)
    assert git.epic_umbrella_exists("TRE04", cwd="/x") is True


def test_epic_umbrella_exists_false_when_no_umbrella(monkeypatch):
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _fail())
    assert git.epic_umbrella_exists("feature", cwd="/x") is False


def test_epic_umbrella_exists_launch_failure_raises_not_false(monkeypatch):
    def boom(args, *, cwd):
        raise ExecError(["git", "show-ref"], rc=None, cause=CAUSE_MISSING_BINARY)

    monkeypatch.setattr(git, "_probe", boom)
    with pytest.raises(ExecError):
        git.epic_umbrella_exists("TRE04", cwd="/x")


def test_commit_present_takes_sha_and_probes_cat_file(monkeypatch):
    seen = {}

    def fake(args, *, cwd):
        seen["args"] = args
        return _ok()

    monkeypatch.setattr(git, "_probe", fake)
    assert git.commit_present(Sha("a" * 40), cwd="/x") is True
    assert seen["args"] == ["cat-file", "-e", f"{'a' * 40}^{{commit}}"]
    monkeypatch.setattr(git, "_probe", lambda args, *, cwd: _fail())
    assert git.commit_present(Sha("a" * 40), cwd="/x") is False


def test_merge_base_returns_a_sha_value_object(monkeypatch):
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
    seen = {}

    def fake(args, *, cwd):
        seen["args"] = args
        return _ok()

    monkeypatch.setattr(git, "_probe", fake)
    assert git.is_ancestor(Sha("a" * 40), Sha("b" * 40), cwd="/x") is True
    assert seen["args"] == ["merge-base", "--is-ancestor", "a" * 40, "b" * 40]


def test_is_ancestor_false_on_nonancestor_and_on_error(monkeypatch):
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


def _ls_remote_line(sha: str, refname: str) -> str:
    return f"{sha}\t{refname}\n"


def test_remote_branch_exists_true_when_exact_ref_present(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(args, *, cwd=None, timeout=None):
        calls.append(args)
        return _ls_remote_line("a" * 40, "refs/heads/TRE04/umbrella")

    monkeypatch.setattr(git, "_git", fake_run)
    assert git.remote_branch_exists("TRE04/umbrella", cwd="/x") is True
    assert calls[0][-1] == "refs/heads/TRE04/umbrella"


def test_remote_branch_exists_false_when_absent(monkeypatch):
    monkeypatch.setattr(git, "_git", lambda args, *, cwd=None, timeout=None: "")
    assert git.remote_branch_exists("TRE04/umbrella", cwd="/x") is False


def test_remote_branch_exists_false_for_glob_metachar_branch(monkeypatch):
    def boom(args, *, cwd=None, timeout=None):
        raise AssertionError("glob-ish branch name must not reach git ls-remote")

    monkeypatch.setattr(git, "_git", boom)
    assert git.remote_branch_exists("TRE04/*", cwd="/x") is False
    assert git.remote_branch_exists("feat[01]", cwd="/x") is False
    assert git.remote_branch_exists("feat?", cwd="/x") is False


def test_remote_branch_exists_false_when_only_a_different_ref_matches(monkeypatch):
    def fake_run(args, *, cwd=None, timeout=None):
        return _ls_remote_line("b" * 40, "refs/heads/TRE04/umbrella-extra")

    monkeypatch.setattr(git, "_git", fake_run)
    assert git.remote_branch_exists("TRE04/umbrella", cwd="/x") is False


def test_remote_branch_exists_true_when_exact_ref_among_several(monkeypatch):
    def fake_run(args, *, cwd=None, timeout=None):
        return _ls_remote_line(
            "c" * 40, "refs/heads/TRE04/umbrella-extra"
        ) + _ls_remote_line("d" * 40, "refs/heads/TRE04/umbrella")

    monkeypatch.setattr(git, "_git", fake_run)
    assert git.remote_branch_exists("TRE04/umbrella", cwd="/x") is True


def _poisoned_clone_error(argv: list[str]) -> ExecError:
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
    warning = next(r for r in caplog.records if r.levelno == logging.WARNING)
    assert "/ref" in warning.getMessage()
    assert "#353" in warning.getMessage()


def test_clone_dissociated_removes_leftover_dest_before_retry(monkeypatch, tmp_path):
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
        "warning: Clone succeeded, but checkout failed.",
        "fatal: unable to parse commit " + "b" * 40,
    ],
)
def test_clone_dissociated_requires_both_markers_no_single_marker_retry(
    monkeypatch, stderr
):
    calls: list[list[str]] = []

    def fake(args, *, cwd=None, timeout=None):
        calls.append(args)
        raise ExecError(["git", *args], rc=128, stderr=stderr, cause=CAUSE_EXIT)

    monkeypatch.setattr(git, "_git", fake)
    with pytest.raises(ExecError):
        git.clone_dissociated("https://x/r.git", "/trees/leaf", reference="/ref")
    assert len(calls) == 1


def test_clone_dissociated_never_retries_a_timeout(monkeypatch):
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
    monkeypatch.setattr(
        git,
        "_probe",
        lambda args, *, cwd: _fail("fatal: bad revision 'origin/gone...HEAD'", rc=128),
    )
    assert git.changed_paths_since("origin/gone", cwd="/tree") is None
