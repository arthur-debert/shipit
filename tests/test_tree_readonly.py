"""Tests for ``tree.readonly`` — the shared, read-only (reviewer) Tree (ADR-0018).

Three things to pin, mirroring ``test_tree_create.py`` / ``test_tree_layout.py``:

- the PURE planner (``readonly_plan``) resolves a deterministic, hash-free leaf so a
  Tree is shared per ``(repo, branch)`` and rejects a branch that sanitizes to nothing;
- provisioning is the read-only VARIANT — clone + checkout only, NO ``.treeinclude``
  copy, NO pixi/provisioning, and the working files left ``chmod``'d read-only — and a
  second reviewer on the same head REUSES the clone instead of re-cloning;
- ``chmod_readonly`` strips the write bits from working files but never from ``.git``.

The reuse + "no provisioning" assertions mock the git boundary (no real clone); one
real-git smoke proves the checkout + read-only chmod end to end.
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
    chmod_writable,
    create_readonly,
    readonly_plan,
    remove_tree,
)

#: The canonical Repo identity the plans in this file namespace under.
REPO = repo_from_slug("acme/widget")


# --- the pure planner --------------------------------------------------------


def test_readonly_plan_is_shared_per_repo_branch_with_no_hash(tmp_path):
    # The leaf is deterministic from (org, repo, branch) — no agent hash — so two
    # reviewers on the same head resolve to the IDENTICAL dir (the sharing key).
    root = tmp_path / "trees"
    one = readonly_plan(repo=REPO, branch="TRE03/WS03", root=root)
    two = readonly_plan(repo=REPO, branch="TRE03/WS03", root=root)

    assert one == two
    # `review` kind segment + a leaf of the sanitized branch (slashes → '-', lowercased)
    # plus a stable branch-name hash disambiguator — but NO per-Run agent hash.
    assert one.dir.parent == root / "acme" / "widget" / "review"
    assert one.dir.name.startswith("tre03-ws03-")
    # The branch is kept VERBATIM for the checkout (the real remote branch name).
    assert one.branch == "TRE03/WS03"


def test_readonly_plan_distinct_branches_get_distinct_dirs(tmp_path):
    root = tmp_path / "trees"
    a = readonly_plan(repo=REPO, branch="TRE03/WS03", root=root)
    b = readonly_plan(repo=REPO, branch="TRE03/WS04", root=root)
    assert a.dir != b.dir


def test_readonly_plan_slug_colliding_branches_get_distinct_dirs(tmp_path):
    # Sanitization is lossy: `feat/a-b` and `feat/a/b` both slug to `feat-a-b`. The
    # branch-name hash disambiguator must keep them in DISTINCT shared slots so one PR's
    # reviewer never reuses another PR's checkout — while the real branch (kept verbatim
    # for checkout) is preserved on each.
    root = tmp_path / "trees"
    a = readonly_plan(repo=REPO, branch="feat/a-b", root=root)
    b = readonly_plan(repo=REPO, branch="feat/a/b", root=root)

    assert a.dir.parent == b.dir.parent  # same review/ kind dir
    assert a.dir != b.dir  # ...but different leaves (the hash differs)
    assert a.branch == "feat/a-b" and b.branch == "feat/a/b"  # verbatim for checkout


@pytest.mark.parametrize("branch", ["", "   ", "///", "."])
def test_readonly_plan_rejects_empty_sanitized_branch(tmp_path, branch):
    with pytest.raises(ValueError, match="alphanumeric"):
        readonly_plan(repo=REPO, branch=branch, root=tmp_path)


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


def test_chmod_writable_restores_what_chmod_readonly_cleared(tmp_path):
    tree = tmp_path / "tree"
    (tree / "pkg").mkdir(parents=True)
    f = tree / "pkg" / "mod.py"
    f.write_text("x = 1\n")
    chmod_readonly(tree)

    chmod_writable(tree)

    assert f.stat().st_mode & stat.S_IWUSR  # file writable again
    assert (tree / "pkg").stat().st_mode & stat.S_IWUSR  # dir writable again
    assert tree.stat().st_mode & stat.S_IWUSR  # root writable again


# --- provisioning: the read-only variant (mocked git boundary) ---------------


def _mock_git_boundary(monkeypatch, *, files):
    """Patch the git boundary so a "clone" just makes the dest + the given files.

    Returns the call-count dict so a test can assert the clone ran exactly once
    (and is REUSED, not repeated, on a second create).
    """
    counts = {"clone": 0, "fetch": 0, "checkout": 0, "reset": 0}

    def fake_clone(url, dest, *, reference):
        counts["clone"] += 1
        d = Path(dest)
        d.mkdir(parents=True)
        (d / ".git").mkdir()  # mark it a real clone (reuse keys off this)
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
        "reset_hard",
        lambda *a, **k: counts.__setitem__("reset", counts["reset"] + 1),
    )
    return counts


def test_create_readonly_clones_checks_out_and_chmods_no_provisioning(
    tmp_path, monkeypatch
):
    plan = readonly_plan(repo=REPO, branch="feat/x", root=tmp_path / "trees")
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
    # clone + fetch + plain checkout of the EXISTING branch, each exactly once; a FRESH
    # create does no reset (that is the reuse-refresh path only).
    assert counts == {"clone": 1, "fetch": 1, "checkout": 1, "reset": 0}
    # The working file is left read-only (the ADR-0018 guardrail).
    assert not ((plan.dir / "README.md").stat().st_mode & 0o222)
    # The temp clone path was renamed into the shared leaf — no leftover sibling.
    assert not plan.dir.with_name(f"{plan.dir.name}.tmp-{os.getpid()}").exists()


def test_create_readonly_skips_treeinclude(tmp_path, monkeypatch):
    # The read-only variant does NOT apply .treeinclude: a gitignored-but-needed file
    # in the source is NOT copied into a reviewer Tree (it only reads tracked code).
    source = tmp_path / "source"
    source.mkdir()
    (source / ".treeinclude").write_text(".env\n")
    (source / ".env").write_text("TOKEN=1")
    _mock_git_boundary(monkeypatch, files={"README.md": "hi\n"})

    plan = readonly_plan(repo=REPO, branch="feat/x", root=tmp_path / "trees")
    create_readonly(plan, source_repo=str(source), github_url="url")

    assert not (plan.dir / ".env").exists()  # .treeinclude was NOT applied


def test_create_readonly_second_reviewer_reuses_the_clone(tmp_path, monkeypatch):
    # Acceptance #157: a second reviewer on the same (repo, branch) REUSES the shared
    # clone — it does not re-clone.
    plan = readonly_plan(repo=REPO, branch="feat/x", root=tmp_path / "trees")
    counts = _mock_git_boundary(monkeypatch, files={"README.md": "hi\n"})

    first = create_readonly(plan, source_repo="/ref", github_url="url")
    second = create_readonly(plan, source_repo="/ref", github_url="url")

    assert first.path == second.path
    assert counts["clone"] == 1  # the second reviewer did NOT re-clone


def test_create_readonly_reuse_refreshes_to_current_head_and_re_guards(
    tmp_path, monkeypatch
):
    # On reuse the shared clone must be REFRESHED to the current remote head (the PR may
    # have advanced) and the read-only guard re-applied — never served stale. The refresh
    # is fetch + checkout + reset --hard origin/<branch>.
    plan = readonly_plan(repo=REPO, branch="feat/x", root=tmp_path / "trees")
    counts = _mock_git_boundary(monkeypatch, files={"README.md": "hi\n"})

    create_readonly(plan, source_repo="/ref", github_url="url")  # first reviewer
    work = plan.dir / "README.md"
    # Simulate a co-tenant having relaxed perms; the re-guard must restore read-only.
    work.chmod(0o644)

    create_readonly(plan, source_repo="/ref", github_url="url")  # second reviewer

    assert counts["clone"] == 1  # still the one shared clone
    assert counts["reset"] == 1  # ...but reset --hard to the current head on reuse
    assert not (work.stat().st_mode & 0o222)  # read-only guard re-applied


def test_create_readonly_rolls_back_partial_tree_on_failure(tmp_path, monkeypatch):
    # If a post-clone step fails, the half-built leaf must not survive — otherwise the
    # next reviewer would "reuse" a broken clone.
    plan = readonly_plan(repo=REPO, branch="feat/x", root=tmp_path / "trees")
    _mock_git_boundary(monkeypatch, files={"README.md": "hi\n"})
    monkeypatch.setattr(
        git,
        "checkout",
        lambda *a, **k: (_ for _ in ()).throw(ExecError(["gh"], rc=1, stderr="boom")),
    )

    with pytest.raises(ExecError):
        create_readonly(plan, source_repo="/ref", github_url="url")
    assert not plan.dir.exists()


def test_create_readonly_refuses_non_clone_in_the_shared_slot(tmp_path, monkeypatch):
    # A pre-existing leaf that is NOT a clone (no .git) is refused, not cloned into or
    # deleted — it would be a stray dir squatting the shared review slot.
    plan = readonly_plan(repo=REPO, branch="feat/x", root=tmp_path / "trees")
    plan.dir.mkdir(parents=True)
    (plan.dir / "stray.txt").write_text("not a clone")

    def boom(*a, **k):
        raise AssertionError("must not clone into an occupied non-clone slot")

    monkeypatch.setattr(git, "clone_dissociated", boom)

    with pytest.raises(FileExistsError, match="not a clone"):
        create_readonly(plan, source_repo="/ref", github_url="url")
    assert (plan.dir / "stray.txt").read_text() == "not a clone"  # untouched


# --- one real-git smoke: checkout of an existing branch + read-only ----------


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

    plan = readonly_plan(repo=REPO, branch="feat/x", root=tmp_path / "trees")
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

    plan = readonly_plan(repo=REPO, branch="feat/x", root=tmp_path / "trees")
    # file:// forces the real pack transport — a plain path URL hardlinks
    # objects and never reproduces the clone-time-checkout death.
    with caplog.at_level(logging.WARNING, logger="shipit.git"):
        tree = create_readonly(
            plan, source_repo=str(reference), github_url=f"file://{remote}"
        )
    dest = Path(tree.path)

    # The reviewer Tree is real, on the PR head, and fully independent.
    assert (dest / "feature.txt").read_text() == "under review\n"
    assert not (dest / ".git" / "objects" / "info" / "alternates").exists()
    # First-attempt success: the #353 retry WARNING never fired.
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_case_divergent_sources_share_one_review_tree(tmp_path):
    # Reviewer sharing is keyed per (repo, branch): a mixed-case API slug and the
    # canonical identity must resolve the SAME shared leaf, or two reviewers on
    # one PR head would silently clone twice (the ADR-0024 disease).
    a = readonly_plan(
        repo=repo_from_slug("AcMe/WiDgEt"), branch="feat/x", root=tmp_path
    )
    b = readonly_plan(repo=REPO, branch="feat/x", root=tmp_path)
    assert a.dir == b.dir
    assert a.dir.parent == tmp_path / "acme" / "widget" / "review"
