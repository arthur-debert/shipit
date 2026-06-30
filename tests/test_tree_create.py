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
from shipit.tree import create as create_mod
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


def test_create_refuses_a_preexisting_dest_without_clobbering_it(
    tmp_path: Path, remote: Path, reference: Path, monkeypatch
):
    # A pre-existing dest (deterministic/colliding hash, or a rerun) must be refused
    # BEFORE any clone, and the rollback must NEVER delete that prior directory.
    spec = _spec(tmp_path)
    dest = tmp_path / "trees" / "acme" / "widget" / "issues" / "123-abcd1234"
    dest.mkdir(parents=True)
    (dest / "precious.txt").write_text("do not delete")

    # Clone would explode if reached; the guard must fire first.
    def boom(*args, **kwargs):
        raise AssertionError("clone must not run when dest already exists")

    monkeypatch.setattr(gh, "git_clone_dissociated", boom)

    with pytest.raises(FileExistsError, match="already exists"):
        create(spec, source_repo=str(reference), github_url=str(remote))

    # The pre-existing checkout is untouched — rollback only removes what THIS run made.
    assert (dest / "precious.txt").read_text() == "do not delete"


# --------------------------------------------------------------------------
# Provisioning — .treeinclude copy + shipit/pixi/npm + ADR-0015 build env.
# These mock the git boundary so they exercise steps 3–4 without a real clone.
# --------------------------------------------------------------------------


def _mock_git_boundary(monkeypatch, *, manifests: list[str]):
    """Patch the git boundary so a "clone" just makes the dest + the given manifests."""

    def fake_clone(url: str, dest: str, *, reference: str) -> None:
        d = Path(dest)
        d.mkdir(parents=True)
        for name in manifests:
            (d / name).write_text("# stub\n")

    monkeypatch.setattr(gh, "git_clone_dissociated", fake_clone)
    monkeypatch.setattr(gh, "git_fetch", lambda **k: None)
    monkeypatch.setattr(gh, "git_checkout_new_branch", lambda *a, **k: None)


def test_create_copies_treeinclude_and_provisions_deps(tmp_path: Path, monkeypatch):
    # The source checkout carries the gitignored-but-needed files + the allow-list.
    source = tmp_path / "source"
    source.mkdir()
    (source / ".treeinclude").write_text(".env\nmodels/\n")
    (source / ".env").write_text("TOKEN=1")
    (source / "models").mkdir()
    (source / "models" / "saml.bin").write_text("BIN")

    _mock_git_boundary(
        monkeypatch, manifests=[".shipit.toml", "pixi.toml", "package.json"]
    )

    calls: list[tuple[list[str], Path, dict[str, str]]] = []
    monkeypatch.setattr(
        create_mod,
        "run_provision",
        lambda cmd, *, cwd, env: calls.append((cmd, Path(cwd), env)),
    )
    # Pin the FS check so it never warns here (covered by its own test).
    monkeypatch.setattr(create_mod, "_st_dev", lambda p: 1)

    tree = create(_spec(tmp_path), source_repo=str(source), github_url="url")
    dest = Path(tree.path)

    # Step 3: the .treeinclude files were copied INTO the fresh Tree.
    assert (dest / ".env").read_text() == "TOKEN=1"
    assert (dest / "models" / "saml.bin").read_text() == "BIN"

    # Step 4: shipit install, then pixi install, then npm ci — each in the Tree dir.
    assert [c[0] for c in calls] == [
        ["shipit", "install", "."],
        ["pixi", "install"],
        ["npm", "ci"],
    ]
    assert all(cwd == dest for _, cwd, _ in calls)

    # ADR-0015 build env on every provisioning command: per-Tree target/, the
    # sccache base dir, and incremental off.
    for _, _, env in calls:
        assert env["CARGO_TARGET_DIR"] == str(dest / "target")
        assert env["SCCACHE_BASEDIRS"] == str(dest)
        assert env["CARGO_INCREMENTAL"] == "0"


def test_create_skips_provisioning_steps_whose_manifest_is_absent(
    tmp_path: Path, monkeypatch
):
    # A pixi-only repo runs ONLY `pixi install` — no shipit install, no npm ci.
    source = tmp_path / "source"
    source.mkdir()
    _mock_git_boundary(monkeypatch, manifests=["pixi.toml"])

    calls: list[list[str]] = []
    monkeypatch.setattr(create_mod, "run_provision", lambda cmd, **k: calls.append(cmd))
    monkeypatch.setattr(create_mod, "_st_dev", lambda p: 1)

    create(_spec(tmp_path), source_repo=str(source), github_url="url")
    assert calls == [["pixi", "install"]]


def test_sccache_env_applies_per_tree_target_and_cache_settings(tmp_path: Path):
    env = create_mod.sccache_env(tmp_path / "t")
    assert env == {
        "CARGO_TARGET_DIR": str(tmp_path / "t" / "target"),
        "SCCACHE_BASEDIRS": str(tmp_path / "t"),
        "CARGO_INCREMENTAL": "0",
    }


def test_provision_env_scrubs_leaked_parent_pixi_pointers(tmp_path: Path, monkeypatch):
    # The regression for #167 defect 4: the env shipit builds for in-clone
    # git/install must NOT carry the parent's PIXI_* project pointers — a leaked
    # PIXI_PROJECT_MANIFEST makes the clone's `pixi run lint` resolve the parent
    # manifest (ambiguous across default/lint/review) and the install commit dies.
    monkeypatch.setenv("PIXI_PROJECT_MANIFEST", "/parent/pixi.toml")
    monkeypatch.setenv("PIXI_PROJECT_ROOT", "/parent")
    monkeypatch.setenv("PIXI_ENVIRONMENT_NAME", "default")
    monkeypatch.setenv("PIXI_EXE", "/parent/.pixi/bin/pixi")
    # User-level cache vars are NOT project pointers — they must survive so the
    # child `pixi install` keeps sharing the package cache across Trees.
    monkeypatch.setenv("PIXI_CACHE_DIR", "/home/me/.cache/rattler")
    # An unrelated var the child still needs (e.g. PATH) must pass through.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    env = create_mod.provision_env(tmp_path / "tree")

    # Every leaked project/environment PIXI_* pointer is gone.
    assert "PIXI_PROJECT_MANIFEST" not in env
    assert "PIXI_PROJECT_ROOT" not in env
    assert "PIXI_ENVIRONMENT_NAME" not in env
    assert "PIXI_EXE" not in env
    # Cache var and unrelated vars are preserved.
    assert env["PIXI_CACHE_DIR"] == "/home/me/.cache/rattler"
    assert env["PATH"] == "/usr/bin:/bin"
    # The ADR-0015 build env is still applied on top.
    assert env["CARGO_TARGET_DIR"] == str(tmp_path / "tree" / "target")
    assert env["SCCACHE_BASEDIRS"] == str(tmp_path / "tree")
    assert env["CARGO_INCREMENTAL"] == "0"


def test_run_provision_uses_scrubbed_env_verbatim_not_merged(
    tmp_path: Path, monkeypatch
):
    # run_provision must hand proc.run the env with replace_env=True, so a parent
    # PIXI_PROJECT_MANIFEST in os.environ cannot creep back in via a merge.
    monkeypatch.setenv("PIXI_PROJECT_MANIFEST", "/parent/pixi.toml")

    captured: dict[str, object] = {}

    def fake_run(cmd, *, cwd, env, replace_env=False, **kwargs):
        captured["env"] = env
        captured["replace_env"] = replace_env

    monkeypatch.setattr(create_mod.proc, "run", fake_run)

    create_mod.run_provision(
        ["shipit", "install", "."],
        cwd=tmp_path,
        env=create_mod.provision_env(tmp_path),
    )

    assert captured["replace_env"] is True
    assert "PIXI_PROJECT_MANIFEST" not in captured["env"]


def test_check_same_filesystem_warns_only_across_filesystems(
    tmp_path: Path, monkeypatch
):
    trees_root = tmp_path / "trees"
    cache = tmp_path / "cache"

    devs = {trees_root: 1, cache: 2}
    monkeypatch.setattr(create_mod, "_st_dev", lambda p: devs[Path(p)])
    msg = create_mod.check_same_filesystem(trees_root, cache)
    assert msg is not None and "#119" in msg

    # Same device id → no warning.
    monkeypatch.setattr(create_mod, "_st_dev", lambda p: 7)
    assert create_mod.check_same_filesystem(trees_root, cache) is None


def test_check_same_filesystem_never_fails_on_missing_path(tmp_path: Path, monkeypatch):
    # A real os.stat on a non-existent path raises; the check must swallow it.
    assert (
        create_mod.check_same_filesystem(tmp_path / "nope", tmp_path / "gone") is None
    )


def test_check_same_filesystem_probes_nearest_existing_parent(
    tmp_path: Path, monkeypatch
):
    # First run (#119): the pixi cache dir does not exist yet, but its existing
    # parent is on a different filesystem than the Trees root. The warning must
    # still fire — probing up to the nearest existing ancestor — not be suppressed
    # just because the leaf cache dir has not been created.
    trees_root = tmp_path / "trees"
    trees_root.mkdir()
    cache_parent = tmp_path / "ext"
    cache_parent.mkdir()
    cache = cache_parent / "rattler" / "cache"  # does NOT exist yet

    def fake_st_dev(p):
        p = Path(p)
        if not p.exists():
            raise OSError("missing")
        return 2 if (p == cache_parent or cache_parent in p.parents) else 1

    monkeypatch.setattr(create_mod, "_st_dev", fake_st_dev)
    msg = create_mod.check_same_filesystem(trees_root, cache)
    assert msg is not None and "#119" in msg


def test_create_warns_when_pixi_cache_on_other_filesystem(
    tmp_path: Path, monkeypatch, caplog
):
    source = tmp_path / "source"
    source.mkdir()
    _mock_git_boundary(monkeypatch, manifests=["pixi.toml"])
    monkeypatch.setattr(create_mod, "run_provision", lambda *a, **k: None)

    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(create_mod, "pixi_cache_dir", lambda: cache)
    # Cache on a different device than everything else (the Trees root).
    monkeypatch.setattr(create_mod, "_st_dev", lambda p: 9 if Path(p) == cache else 1)

    with caplog.at_level("WARNING", logger="shipit.tree"):
        create(_spec(tmp_path), source_repo=str(source), github_url="url")

    assert any("#119" in r.message for r in caplog.records)
