from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from shipit import config, execrun, gh, git, pixienv
from shipit.execrun import ExecError
from shipit.identity import repo_from_slug
from shipit.install import units as iunits
from shipit.install.apply import COMMIT_MESSAGE as INSTALL_COMMIT_MESSAGE
from shipit.tree import create as create_mod
from shipit.tree import layout
from shipit.tree.create import create, create_from_source
from shipit.tree.layout import TreeSpec

_PIN = "5eed" * 10
_PINNED_MANIFEST = f'[shipit]\nversion = "{_PIN}"\n\n[managed]\n'

_AGENT = "claude"
_CREATED = "20260717-081333"
_TREE_ID = "619cf51a-f501-44dc-992f-74df773204aa"
_LEAF = f"widget-{_AGENT}-{_CREATED}-{_TREE_ID}"


def _dest(tmp_path: Path) -> Path:
    return tmp_path / "trees" / _LEAF


def _hooks_ok() -> execrun.ExecResult:
    return execrun.ExecResult(
        argv=("lefthook", "install"), rc=0, stdout="", stderr="", duration_ms=1
    )


def _git(args: list[str], cwd: Path) -> str:
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
    repo = tmp_path / "remote"
    repo.mkdir()
    _git(["init"], cwd=repo)
    (repo / "README.md").write_text("hello tree\n")
    (repo / ".shipit.toml").write_text(_PINNED_MANIFEST)
    _git(["add", "."], cwd=repo)
    _git(["commit", "-m", "init"], cwd=repo)
    _git(["branch", "-M", "main"], cwd=repo)
    return repo


def _pixi_result() -> execrun.ExecResult:
    return execrun.ExecResult(
        argv=("pixi", "install"), rc=0, stdout="", stderr="", duration_ms=1
    )


def _stub_provision(monkeypatch):
    monkeypatch.setattr(create_mod, "run_provision", lambda *a, **k: None)
    monkeypatch.setattr(create_mod.pixienv, "install", lambda *a, **k: _pixi_result())
    monkeypatch.setattr(create_mod.pixienv, "run_in_env", lambda *a, **k: _hooks_ok())


@pytest.fixture
def reference(tmp_path: Path, remote: Path) -> Path:
    ref = tmp_path / "ref"
    _git(["clone", str(remote), str(ref)], cwd=tmp_path)
    return ref


def _spec(tmp_path: Path) -> TreeSpec:
    return TreeSpec(
        repo=repo_from_slug("acme/widget"),
        agent=_AGENT,
        created=_CREATED,
        tree_id=_TREE_ID,
        issue=123,
        slug="smoke",
        root=tmp_path / "trees",
    )


def test_create_produces_an_independent_dissociated_clone(
    tmp_path: Path, remote: Path, reference: Path, monkeypatch
):
    _stub_provision(monkeypatch)
    spec = _spec(tmp_path)
    tree = create(spec, source_repo=str(reference), github_url=str(remote))

    dest = Path(tree.path)

    assert dest == _dest(tmp_path)
    assert tree.branch == "issues/123/work"
    assert tree.base == "origin/main"

    assert not (dest / ".git" / "objects" / "info" / "alternates").exists()

    assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=dest) == "issues/123/work"

    assert _git(["remote", "get-url", "origin"], cwd=dest) == str(remote)

    assert (dest / "README.md").read_text() == "hello tree\n"


def test_create_freeform_tree_on_the_default_branch(
    tmp_path: Path, remote: Path, reference: Path, monkeypatch
):
    _stub_provision(monkeypatch)
    spec = TreeSpec(
        repo=repo_from_slug("acme/widget"),
        agent=_AGENT,
        created=_CREATED,
        tree_id=_TREE_ID,
        branch="main",
        root=tmp_path / "trees",
    )
    base_sha = _git(["rev-parse", "main"], cwd=remote)

    tree = create(spec, source_repo=str(reference), github_url=str(remote))

    dest = Path(tree.path)
    assert tree.branch == "main"
    assert tree.base == "origin/main"
    assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=dest) == "main"
    assert _git(["rev-parse", "HEAD"], cwd=dest) == base_sha
    assert (dest / "README.md").read_text() == "hello tree\n"


def test_create_from_source_resolves_origin_url(
    tmp_path: Path, remote: Path, reference: Path, monkeypatch
):
    _stub_provision(monkeypatch)
    spec = _spec(tmp_path)
    tree = create_from_source(spec, source_repo=str(reference))

    dest = Path(tree.path)
    assert _git(["remote", "get-url", "origin"], cwd=dest) == str(remote)
    assert not (dest / ".git" / "objects" / "info" / "alternates").exists()


def test_tree_satisfies_the_critical_isolation_invariants(
    tmp_path: Path, remote: Path, reference: Path, monkeypatch
):
    _stub_provision(monkeypatch)
    trees_root = tmp_path / "trees"
    spec = _spec(tmp_path)
    tree = create(spec, source_repo=str(reference), github_url=str(remote))
    dest = Path(tree.path)

    assert dest.is_relative_to(trees_root)
    assert ".claude" not in dest.parts

    git_path = dest / ".git"
    assert git_path.is_dir()
    assert not git_path.is_file()

    assert not (dest / ".git" / "objects" / "info" / "alternates").exists()


def test_create_hardens_the_tree_as_a_reference_donor(
    tmp_path: Path, remote: Path, reference: Path, monkeypatch
):
    _stub_provision(monkeypatch)
    tree = create(_spec(tmp_path), source_repo=str(reference), github_url=str(remote))
    dest = Path(tree.path)

    for key, value in git.SAFE_DONOR_CONFIG:
        assert _git(["config", "--local", "--get", key], cwd=dest) == value


def _poison_split_chain(reference: Path) -> Path:
    _git(["commit-graph", "write", "--reachable", "--split"], cwd=reference)
    return reference / ".git" / "objects" / "info" / "commit-graphs"


def _poison_plain_commit_graph(reference: Path) -> Path:
    _git(["commit-graph", "write", "--reachable"], cwd=reference)
    return reference / ".git" / "objects" / "info" / "commit-graph"


def _poison_multi_pack_index(reference: Path) -> Path:
    _git(["repack", "-ad"], cwd=reference)
    _git(["multi-pack-index", "write"], cwd=reference)
    return reference / ".git" / "objects" / "pack" / "multi-pack-index"


@pytest.mark.parametrize(
    "poison",
    [
        pytest.param(_poison_split_chain, id="split-chain"),
        pytest.param(_poison_plain_commit_graph, id="plain-commit-graph"),
        pytest.param(_poison_multi_pack_index, id="multi-pack-index"),
    ],
)
def test_clone_dissociated_survives_a_commit_graph_bearing_reference(
    tmp_path: Path, remote: Path, reference: Path, caplog, poison
):
    for i in range(3):
        (reference / f"file{i}.txt").write_text(f"{i}\n")
        _git(["add", "."], cwd=reference)
        _git(["commit", "-m", f"c{i}"], cwd=reference)
    artifact = poison(reference)
    assert artifact.exists(), "fixture must model the poisoned donor"

    dest = tmp_path / "clone-under-test"
    with caplog.at_level(logging.WARNING, logger="shipit.git"):
        git.clone_dissociated(remote.as_uri(), str(dest), reference=str(reference))

    assert (dest / "README.md").read_text() == "hello tree\n"
    assert not (dest / ".git" / "objects" / "info" / "alternates").exists()
    assert not [
        r
        for r in caplog.records
        if r.name == "shipit.git" and r.levelno >= logging.WARNING
    ]
    assert (
        subprocess.run(
            ["git", "config", "--local", "--get", "core.commitGraph"],
            cwd=dest,
            capture_output=True,
            text=True,
        ).returncode
        != 0
    )


def test_clone_dissociated_dereferences_a_linked_worktree_reference(
    tmp_path: Path, remote: Path, reference: Path, caplog
):
    linked = tmp_path / "linked-wt"
    _git(["worktree", "add", "-b", "wt-branch", str(linked)], cwd=reference)
    assert (linked / ".git").is_file()

    dest = tmp_path / "clone-under-test"
    with caplog.at_level(logging.INFO, logger="shipit.git"):
        git.clone_dissociated(remote.as_uri(), str(dest), reference=str(linked))

    assert (dest / "README.md").read_text() == "hello tree\n"
    assert not (dest / ".git" / "objects" / "info" / "alternates").exists()
    assert any(
        r.name == "shipit.git"
        and r.levelno == logging.INFO
        and str(linked) in r.getMessage()
        for r in caplog.records
    )


def test_resolve_reference_donor_derefs_worktree_but_passes_normal_through(
    tmp_path: Path, reference: Path
):
    assert git._resolve_reference_donor(str(reference)) == str(reference)

    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert git._resolve_reference_donor(str(plain)) == str(plain)

    linked = tmp_path / "linked-wt2"
    _git(["worktree", "add", "-b", "wt2", str(linked)], cwd=reference)
    resolved = git._resolve_reference_donor(str(linked))
    assert Path(resolved) == (reference / ".git").resolve()


def test_central_root_is_absolute_and_outside_any_claude_dir(monkeypatch):
    monkeypatch.delenv(layout.CENTRAL_ROOT_ENV, raising=False)
    default_root = create_mod.central_root()
    assert default_root.is_absolute()
    assert ".claude" not in default_root.parts

    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, "/srv/agents/trees")
    override_root = create_mod.central_root()
    assert override_root.is_absolute()
    assert ".claude" not in override_root.parts


def test_create_mutates_nothing_managed_zero_commits_on_a_pinned_base(
    tmp_path: Path, remote: Path, reference: Path, monkeypatch
):
    def no_push(*a, **k):
        raise AssertionError("tree create provisioning must NOT push to origin")

    def no_pr(*a, **k):
        raise AssertionError("tree create provisioning must NOT open a PR")

    def no_switch(*a, **k):
        raise AssertionError("tree create provisioning must NOT switch branches")

    monkeypatch.setattr(git, "push", no_push)
    monkeypatch.setattr(gh, "pr_create", no_pr)
    monkeypatch.setattr(git, "switch_create", no_switch)

    def no_managed_mutation(cmd, **_k):
        raise AssertionError(f"provisioning ran an unexpected step: {cmd}")

    monkeypatch.setattr(create_mod, "run_provision", no_managed_mutation)
    monkeypatch.setattr(create_mod.pixienv, "install", lambda *a, **k: _pixi_result())
    monkeypatch.setattr(create_mod.pixienv, "run_in_env", lambda *a, **k: _hooks_ok())

    base_sha = _git(["rev-parse", "main"], cwd=remote)
    tree = create(_spec(tmp_path), source_repo=str(reference), github_url=str(remote))
    dest = Path(tree.path)

    assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=dest) == "issues/123/work"
    assert _git(["rev-parse", "HEAD"], cwd=dest) == base_sha
    assert INSTALL_COMMIT_MESSAGE not in _git(["log", "--format=%s"], cwd=dest)
    assert _git(["status", "--porcelain"], cwd=dest) == ""

    assert (dest / ".git").is_dir()
    assert not (dest / ".git" / "objects" / "info" / "alternates").exists()
    assert ".claude" not in dest.parts


def test_create_writes_no_provision_record(
    tmp_path: Path, remote: Path, reference: Path, monkeypatch
):
    _stub_provision(monkeypatch)
    tree = create(_spec(tmp_path), source_repo=str(reference), github_url=str(remote))
    dest = Path(tree.path)
    assert not (dest / ".git" / "shipit-provision.json").exists()


def test_create_rolls_back_partial_tree_on_failure(
    tmp_path: Path, remote: Path, reference: Path, monkeypatch
):
    spec = _spec(tmp_path)

    def boom(*args, **kwargs):
        raise ExecError(["gh"], rc=1, stderr="checkout blew up")

    monkeypatch.setattr(git, "checkout_create_or_reset", boom)

    with pytest.raises(ExecError):
        create(spec, source_repo=str(reference), github_url=str(remote))

    dest = tmp_path / "trees" / _LEAF
    assert not dest.exists()


def test_create_fails_closed_and_rolls_back_on_a_pinless_source(
    tmp_path: Path, remote: Path, reference: Path, monkeypatch
):
    (remote / ".shipit.toml").unlink()
    _git(["commit", "-am", "de-pin"], cwd=remote)

    def _must_not_provision(*_a, **_k):
        raise AssertionError("fail-closed breached: provisioning ran on a pinless base")

    monkeypatch.setattr(create_mod, "run_provision", _must_not_provision)
    monkeypatch.setattr(create_mod.pixienv, "install", _must_not_provision)

    spec = _spec(tmp_path)
    with pytest.raises(ValueError, match="no \\[shipit\\].version pin"):
        create(spec, source_repo=str(reference), github_url=str(remote))

    dest = tmp_path / "trees" / _LEAF
    assert not dest.exists()


def test_create_refuses_a_preexisting_dest_without_clobbering_it(
    tmp_path: Path, remote: Path, reference: Path, monkeypatch
):
    spec = _spec(tmp_path)
    dest = tmp_path / "trees" / _LEAF
    dest.mkdir(parents=True)
    (dest / "precious.txt").write_text("do not delete")

    def boom(*args, **kwargs):
        raise AssertionError("clone must not run when dest already exists")

    monkeypatch.setattr(git, "clone_dissociated", boom)

    with pytest.raises(FileExistsError, match="already exists"):
        create(spec, source_repo=str(reference), github_url=str(remote))

    assert (dest / "precious.txt").read_text() == "do not delete"


def _mock_git_boundary(monkeypatch, *, manifests: list[str]):

    def fake_clone(url: str, dest: str, *, reference: str) -> None:
        d = Path(dest)
        d.mkdir(parents=True)
        for name in manifests:
            if name == ".shipit.toml":
                content = f'[shipit]\nversion = "{_PIN}"\n'
            elif name.endswith(".json"):
                content = "{}\n"
            else:
                content = "# stub\n"
            (d / name).write_text(content)

    monkeypatch.setattr(git, "clone_dissociated", fake_clone)
    monkeypatch.setattr(git, "configure_safe_reference_donor", lambda **k: None)
    monkeypatch.setattr(git, "fetch", lambda **k: None)
    monkeypatch.setattr(git, "checkout_create_or_reset", lambda *a, **k: None)
    monkeypatch.setattr(git, "submodule_update_init", lambda **k: None)


def test_create_copies_treeinclude_and_provisions_deps(tmp_path: Path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / ".treeinclude").write_text(".env\nmodels/\n")
    (source / ".env").write_text("TOKEN=1")
    (source / "models").mkdir()
    (source / "models" / "saml.bin").write_text("BIN")

    _mock_git_boundary(
        monkeypatch,
        manifests=[".shipit.toml", "pixi.toml", "package.json", "package-lock.json"],
    )

    calls: list[tuple[list[str], Path, dict[str, str]]] = []
    monkeypatch.setattr(
        create_mod,
        "run_provision",
        lambda cmd, *, cwd, env: calls.append((cmd, Path(cwd), env)),
    )

    def fake_pixi_install(root, *, env=None, **_k):
        calls.append((["pixi", "install"], Path(root), env))
        return _pixi_result()

    monkeypatch.setattr(create_mod.pixienv, "install", fake_pixi_install)
    monkeypatch.setattr(create_mod, "_st_dev", lambda p: 1)
    monkeypatch.setenv("CARGO_TARGET_DIR", "/parent/tree/target")
    monkeypatch.setenv("SCCACHE_BASEDIRS", "/parent/tree")
    monkeypatch.setenv("CARGO_INCREMENTAL", "0")

    tree = create(_spec(tmp_path), source_repo=str(source), github_url="url")
    dest = Path(tree.path)

    assert (dest / ".env").read_text() == "TOKEN=1"
    assert (dest / "models" / "saml.bin").read_text() == "BIN"

    assert [c[0] for c in calls] == [
        ["pixi", "install"],
        ["npm", "ci"],
    ]
    assert all(cwd == dest for _, cwd, _ in calls)

    for _, _, env in calls:
        assert "CARGO_TARGET_DIR" not in env
        assert "SCCACHE_BASEDIRS" not in env
        assert "CARGO_INCREMENTAL" not in env


def test_create_initializes_submodules_after_checkout_before_provision(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    order: list[str] = []

    def fake_clone(url: str, dest: str, *, reference: str) -> None:
        d = Path(dest)
        d.mkdir(parents=True)
        (d / ".shipit.toml").write_text(f'[shipit]\nversion = "{_PIN}"\n')
        (d / "pixi.toml").write_text("# stub\n")

    monkeypatch.setattr(git, "clone_dissociated", fake_clone)
    monkeypatch.setattr(git, "configure_safe_reference_donor", lambda **k: None)
    monkeypatch.setattr(git, "fetch", lambda **k: None)
    monkeypatch.setattr(
        git, "checkout_create_or_reset", lambda *a, **k: order.append("checkout")
    )
    monkeypatch.setattr(
        git, "submodule_update_init", lambda **k: order.append("submodule")
    )

    def fake_pixi_install(root, **_k):
        order.append("provision")
        return _pixi_result()

    monkeypatch.setattr(create_mod.pixienv, "install", fake_pixi_install)
    monkeypatch.setattr(create_mod, "run_provision", lambda *a, **k: None)
    monkeypatch.setattr(create_mod, "_st_dev", lambda p: 1)

    create(_spec(tmp_path), source_repo=str(source), github_url="url")

    assert order == ["checkout", "submodule", "provision"]


def test_create_rolls_back_when_submodule_init_fails(
    tmp_path: Path, remote: Path, reference: Path, monkeypatch
):
    _stub_provision(monkeypatch)

    def boom(**_k):
        raise ExecError(["git", "submodule"], rc=1, stderr="auth failed")

    monkeypatch.setattr(git, "submodule_update_init", boom)

    with pytest.raises(ExecError):
        create(_spec(tmp_path), source_repo=str(reference), github_url=str(remote))

    dest = tmp_path / "trees" / _LEAF
    assert not dest.exists()


def test_create_skips_provisioning_steps_whose_manifest_is_absent(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    _mock_git_boundary(monkeypatch, manifests=[".shipit.toml", "pixi.toml"])

    calls: list[list[str]] = []
    monkeypatch.setattr(create_mod, "run_provision", lambda cmd, **k: calls.append(cmd))

    def fake_pixi_install(root, **_k):
        calls.append(["pixi", "install"])
        return _pixi_result()

    monkeypatch.setattr(create_mod.pixienv, "install", fake_pixi_install)
    monkeypatch.setattr(create_mod, "_st_dev", lambda p: 1)

    create(_spec(tmp_path), source_repo=str(source), github_url="url")
    assert calls == [["pixi", "install"]]


@pytest.mark.parametrize(
    ("pin", "argv"),
    [
        ("npm@11.4.2", ["npm", "ci"]),
        ("pnpm@10.29.3", ["pnpm", "install", "--frozen-lockfile"]),
        ("yarn@4.9.1", ["yarn", "install", "--immutable"]),
        ("yarn@1.22.22", ["yarn", "install", "--frozen-lockfile"]),
    ],
)
def test_node_install_argv_honours_the_packagemanager_pin(
    tmp_path: Path, pin: str, argv: list[str]
):
    (tmp_path / "package.json").write_text(f'{{"packageManager": "{pin}"}}\n')
    assert create_mod.node_install_argv(tmp_path) == argv


def test_node_install_argv_rejects_a_yarn_pin_with_no_numeric_major(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"packageManager": "yarn@stable"}\n')
    with pytest.raises(ValueError, match="unparseable yarn version"):
        create_mod.node_install_argv(tmp_path)


def test_node_install_argv_pin_wins_over_a_conflicting_lockfile(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"packageManager": "pnpm@10.29.3"}\n')
    (tmp_path / "package-lock.json").write_text("{}\n")
    assert create_mod.node_install_argv(tmp_path) == [
        "pnpm",
        "install",
        "--frozen-lockfile",
    ]


@pytest.mark.parametrize(
    ("lockfile", "argv"),
    [
        ("package-lock.json", ["npm", "ci"]),
        ("pnpm-lock.yaml", ["pnpm", "install", "--frozen-lockfile"]),
        ("yarn.lock", ["yarn", "install", "--immutable"]),
    ],
)
def test_node_install_argv_falls_back_to_the_lockfile(
    tmp_path: Path, lockfile: str, argv: list[str]
):
    (tmp_path / "package.json").write_text("{}\n")
    (tmp_path / lockfile).write_text("stub\n")
    assert create_mod.node_install_argv(tmp_path) == argv


def test_node_install_argv_reads_the_yarn_v1_banner_without_a_pin(tmp_path: Path):
    (tmp_path / "package.json").write_text("{}\n")
    (tmp_path / "yarn.lock").write_text(
        "# THIS IS AN AUTOGENERATED FILE. DO NOT EDIT THIS FILE DIRECTLY.\n"
        "# yarn lockfile v1\n\n"
    )
    assert create_mod.node_install_argv(tmp_path) == [
        "yarn",
        "install",
        "--frozen-lockfile",
    ]


def test_node_install_argv_fails_loud_with_no_signal(tmp_path: Path):
    (tmp_path / "package.json").write_text("{}\n")
    with pytest.raises(ValueError, match="no recognized lockfile"):
        create_mod.node_install_argv(tmp_path)


def test_node_install_argv_fails_loud_on_ambiguous_lockfiles(tmp_path: Path):
    (tmp_path / "package.json").write_text("{}\n")
    (tmp_path / "package-lock.json").write_text("{}\n")
    (tmp_path / "yarn.lock").write_text("stub\n")
    with pytest.raises(ValueError, match="multiple lockfiles"):
        create_mod.node_install_argv(tmp_path)


@pytest.mark.parametrize("pin", ["npm", "pnpm", "yarn", "npm@"])
def test_node_install_argv_fails_loud_on_a_versionless_pin(tmp_path: Path, pin: str):
    (tmp_path / "package.json").write_text(f'{{"packageManager": "{pin}"}}\n')
    (tmp_path / "package-lock.json").write_text("{}\n")
    with pytest.raises(ValueError, match="malformed packageManager"):
        create_mod.node_install_argv(tmp_path)


def test_node_install_argv_fails_loud_on_an_unknown_manager(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"packageManager": "bun@1.2.3"}\n')
    with pytest.raises(ValueError, match="unsupported packageManager"):
        create_mod.node_install_argv(tmp_path)


def test_node_install_argv_fails_loud_on_unparseable_manifest(tmp_path: Path):
    (tmp_path / "package.json").write_text("# not json\n")
    with pytest.raises(ValueError, match="unparseable package.json"):
        create_mod.node_install_argv(tmp_path)


def test_node_install_argv_fails_loud_on_a_non_object_manifest(tmp_path: Path):
    (tmp_path / "package.json").write_text("[1, 2, 3]\n")
    (tmp_path / "package-lock.json").write_text("{}\n")
    with pytest.raises(ValueError, match="not an object"):
        create_mod.node_install_argv(tmp_path)


def test_provision_runs_the_pnpm_frozen_install_on_a_pnpm_repo(
    tmp_path: Path, monkeypatch
):
    dest = tmp_path / "tree"
    dest.mkdir()
    (dest / config.CONFIG_NAME).write_text(_PINNED_MANIFEST)
    (dest / "package.json").write_text('{"packageManager": "pnpm@10.29.3"}\n')
    (dest / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")

    calls: list[list[str]] = []
    monkeypatch.setattr(create_mod, "run_provision", lambda cmd, **k: calls.append(cmd))
    monkeypatch.setattr(create_mod, "_st_dev", lambda p: 1)

    create_mod._provision(dest, trees_root=tmp_path / "trees")
    assert calls == [["pnpm", "install", "--frozen-lockfile"]]


def test_create_rolls_back_when_the_node_manager_is_undecidable(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    _mock_git_boundary(monkeypatch, manifests=[".shipit.toml", "package.json"])
    monkeypatch.setattr(create_mod, "_st_dev", lambda p: 1)

    with pytest.raises(ValueError, match="no recognized lockfile"):
        create(_spec(tmp_path), source_repo=str(source), github_url="url")
    leaf = tmp_path / "trees" / _LEAF
    assert not leaf.exists()


def test_provision_env_scrubs_leaked_parent_build_env(monkeypatch):
    monkeypatch.setenv("CARGO_TARGET_DIR", "/parent/tree/target")
    monkeypatch.setenv("SCCACHE_BASEDIRS", "/parent/tree")
    monkeypatch.setenv("CARGO_INCREMENTAL", "0")
    monkeypatch.setenv("SCCACHE_GCS_KEY", "creds")
    env = create_mod.provision_env()
    assert "CARGO_TARGET_DIR" not in env
    assert "SCCACHE_BASEDIRS" not in env
    assert "CARGO_INCREMENTAL" not in env
    assert env["SCCACHE_GCS_KEY"] == "creds"


def test_provision_fails_closed_when_base_is_pinless(tmp_path: Path, monkeypatch):
    dest = tmp_path / "tree"
    dest.mkdir()
    (dest / config.CONFIG_NAME).write_text('[secrets]\nGH_PAT = { env = "X" }\n')
    (dest / pixienv.MANIFEST_NAME).write_text("# stub\n")

    calls: list[list[str]] = []
    monkeypatch.setattr(create_mod, "run_provision", lambda cmd, **k: calls.append(cmd))
    monkeypatch.setattr(
        create_mod.pixienv,
        "install",
        lambda root, **k: calls.append(["pixi", "install"]),
    )
    monkeypatch.setattr(create_mod, "_st_dev", lambda p: 1)

    with pytest.raises(
        ValueError, match="no \\[shipit\\].version pin — run the bootstrap"
    ):
        create_mod._provision(dest, trees_root=tmp_path / "trees")
    assert calls == []


def test_provision_runs_no_step_at_all_on_a_pinned_manifestless_repo(
    tmp_path: Path, monkeypatch
):
    dest = tmp_path / "tree"
    dest.mkdir()
    (dest / config.CONFIG_NAME).write_text(_PINNED_MANIFEST)

    calls: list[list[str]] = []
    monkeypatch.setattr(create_mod, "run_provision", lambda cmd, **k: calls.append(cmd))
    monkeypatch.setattr(create_mod, "_st_dev", lambda p: 1)

    create_mod._provision(dest, trees_root=tmp_path / "trees")
    assert calls == []


def _provision_stubs(monkeypatch) -> list[tuple[list[str], Path, object]]:
    calls: list[tuple[list[str], Path, object]] = []
    monkeypatch.setattr(
        create_mod,
        "run_provision",
        lambda cmd, *, cwd, env: calls.append((cmd, Path(cwd), env)),
    )

    def fake_pixi_install(root, *, env=None, **_k):
        calls.append((["pixi", "install"], Path(root), env))
        return _pixi_result()

    def fake_run_in_env(argv, root, *, environment=None, env=None, **_k):
        calls.append((["pixi", "run", "-e", str(environment), *argv], Path(root), env))
        return execrun.ExecResult(
            argv=tuple(pixienv.run_argv(argv, root, environment=environment)),
            rc=0,
            stdout="",
            stderr="",
            duration_ms=1,
        )

    monkeypatch.setattr(create_mod.pixienv, "install", fake_pixi_install)
    monkeypatch.setattr(create_mod.pixienv, "run_in_env", fake_run_in_env)
    monkeypatch.setattr(create_mod, "_st_dev", lambda p: 1)
    return calls


def test_provision_activates_hooks_when_the_clone_carries_lefthook(
    tmp_path: Path, monkeypatch
):
    dest = tmp_path / "tree"
    dest.mkdir()
    (dest / config.CONFIG_NAME).write_text(_PINNED_MANIFEST)
    (dest / pixienv.MANIFEST_NAME).write_text("# stub\n")
    (dest / iunits.LEFTHOOK_FILE).write_text("# stub\n")
    (dest / "package.json").write_text("{}\n")
    (dest / "package-lock.json").write_text("{}\n")

    calls = _provision_stubs(monkeypatch)
    create_mod._provision(dest, trees_root=tmp_path / "trees")

    assert [c[0] for c in calls] == [
        ["pixi", "install"],
        ["pixi", "run", "-e", "lint", "lefthook", "install"],
        ["npm", "ci"],
    ]
    assert all(cwd == dest for _, cwd, _ in calls)
    envs = [env for _, _, env in calls]
    assert all(env is envs[0] and env is not None for env in envs)


def test_provision_skips_hook_activation_without_a_lefthook_config(
    tmp_path: Path, monkeypatch
):
    dest = tmp_path / "tree"
    dest.mkdir()
    (dest / config.CONFIG_NAME).write_text(_PINNED_MANIFEST)
    (dest / pixienv.MANIFEST_NAME).write_text("# stub\n")

    calls = _provision_stubs(monkeypatch)
    create_mod._provision(dest, trees_root=tmp_path / "trees")
    assert [c[0] for c in calls] == [["pixi", "install"]]


def test_provision_skips_hook_activation_without_a_pixi_manifest(
    tmp_path: Path, monkeypatch
):
    dest = tmp_path / "tree"
    dest.mkdir()
    (dest / config.CONFIG_NAME).write_text(_PINNED_MANIFEST)
    (dest / iunits.LEFTHOOK_FILE).write_text("# stub\n")

    calls = _provision_stubs(monkeypatch)
    create_mod._provision(dest, trees_root=tmp_path / "trees")
    assert [c[0] for c in calls] == []


def test_provision_hook_activation_failure_fails_the_create(
    tmp_path: Path, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    _mock_git_boundary(
        monkeypatch, manifests=[".shipit.toml", "pixi.toml", iunits.LEFTHOOK_FILE]
    )
    monkeypatch.setattr(create_mod, "run_provision", lambda *a, **k: None)
    monkeypatch.setattr(create_mod.pixienv, "install", lambda *a, **k: _pixi_result())
    monkeypatch.setattr(create_mod, "_st_dev", lambda p: 1)

    def failing_activation(argv, root, **_k):
        raise ExecError(tuple(argv), rc=1, stderr="lefthook: boom")

    monkeypatch.setattr(create_mod.pixienv, "run_in_env", failing_activation)

    with pytest.raises(ExecError):
        create(_spec(tmp_path), source_repo=str(source), github_url="url")
    leaf = tmp_path / "trees" / _LEAF
    assert not leaf.exists()


def test_provision_env_scrubs_leaked_parent_pixi_pointers(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PIXI_PROJECT_MANIFEST", "/parent/pixi.toml")
    monkeypatch.setenv("PIXI_PROJECT_ROOT", "/parent")
    monkeypatch.setenv("PIXI_ENVIRONMENT_NAME", "default")
    monkeypatch.setenv("PIXI_EXE", "/parent/.pixi/bin/pixi")
    monkeypatch.setenv("PIXI_CACHE_DIR", "/home/me/.cache/rattler")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    env = create_mod.provision_env()

    assert "PIXI_PROJECT_MANIFEST" not in env
    assert "PIXI_PROJECT_ROOT" not in env
    assert "PIXI_ENVIRONMENT_NAME" not in env
    assert "PIXI_EXE" not in env
    assert env["PIXI_CACHE_DIR"] == "/home/me/.cache/rattler"
    assert env["PATH"] == "/usr/bin:/bin"


def test_run_provision_uses_scrubbed_env_verbatim_not_merged(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("PIXI_PROJECT_MANIFEST", "/parent/pixi.toml")

    captured: dict[str, object] = {}

    def fake_run(cmd, *, cwd, env, replace_env=False, **kwargs):
        captured["env"] = env
        captured["replace_env"] = replace_env
        captured["timeout"] = kwargs.get("timeout")
        return execrun.ExecResult(
            argv=tuple(cmd), rc=0, stdout="", stderr="", duration_ms=1
        )

    monkeypatch.setattr(create_mod.execrun, "run", fake_run)

    create_mod.run_provision(
        ["shipit", "install", "."],
        cwd=tmp_path,
        env=create_mod.provision_env(),
    )

    assert captured["replace_env"] is True
    assert "PIXI_PROJECT_MANIFEST" not in captured["env"]
    assert captured["timeout"] == create_mod.PROVISION_TIMEOUT


def test_run_provision_narrates_step_timing_at_info(
    tmp_path: Path, monkeypatch, caplog
):
    monkeypatch.setattr(
        create_mod.execrun,
        "run",
        lambda cmd, **kw: execrun.ExecResult(
            argv=tuple(cmd), rc=0, stdout="", stderr="", duration_ms=1234
        ),
    )
    with caplog.at_level(logging.INFO, logger="shipit.tree"):
        create_mod.run_provision(["npm", "ci"], cwd=tmp_path, env={"PATH": "/usr/bin"})
    messages = [r.getMessage() for r in caplog.records]
    assert any("npm ci" in m and "1234ms" in m for m in messages)


def test_pixi_install_step_narrates_timing_at_info(tmp_path: Path, monkeypatch, caplog):
    monkeypatch.setattr(
        create_mod.execrun,
        "run",
        lambda cmd, **kw: execrun.ExecResult(
            argv=tuple(cmd), rc=0, stdout="", stderr="", duration_ms=987
        ),
    )
    with caplog.at_level(logging.INFO, logger="shipit.tree"):
        create_mod._narrate_step(pixienv.install(tmp_path, env={"PATH": "/usr/bin"}))
    messages = [r.getMessage() for r in caplog.records]
    assert any("pixi install" in m and "987ms" in m for m in messages)


def test_run_provision_failure_leaves_durable_record_with_both_streams(
    tmp_path: Path, caplog
):
    cmd = [
        sys.executable,
        "-c",
        "import sys; print('out-diag'); print('err-diag', file=sys.stderr); sys.exit(3)",
    ]
    with caplog.at_level(logging.DEBUG, logger="shipit.exec"):
        with pytest.raises(execrun.ExecError) as exc_info:
            create_mod.run_provision(cmd, cwd=tmp_path, env=create_mod.provision_env())
    error = exc_info.value
    assert error.rc == 3
    assert "out-diag" in error.stdout
    assert "err-diag" in error.stderr
    failures = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(failures) == 1
    message = failures[0].getMessage()
    assert "out-diag" in message
    assert "err-diag" in message


def test_check_same_filesystem_warns_only_across_filesystems(
    tmp_path: Path, monkeypatch
):
    trees_root = tmp_path / "trees"
    cache = tmp_path / "cache"

    devs = {trees_root: 1, cache: 2}
    monkeypatch.setattr(create_mod, "_st_dev", lambda p: devs[Path(p)])
    msg = create_mod.check_same_filesystem(trees_root, cache)
    assert msg is not None and "#119" in msg

    monkeypatch.setattr(create_mod, "_st_dev", lambda p: 7)
    assert create_mod.check_same_filesystem(trees_root, cache) is None


def test_check_same_filesystem_never_fails_on_missing_path(tmp_path: Path, monkeypatch):
    assert (
        create_mod.check_same_filesystem(tmp_path / "nope", tmp_path / "gone") is None
    )


def test_check_same_filesystem_probes_nearest_existing_parent(
    tmp_path: Path, monkeypatch
):
    trees_root = tmp_path / "trees"
    trees_root.mkdir()
    cache_parent = tmp_path / "ext"
    cache_parent.mkdir()
    cache = cache_parent / "rattler" / "cache"

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
    _mock_git_boundary(monkeypatch, manifests=[".shipit.toml", "pixi.toml"])
    monkeypatch.setattr(create_mod, "run_provision", lambda *a, **k: None)
    monkeypatch.setattr(create_mod.pixienv, "install", lambda *a, **k: _pixi_result())

    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(create_mod.pixienv, "cache_dir", lambda: cache)
    monkeypatch.setattr(create_mod, "_st_dev", lambda p: 9 if Path(p) == cache else 1)

    with caplog.at_level("WARNING", logger="shipit.tree"):
        create(_spec(tmp_path), source_repo=str(source), github_url="url")

    assert any("#119" in r.message for r in caplog.records)


def _home(monkeypatch, tmp_path: Path) -> Path:
    from shipit import sessionstore

    home = tmp_path / "fake-home"
    monkeypatch.setattr(sessionstore, "_default_home", lambda: home)
    return home


def test_create_plants_the_session_store_link(
    tmp_path: Path, remote: Path, reference: Path, monkeypatch
):
    from shipit import sessionstore

    _stub_provision(monkeypatch)
    home = _home(monkeypatch, tmp_path)

    tree = create(_spec(tmp_path), source_repo=str(reference), github_url=str(remote))

    link = sessionstore.link_path(Path(tree.path), home=home)
    assert link.is_symlink()
    assert Path(os.readlink(link)).parts[-3:-2] == ("stores",)
    assert Path(os.readlink(link)).is_dir()


def test_create_links_every_tree_of_a_repo_to_one_store(
    tmp_path: Path, remote: Path, reference: Path, monkeypatch
):
    from shipit import sessionstore

    _stub_provision(monkeypatch)
    home = _home(monkeypatch, tmp_path)

    first = create(_spec(tmp_path), source_repo=str(reference), github_url=str(remote))
    second_spec = TreeSpec(
        repo=repo_from_slug("acme/widget"),
        agent=_AGENT,
        created=_CREATED,
        tree_id="beef5678-beef-4bee-8bee-beef5678beef",
        issue=456,
        slug="other",
        root=tmp_path / "trees",
    )
    second = create(second_spec, source_repo=str(reference), github_url=str(remote))

    assert os.readlink(
        sessionstore.link_path(Path(first.path), home=home)
    ) == os.readlink(sessionstore.link_path(Path(second.path), home=home))


def test_create_survives_an_unplantable_session_store(
    tmp_path: Path, remote: Path, reference: Path, monkeypatch, caplog
):
    from shipit import sessionstore

    _stub_provision(monkeypatch)

    def boom(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(sessionstore, "plant", boom)

    with caplog.at_level(logging.DEBUG):
        tree = create(
            _spec(tmp_path), source_repo=str(reference), github_url=str(remote)
        )

    assert Path(tree.path).is_dir(), "the Tree was lost to a session-store failure"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("session store not planted" in r.message for r in caplog.records)
