from __future__ import annotations

import logging
import os
import stat
import subprocess
from pathlib import Path

import pytest

from shipit import git
from shipit.execrun import ExecError
from shipit.identity import repo_from_slug
from shipit.tree.readonly import (
    chmod_readonly,
    create_readonly,
    readonly_plan,
    remove_tree,
)

REPO = repo_from_slug("acme/widget")

AGENT = "codex"
CREATED = "20260717-081333"
TREE_ID = "619cf51a-f501-44dc-992f-74df773204aa"


def _plan(**over):
    base = dict(
        repo=REPO,
        branch="feat/x",
        agent=AGENT,
        created=CREATED,
        tree_id=TREE_ID,
    )
    base.update(over)
    return readonly_plan(**base)


def test_readonly_plan_resolves_the_flat_per_run_leaf(tmp_path):
    root = tmp_path / "trees"
    p = _plan(root=root)
    assert p.dir == root / f"widget-{AGENT}-{CREATED}-{TREE_ID}"
    assert p.branch == "feat/x"


def test_readonly_plan_is_per_run_not_shared(tmp_path):
    root = tmp_path / "trees"
    one = _plan(root=root, tree_id="11111111-1111-4111-8111-111111111111")
    two = _plan(root=root, tree_id="22222222-2222-4222-8222-222222222222")
    assert one.dir != two.dir
    assert one.branch == two.branch == "feat/x"


def test_readonly_plan_branch_does_not_shape_the_leaf(tmp_path):
    root = tmp_path / "trees"
    a = _plan(root=root, branch="feat/a-b")
    b = _plan(root=root, branch="feat/a/b")
    assert a.dir == b.dir
    assert a.branch == "feat/a-b" and b.branch == "feat/a/b"


@pytest.mark.parametrize("branch", ["", "   "])
def test_readonly_plan_rejects_empty_branch(tmp_path, branch):
    with pytest.raises(ValueError, match="non-empty remote branch"):
        _plan(root=tmp_path, branch=branch)


def test_chmod_readonly_strips_write_bits_from_files_but_not_git(tmp_path):
    (tmp_path / "src").mkdir()
    work = tmp_path / "src" / "main.py"
    work.write_text("print('hi')\n")
    work.chmod(0o644)
    gitdir = tmp_path / ".git"
    gitdir.mkdir()
    gitfile = gitdir / "HEAD"
    gitfile.write_text("ref: refs/heads/main\n")
    gitfile.chmod(0o644)

    chmod_readonly(tmp_path)

    assert not (work.stat().st_mode & 0o222)
    assert work.stat().st_mode & stat.S_IRUSR
    assert not ((tmp_path / "src").stat().st_mode & 0o222)
    assert (tmp_path / "src").stat().st_mode & stat.S_IXUSR
    assert not (tmp_path.stat().st_mode & 0o222)
    assert gitfile.stat().st_mode & stat.S_IWUSR
    assert gitdir.stat().st_mode & stat.S_IWUSR


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_chmod_readonly_skips_symlinks_and_does_not_follow_them(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("external\n")
    outside.chmod(0o644)
    (tree / "good").symlink_to(outside)
    (tree / "broken").symlink_to(tree / "nope")

    chmod_readonly(tree)

    assert outside.stat().st_mode & 0o222
    assert (tree / "good").is_symlink() and (tree / "broken").is_symlink()
    remove_tree(tree)


def test_remove_tree_reclaims_a_read_only_checkout(tmp_path):
    tree = tmp_path / "tree"
    (tree / "pkg").mkdir(parents=True)
    (tree / "pkg" / "mod.py").write_text("x = 1\n")
    (tree / "top.txt").write_text("hi\n")
    chmod_readonly(tree)
    assert not (tree / "pkg").stat().st_mode & 0o222

    deleted = remove_tree(tree)

    assert not tree.exists()
    assert deleted is True


def test_remove_tree_is_a_noop_on_a_missing_path(tmp_path):
    assert remove_tree(tmp_path / "does-not-exist") is False


def _mock_git_boundary(monkeypatch, *, files):
    counts = {"clone": 0, "fetch": 0, "checkout": 0, "submodule": 0}

    def fake_clone(url, dest, *, reference):
        counts["clone"] += 1
        d = Path(dest)
        d.mkdir(parents=True)
        (d / ".git").mkdir()
        for name, body in files.items():
            (d / name).write_text(body)

    monkeypatch.setattr(git, "clone_dissociated", fake_clone)
    monkeypatch.setattr(
        git, "fetch", lambda **k: counts.__setitem__("fetch", counts["fetch"] + 1)
    )
    monkeypatch.setattr(
        git,
        "checkout",
        lambda *a, **k: counts.__setitem__("checkout", counts["checkout"] + 1),
    )
    monkeypatch.setattr(
        git,
        "submodule_update_init",
        lambda **k: counts.__setitem__("submodule", counts["submodule"] + 1),
    )
    return counts


def test_create_readonly_clones_checks_out_and_chmods_no_provisioning(
    tmp_path, monkeypatch
):
    plan = _plan(root=tmp_path / "trees")
    counts = _mock_git_boundary(monkeypatch, files={"README.md": "hi\n"})

    import shipit.tree.create as create_mod

    monkeypatch.setattr(
        create_mod,
        "run_provision",
        lambda *a, **k: pytest.fail("a read-only Tree must not provision"),
    )

    tree = create_readonly(plan, source_repo="/ref", github_url="url")

    assert Path(tree.path) == plan.dir
    assert tree.branch == "feat/x"
    assert tree.base == "origin/feat/x"
    assert counts == {"clone": 1, "fetch": 1, "checkout": 1, "submodule": 1}
    assert not ((plan.dir / "README.md").stat().st_mode & 0o222)
    assert not plan.dir.with_name(f"{plan.dir.name}.tmp-{os.getpid()}").exists()


def test_create_readonly_skips_treeinclude(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / ".treeinclude").write_text(".env\n")
    (source / ".env").write_text("TOKEN=1")
    _mock_git_boundary(monkeypatch, files={"README.md": "hi\n"})

    plan = _plan(root=tmp_path / "trees")
    create_readonly(plan, source_repo=str(source), github_url="url")

    assert not (plan.dir / ".env").exists()


def test_create_readonly_rolls_back_partial_tree_on_failure(tmp_path, monkeypatch):
    plan = _plan(root=tmp_path / "trees")
    _mock_git_boundary(monkeypatch, files={"README.md": "hi\n"})
    monkeypatch.setattr(
        git,
        "checkout",
        lambda *a, **k: (_ for _ in ()).throw(ExecError(["gh"], rc=1, stderr="boom")),
    )

    with pytest.raises(ExecError):
        create_readonly(plan, source_repo="/ref", github_url="url")
    assert not plan.dir.exists()


def test_create_readonly_refuses_a_pre_existing_leaf(tmp_path, monkeypatch):
    plan = _plan(root=tmp_path / "trees")
    plan.dir.mkdir(parents=True)
    (plan.dir / "stray.txt").write_text("not mine")

    def boom(*a, **k):
        raise AssertionError("must not clone into an occupied leaf")

    monkeypatch.setattr(git, "clone_dissociated", boom)

    with pytest.raises(FileExistsError, match="already exists"):
        create_readonly(plan, source_repo="/ref", github_url="url")
    assert (plan.dir / "stray.txt").read_text() == "not mine"


def test_create_readonly_tags_tree_created(tmp_path, monkeypatch, caplog):
    from shipit import events

    plan = _plan(root=tmp_path / "trees")
    _mock_git_boundary(monkeypatch, files={"README.md": "hi\n"})

    with caplog.at_level(logging.INFO, logger="shipit.tree"):
        create_readonly(plan, source_repo="/ref", github_url="url")
    assert [
        getattr(r, events.EXTRA_KEY)
        for r in caplog.records
        if getattr(r, events.EXTRA_KEY, None)
    ] == ["tree.created"]


def test_case_divergent_sources_share_one_repo_prefix(tmp_path):
    a = readonly_plan(
        repo=repo_from_slug("AcMe/WiDgEt"),
        branch="feat/x",
        agent=AGENT,
        created=CREATED,
        tree_id=TREE_ID,
        root=tmp_path,
    )
    b = _plan(root=tmp_path)
    assert a.dir == b.dir
    assert a.dir.name.startswith("widget-")


def _git(args, cwd):
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@e.com",
            "-c",
            "user.name=T",
            "-c",
            "init.defaultBranch=main",
            "-c",
            "protocol.file.allow=always",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def test_create_readonly_real_git_checks_out_existing_branch_read_only(tmp_path):
    remote = tmp_path / "remote"
    remote.mkdir()
    _git(["init"], remote)
    (remote / "README.md").write_text("hello\n")
    _git(["add", "."], remote)
    _git(["commit", "-m", "init"], remote)
    _git(["branch", "-M", "main"], remote)
    _git(["checkout", "-b", "feat/x"], remote)
    (remote / "feature.txt").write_text("under review\n")
    _git(["add", "."], remote)
    _git(["commit", "-m", "feat"], remote)
    _git(["checkout", "main"], remote)

    reference = tmp_path / "ref"
    _git(["clone", str(remote), str(reference)], tmp_path)

    plan = _plan(root=tmp_path / "trees")
    tree = create_readonly(plan, source_repo=str(reference), github_url=str(remote))
    dest = Path(tree.path)

    assert (
        subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=dest,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "feat/x"
    )
    assert (dest / "feature.txt").read_text() == "under review\n"
    assert not (dest / ".git" / "objects" / "info" / "alternates").exists()
    assert not (dest / "README.md").stat().st_mode & 0o222
    assert not (dest / "feature.txt").stat().st_mode & 0o222


def test_create_readonly_real_git_survives_a_commit_graph_bearing_reference(
    tmp_path, caplog
):
    remote = tmp_path / "remote"
    remote.mkdir()
    _git(["init"], remote)
    (remote / "README.md").write_text("hello\n")
    _git(["add", "."], remote)
    _git(["commit", "-m", "init"], remote)
    _git(["branch", "-M", "main"], remote)
    _git(["checkout", "-b", "feat/x"], remote)
    (remote / "feature.txt").write_text("under review\n")
    _git(["add", "."], remote)
    _git(["commit", "-m", "feat"], remote)
    _git(["checkout", "main"], remote)

    reference = tmp_path / "ref"
    _git(["clone", str(remote), str(reference)], tmp_path)
    _git(["commit-graph", "write", "--reachable", "--split"], reference)
    assert (reference / ".git" / "objects" / "info" / "commit-graphs").exists()

    plan = _plan(root=tmp_path / "trees")
    with caplog.at_level(logging.WARNING, logger="shipit.git"):
        tree = create_readonly(
            plan, source_repo=str(reference), github_url=remote.as_uri()
        )
    dest = Path(tree.path)

    assert (dest / "feature.txt").read_text() == "under review\n"
    assert not (dest / ".git" / "objects" / "info" / "alternates").exists()
    assert not [
        r
        for r in caplog.records
        if r.name == "shipit.git" and r.levelno >= logging.WARNING
    ]
