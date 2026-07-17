"""Tests for ``tree.readonly`` — the per-Run, read-only (reviewer) Tree (ADR-0074).

Since ADR-0074 a reviewer clone is PER-RUN, not shared: ADR-0018's read-only
*mode* stands (the working tree is still ``chmod``'d read-only), but the *sharing*
is gone. So the whole reuse/refresh/acquisition-stamp machinery — a deterministic
``(repo, branch)`` leaf, ``_reuse_or_refuse``, ``_refresh_readonly``,
``_stamp_acquisition``, ``chmod_writable`` — is retired with the sharing it existed
for. What remains:

- the PURE planner (``readonly_plan``) resolves the single flat leaf
  ``<repo>-<agent>-<timestamp>-<id>`` (unique per Run, its <id> a fresh UUID) and
  rejects an empty branch;
- provisioning is the read-only VARIANT — clone + checkout + submodule init only,
  NO ``.treeinclude`` copy, NO pixi/provisioning — with the working files left
  ``chmod``'d read-only, and a pre-existing leaf REFUSED (a per-Run id never
  legitimately collides);
- ``chmod_readonly`` strips the write bits from working files but never from ``.git``,
  and ``remove_tree`` reclaims a read-only checkout.

The "no provisioning" assertions mock the git boundary (no real clone); two real-git
smokes prove the checkout + read-only chmod (and the #372 commit-graph donor) end to end.
"""

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

#: The canonical Repo identity the plans in this file namespace under — its NAME
#: (``widget``) leads the flat leaf.
REPO = repo_from_slug("acme/widget")

#: The flat-leaf coordinates a per-Run reviewer Tree carries (ADR-0074): the backend
#: binary name, the timestamp stamp, and this Run's own fresh UUID.
AGENT = "codex"
CREATED = "20260717-081333"
TREE_ID = "619cf51a-f501-44dc-992f-74df773204aa"


def _plan(**over):
    """A per-Run read-only plan with the flat-leaf coordinates, ``over`` winning."""
    base = dict(
        repo=REPO,
        branch="feat/x",
        agent=AGENT,
        created=CREATED,
        tree_id=TREE_ID,
    )
    base.update(over)
    return readonly_plan(**base)


# --- the pure planner --------------------------------------------------------


def test_readonly_plan_resolves_the_flat_per_run_leaf(tmp_path):
    # ADR-0074: the dir is the single flat shape every Tree uses —
    # <root>/<repo>-<agent>-<timestamp>-<id> — with no `review` segment and no shared
    # branch-keyed leaf.
    root = tmp_path / "trees"
    p = _plan(root=root)
    assert p.dir == root / f"widget-{AGENT}-{CREATED}-{TREE_ID}"
    # The branch is kept VERBATIM for the checkout (the real remote branch name).
    assert p.branch == "feat/x"


def test_readonly_plan_is_per_run_not_shared(tmp_path):
    # Two reviewer Runs on the SAME (repo, branch) resolve to DISTINCT dirs now: each
    # carries its own fresh UUID, so there is no co-tenant to race (sharing is gone).
    root = tmp_path / "trees"
    one = _plan(root=root, tree_id="11111111-1111-4111-8111-111111111111")
    two = _plan(root=root, tree_id="22222222-2222-4222-8222-222222222222")
    assert one.dir != two.dir
    assert one.branch == two.branch == "feat/x"


def test_readonly_plan_branch_does_not_shape_the_leaf(tmp_path):
    # The dir leaf no longer derives from the branch (branch sanitization went with the
    # sharing that needed it), so slug-colliding branches on the same Run id land the
    # SAME leaf — the id is the disambiguator now, not the branch.
    root = tmp_path / "trees"
    a = _plan(root=root, branch="feat/a-b")
    b = _plan(root=root, branch="feat/a/b")
    assert a.dir == b.dir  # same coordinates → same leaf
    assert a.branch == "feat/a-b" and b.branch == "feat/a/b"  # verbatim for checkout


@pytest.mark.parametrize("branch", ["", "   "])
def test_readonly_plan_rejects_empty_branch(tmp_path, branch):
    # A reviewer checks out an EXISTING head, so an empty branch is an unusable checkout
    # target — refused (the dir leaf no longer needs the branch, so this is the only guard).
    with pytest.raises(ValueError, match="non-empty remote branch"):
        _plan(root=tmp_path, branch=branch)


# --- chmod_readonly ----------------------------------------------------------


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

    # A working file lost every write bit (owner/group/other).
    assert not (work.stat().st_mode & 0o222)
    assert work.stat().st_mode & stat.S_IRUSR  # still readable
    # The working DIR is read-only too — on Unix, deleting/creating an entry is governed
    # by the directory mode, so a writable dir would defeat the guard.
    assert not ((tmp_path / "src").stat().st_mode & 0o222)
    assert (tmp_path / "src").stat().st_mode & stat.S_IXUSR  # still traversable
    # The Tree root itself is protected (no top-level file creation).
    assert not (tmp_path.stat().st_mode & 0o222)
    # The repo database under .git is untouched (git still needs it writable).
    assert gitfile.stat().st_mode & stat.S_IWUSR
    assert gitdir.stat().st_mode & stat.S_IWUSR


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_chmod_readonly_skips_symlinks_and_does_not_follow_them(tmp_path):
    # `stat`/`chmod` follow symlinks, so a link in the checkout could otherwise
    # re-permission a target OUTSIDE the Tree; a broken link would raise. The guard must
    # skip symlinks entirely, leaving both the link and any target untouched.
    tree = tmp_path / "tree"
    tree.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("external\n")
    outside.chmod(0o644)
    (tree / "good").symlink_to(outside)  # link to a real external target
    (tree / "broken").symlink_to(tree / "nope")  # dangling link

    chmod_readonly(tree)  # must not raise on the broken link

    # The external target keeps its write bit — the guard did not follow the link.
    assert outside.stat().st_mode & 0o222
    # The links themselves are still links (untouched), the broken one still dangling.
    assert (tree / "good").is_symlink() and (tree / "broken").is_symlink()
    remove_tree(tree)  # tidy the read-only tree (and exercise the reclaim path)


def test_remove_tree_reclaims_a_read_only_checkout(tmp_path):
    # The guard makes dirs read-only, which would make a plain shutil.rmtree fail
    # (deleting an entry needs a writable parent dir). remove_tree must restore perms
    # and reclaim the Tree completely.
    tree = tmp_path / "tree"
    (tree / "pkg").mkdir(parents=True)
    (tree / "pkg" / "mod.py").write_text("x = 1\n")
    (tree / "top.txt").write_text("hi\n")
    chmod_readonly(tree)
    assert not (tree / "pkg").stat().st_mode & 0o222  # precondition: read-only dir

    deleted = remove_tree(tree)

    assert not tree.exists()
    assert deleted is True  # a present Tree that came off disk reports True


def test_remove_tree_is_a_noop_on_a_missing_path(tmp_path):
    # A missing path is a no-op AND reports False, so callers (gc) never credit a
    # removal that did not happen.
    assert remove_tree(tmp_path / "does-not-exist") is False  # must not raise


# --- provisioning: the read-only variant (mocked git boundary) ---------------


def _mock_git_boundary(monkeypatch, *, files):
    """Patch the git boundary so a "clone" just makes the dest + the given files.

    Returns the call-count dict so a test can assert clone/fetch/checkout/submodule
    each ran exactly once (a per-Run create is one shot — no reuse, no reset).
    """
    counts = {"clone": 0, "fetch": 0, "checkout": 0, "submodule": 0}

    def fake_clone(url, dest, *, reference):
        counts["clone"] += 1
        d = Path(dest)
        d.mkdir(parents=True)
        (d / ".git").mkdir()  # mark it a real clone
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

    # A reviewer Tree must NEVER provision: if provisioning were wired, this guard
    # would fire. (readonly.py imports no run_provision; this pins that structurally.)
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
    # clone + fetch + plain checkout of the EXISTING branch, then submodule init, each
    # exactly once (#485). A per-Run create is one shot: no reuse, no reset.
    assert counts == {"clone": 1, "fetch": 1, "checkout": 1, "submodule": 1}
    # The working file is left read-only (the ADR-0018 guardrail).
    assert not ((plan.dir / "README.md").stat().st_mode & 0o222)
    # The temp clone path was renamed into the leaf — no leftover sibling.
    assert not plan.dir.with_name(f"{plan.dir.name}.tmp-{os.getpid()}").exists()


def test_create_readonly_skips_treeinclude(tmp_path, monkeypatch):
    # The read-only variant does NOT apply .treeinclude: a gitignored-but-needed file
    # in the source is NOT copied into a reviewer Tree (it only reads tracked code).
    source = tmp_path / "source"
    source.mkdir()
    (source / ".treeinclude").write_text(".env\n")
    (source / ".env").write_text("TOKEN=1")
    _mock_git_boundary(monkeypatch, files={"README.md": "hi\n"})

    plan = _plan(root=tmp_path / "trees")
    create_readonly(plan, source_repo=str(source), github_url="url")

    assert not (plan.dir / ".env").exists()  # .treeinclude was NOT applied


def test_create_readonly_rolls_back_partial_tree_on_failure(tmp_path, monkeypatch):
    # If a post-clone step fails, the half-built leaf must not survive — otherwise a
    # stray broken clone would litter the root.
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
    # A per-Run leaf carries a fresh UUID, so a pre-existing dest means a REUSED id —
    # a programming error. It is refused up front (never cloned into or deleted), so a
    # failed create can never clobber a directory already on disk.
    plan = _plan(root=tmp_path / "trees")
    plan.dir.mkdir(parents=True)
    (plan.dir / "stray.txt").write_text("not mine")

    def boom(*a, **k):
        raise AssertionError("must not clone into an occupied leaf")

    monkeypatch.setattr(git, "clone_dissociated", boom)

    with pytest.raises(FileExistsError, match="already exists"):
        create_readonly(plan, source_repo="/ref", github_url="url")
    assert (plan.dir / "stray.txt").read_text() == "not mine"  # untouched


def test_create_readonly_tags_tree_created(tmp_path, monkeypatch, caplog):
    """A per-Run read-only Tree is a Tree birth — the `tree.created` dev-cycle event
    (LOG04-WS02 / ADR-0032)."""
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


# --- identity threading ------------------------------------------------------


def test_case_divergent_sources_share_one_repo_prefix(tmp_path):
    # Reviewer Trees are per-Run now, so two Runs never share a dir — but the flat
    # leaf's <repo> prefix must still normalize: a mixed-case API slug and the
    # canonical identity resolve the IDENTICAL leaf when the other coordinates match
    # (the ADR-0024 disease stays out of the plumbing).
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


# --- real-git smokes ---------------------------------------------------------


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
    # The PR head the reviewer will check out.
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

    # On the EXISTING PR-head branch, with that branch's content present.
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
    # Independent clone (dissociated) and working files left read-only.
    assert not (dest / ".git" / "objects" / "info" / "alternates").exists()
    assert not (dest / "README.md").stat().st_mode & 0o222
    assert not (dest / "feature.txt").stat().st_mode & 0o222


def test_create_readonly_real_git_survives_a_commit_graph_bearing_reference(
    tmp_path, caplog
):
    # #372: EVERY detached local review from an ephemeral session Tree begins
    # with this exact call shape — create_readonly cold-cloning with the session
    # Tree as --reference. A commit-graph in that donor (auto-maintenance writes
    # one) killed the clone on git 2.54; the review Tree must now come up on the
    # FIRST attempt (no #353 degraded full-clone retry).
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

    # The donor: a session-Tree stand-in poisoned with a commit-graph.
    reference = tmp_path / "ref"
    _git(["clone", str(remote), str(reference)], tmp_path)
    _git(["commit-graph", "write", "--reachable", "--split"], reference)
    assert (reference / ".git" / "objects" / "info" / "commit-graphs").exists()

    plan = _plan(root=tmp_path / "trees")
    # file:// forces the real pack transport — a plain path URL hardlinks
    # objects and never reproduces the clone-time-checkout death.
    with caplog.at_level(logging.WARNING, logger="shipit.git"):
        tree = create_readonly(
            plan, source_repo=str(reference), github_url=remote.as_uri()
        )
    dest = Path(tree.path)

    # The reviewer Tree is real, on the PR head, and fully independent.
    assert (dest / "feature.txt").read_text() == "under review\n"
    assert not (dest / ".git" / "objects" / "info" / "alternates").exists()
    # First-attempt success: the #353 retry WARNING never fired. Filter by
    # logger so an unrelated warning elsewhere cannot flake this assertion.
    assert not [
        r
        for r in caplog.records
        if r.name == "shipit.git" and r.levelno >= logging.WARNING
    ]
