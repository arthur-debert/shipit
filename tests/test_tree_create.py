"""Integration smoke for ``tree.create.create`` — ONE real-git happy path.

Asserts the EXTERNAL result of a real ``git clone --reference … --dissociate``:
the new Tree is a fully-independent clone (no ``alternates``), sits on the planned
branch, its ``origin`` points at the remote, and the READY summary is correct.
The clone-strategy details are otherwise covered by the pure ``layout`` unit tests.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from shipit import gh
from shipit.tree.create import create, create_from_source
from shipit.tree.layout import TreeSpec


def _git(args: list[str], cwd: Path) -> str:
    """Run git with a deterministic identity/config, returning stdout."""
    proc = subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@example.com",
            "-c",
            "user.name=Test",
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
    return proc.stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    """A real upstream repo (stands in for the GitHub URL) with one commit on main."""
    repo = tmp_path / "remote"
    repo.mkdir()
    _git(["init"], cwd=repo)
    (repo / "README.md").write_text("hello tree\n")
    _git(["add", "."], cwd=repo)
    _git(["commit", "-m", "init"], cwd=repo)
    _git(["branch", "-M", "main"], cwd=repo)
    return repo


@pytest.fixture
def reference(tmp_path: Path, remote: Path) -> Path:
    """A local checkout of the remote — the ``--reference`` object donor."""
    ref = tmp_path / "ref"
    _git(["clone", str(remote), str(ref)], cwd=tmp_path)
    return ref


def _spec(tmp_path: Path) -> TreeSpec:
    return TreeSpec(
        org="acme",
        repo="widget",
        agent_hash="abcd1234",
        issue=123,
        slug="smoke",
        root=tmp_path / "trees",
    )


def test_create_produces_an_independent_dissociated_clone(
    tmp_path: Path, remote: Path, reference: Path
):
    spec = _spec(tmp_path)
    tree = create(spec, source_repo=str(reference), github_url=str(remote))

    dest = Path(tree.path)

    # READY summary is the planned {path, branch, base}.
    assert dest == tmp_path / "trees" / "acme" / "widget" / "issues" / "123-abcd1234"
    assert tree.branch == "fix/123-smoke"
    assert tree.base == "origin/main"

    # Independent: --dissociate removed the alternates link entirely.
    assert not (dest / ".git" / "objects" / "info" / "alternates").exists()

    # On the planned branch.
    assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=dest) == "fix/123-smoke"

    # origin points at the remote, so git/gh work inside the Tree.
    assert _git(["remote", "get-url", "origin"], cwd=dest) == str(remote)

    # The upstream content is really there.
    assert (dest / "README.md").read_text() == "hello tree\n"


def test_create_from_source_resolves_origin_url(
    tmp_path: Path, remote: Path, reference: Path
):
    # create_from_source clones from the URL the reference checkout already uses.
    spec = _spec(tmp_path)
    tree = create_from_source(spec, source_repo=str(reference))

    dest = Path(tree.path)
    assert _git(["remote", "get-url", "origin"], cwd=dest) == str(remote)
    assert not (dest / ".git" / "objects" / "info" / "alternates").exists()


def test_create_rolls_back_partial_tree_on_failure(
    tmp_path: Path, remote: Path, reference: Path, monkeypatch
):
    # If a post-clone step fails, the half-built leaf must not survive — otherwise
    # the next run trips over a partial directory.
    spec = _spec(tmp_path)

    def boom(*args, **kwargs):
        raise gh.GhError("checkout blew up")

    monkeypatch.setattr(gh, "git_checkout_new_branch", boom)

    with pytest.raises(gh.GhError):
        create(spec, source_repo=str(reference), github_url=str(remote))

    dest = tmp_path / "trees" / "acme" / "widget" / "issues" / "123-abcd1234"
    assert not dest.exists()
