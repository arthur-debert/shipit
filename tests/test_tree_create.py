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

from shipit import config, gh
from shipit.tree import create as create_mod
from shipit.tree import layout
from shipit.tree.create import create, create_from_source
from shipit.tree.layout import TreeSpec
from shipit.verbs import install as install_mod


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

    # READY summary is the planned {path, branch, base}. The slug ("smoke") rides the
    # DIR leaf after the default `work` session; the branch stays issues/<id>/<session>.
    assert (
        dest
        == tmp_path
        / "trees"
        / "acme"
        / "widget"
        / "issues"
        / "123"
        / "work-smoke-abcd1234"
    )
    assert tree.branch == "issues/123/work"
    assert tree.base == "origin/main"

    # Independent: --dissociate removed the alternates link entirely.
    assert not (dest / ".git" / "objects" / "info" / "alternates").exists()

    # On the planned branch.
    assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=dest) == "issues/123/work"

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


def test_tree_satisfies_the_critical_isolation_invariants(
    tmp_path: Path, remote: Path, reference: Path
):
    """The three non-negotiable invariants of the Tree the WorktreeCreate hook returns
    (ADR-0014), pinned as real assertions on a real git clone so a regression in the
    clone strategy fails loud:

    (a) it lives under the central trees root and inside NO ``.claude`` directory —
        a Tree must never land in the harness's own ``.claude/worktrees`` (the #139
        trap the demoted adapter exists to close);
    (b) it is a real dissociated CLONE — ``.git`` is a DIRECTORY, not the ``.git``
        *file* pointer a ``git worktree`` checkout leaves behind;
    (c) it borrows NO objects — ``--dissociate`` copied them, so there is no
        ``objects/info/alternates`` link back to the reference.
    """
    trees_root = tmp_path / "trees"
    spec = _spec(tmp_path)  # spec.root == tmp_path / "trees"
    tree = create(spec, source_repo=str(reference), github_url=str(remote))
    dest = Path(tree.path)

    # (a) Under the central trees root, within NO `.claude` directory.
    assert dest.is_relative_to(trees_root)
    assert ".claude" not in dest.parts

    # (b) A real dissociated clone: `.git` is a directory, not a worktree pointer file.
    git_path = dest / ".git"
    assert git_path.is_dir()
    assert not git_path.is_file()

    # (c) No borrowed objects.
    assert not (dest / ".git" / "objects" / "info" / "alternates").exists()


def test_central_root_is_absolute_and_outside_any_claude_dir(monkeypatch):
    """The central root every Tree hangs off is absolute and `.claude`-free, so a Tree
    provisioned WITHOUT an explicit root (the WorktreeCreate hook path, which calls
    `create_from_source`) cannot land inside the harness's `.claude/worktrees`
    (ADR-0014 isolation). Covers both the default and an env-override central root."""
    monkeypatch.delenv(layout.CENTRAL_ROOT_ENV, raising=False)
    default_root = create_mod.central_root()
    assert default_root.is_absolute()
    assert ".claude" not in default_root.parts

    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, "/srv/agents/trees")
    override_root = create_mod.central_root()
    assert override_root.is_absolute()
    assert ".claude" not in override_root.parts


def test_create_provisions_local_only_on_planned_branch_no_origin_side_effects(
    tmp_path: Path, remote: Path, reference: Path, monkeypatch
):
    # #170 end-to-end against REAL git: a Tree whose source carries `.shipit.toml`
    # gets the managed set committed on its PLANNED branch by a LOCAL-ONLY install
    # — and `tree create` makes NO push and opens NO PR. The provisioning install
    # step is run in-process via `install.run(local=True)`; the pixi/npm steps are
    # no-ops here (covered elsewhere).

    # The Tree's clone carries `.shipit.toml`, so `_provision` runs the install step.
    (remote / ".shipit.toml").write_text('[shipit]\nversion = "seed"\n')
    _git(["add", "."], cwd=remote)
    _git(["commit", "-m", "add manifest"], cwd=remote)

    # A deterministic commit identity for the real local commit `install --local` makes.
    for var, val in {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }.items():
        monkeypatch.setenv(var, val)

    # ANY push / PR / branch-switch during provisioning is the bug — make it fail loud.
    def no_push(*a, **k):
        raise AssertionError("tree create provisioning must NOT push to origin (#170)")

    def no_pr(*a, **k):
        raise AssertionError("tree create provisioning must NOT open a PR (#170)")

    def no_switch(*a, **k):
        raise AssertionError("local-only install must NOT switch branches (#170)")

    monkeypatch.setattr(gh, "git_push", no_push)
    monkeypatch.setattr(gh, "pr_create", no_pr)
    monkeypatch.setattr(gh, "git_switch_create", no_switch)

    # Drive the real install through the provisioning boundary in local mode; skip
    # the real pixi/npm spawns. The injected lefthook boundary keeps it hermetic.
    def fake_provision(cmd, *, cwd, env):
        if cmd[:2] == ["shipit", "install"]:
            assert "--local" in cmd
            rc = install_mod.run(
                str(cwd), local=True, activate_hooks=lambda root: (0, "")
            )
            assert rc == 0

    monkeypatch.setattr(create_mod, "run_provision", fake_provision)

    tree = create(_spec(tmp_path), source_repo=str(reference), github_url=str(remote))
    dest = Path(tree.path)

    # HEAD is on the PLANNED branch (never `shipit/install`).
    assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=dest) == "issues/123/work"
    assert tree.branch == "issues/123/work"

    # The managed set is present AND committed on the planned branch.
    assert (dest / "bin" / "shipit").is_file()
    tracked = _git(["ls-files"], cwd=dest).splitlines()
    assert "bin/shipit" in tracked
    assert _git(["log", "-1", "--format=%s"], cwd=dest) == install_mod.COMMIT_MESSAGE
    # Nothing left uncommitted by provisioning.
    assert _git(["status", "--porcelain"], cwd=dest) == ""

    # The 3 isolation invariants still hold (unchanged by this WS).
    assert (dest / ".git").is_dir()
    assert not (dest / ".git" / "objects" / "info" / "alternates").exists()
    assert ".claude" not in dest.parts


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

    dest = (
        tmp_path
        / "trees"
        / "acme"
        / "widget"
        / "issues"
        / "123"
        / "work-smoke-abcd1234"
    )
    assert not dest.exists()


def test_create_refuses_a_preexisting_dest_without_clobbering_it(
    tmp_path: Path, remote: Path, reference: Path, monkeypatch
):
    # A pre-existing dest (deterministic/colliding hash, or a rerun) must be refused
    # BEFORE any clone, and the rollback must NEVER delete that prior directory.
    spec = _spec(tmp_path)
    dest = (
        tmp_path
        / "trees"
        / "acme"
        / "widget"
        / "issues"
        / "123"
        / "work-smoke-abcd1234"
    )
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
            # A stub `.shipit.toml` carries the ONBOARDED marker (a [shipit] block) so
            # `_provision` runs the managed-set reconcile — that gate is
            # `config.is_onboarded`, not mere file presence (#205).
            content = (
                '[shipit]\nversion = "stub"\n' if name == ".shipit.toml" else "# stub\n"
            )
            (d / name).write_text(content)

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
    # The PARENT (this test's env, or a real coordinator) may carry the ADR-0015 build
    # vars pointing at ITS OWN Tree. They MUST be scrubbed from the child provisioning
    # env: a leaked value would shadow the child Tree's own pixi `[activation.env]` value
    # and mis-route build artifacts to the parent Tree (agy ERROR).
    monkeypatch.setenv("CARGO_TARGET_DIR", "/parent/tree/target")
    monkeypatch.setenv("SCCACHE_BASEDIRS", "/parent/tree")
    monkeypatch.setenv("CARGO_INCREMENTAL", "0")

    tree = create(_spec(tmp_path), source_repo=str(source), github_url="url")
    dest = Path(tree.path)

    # Step 3: the .treeinclude files were copied INTO the fresh Tree.
    assert (dest / ".env").read_text() == "TOKEN=1"
    assert (dest / "models" / "saml.bin").read_text() == "BIN"

    # Step 4: shipit install (LOCAL-ONLY, #170), then pixi install, then npm ci —
    # each in the Tree dir. The install MUST carry --local so Tree provisioning
    # never switches branches, pushes, or opens a PR (no origin pollution).
    assert [c[0] for c in calls] == [
        ["shipit", "install", ".", "--local"],
        ["pixi", "install"],
        ["npm", "ci"],
    ]
    assert all(cwd == dest for _, cwd, _ in calls)

    # The ADR-0015 build env is NO LONGER synthesized by shipit for the provisioning
    # subprocess (COR01): it moved to pixi `[activation.env]` so pixi sets it on every
    # activation and it reaches the agent's own in-Tree cargo. shipit therefore never
    # rewrites CARGO_TARGET_DIR to a per-`dest` value here — and, critically, the leaked
    # PARENT values set above are SCRUBBED, not carried, so pixi's per-Tree activation
    # value is authoritative on activation.
    for _, _, env in calls:
        assert "CARGO_TARGET_DIR" not in env
        assert "SCCACHE_BASEDIRS" not in env
        assert "CARGO_INCREMENTAL" not in env


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


def test_is_leaked_env_var_scrubs_build_env_but_keeps_sccache_backend_vars():
    # agy ERROR: the ADR-0015 build vars that pixi `[activation.env]` now re-sets PER-TREE
    # must be scrubbed so a leaked parent value cannot shadow the Tree's own value.
    assert create_mod.is_leaked_env_var("CARGO_TARGET_DIR")
    assert create_mod.is_leaked_env_var("SCCACHE_BASEDIRS")
    assert create_mod.is_leaked_env_var("CARGO_INCREMENTAL")
    # Install-/backend-level vars are NOT per-Tree paths and the child NEEDS them: the
    # sccache binary pointer and the cache location/credential must survive (else sccache
    # is disabled or cut off from the shared cache backend).
    assert not create_mod.is_leaked_env_var("RUSTC_WRAPPER")
    assert not create_mod.is_leaked_env_var("SCCACHE_DIR")
    assert not create_mod.is_leaked_env_var("SCCACHE_GCS_KEY")


def test_provision_env_scrubs_leaked_parent_build_env(monkeypatch):
    # COR01: the ADR-0015 build env moved to pixi `[activation.env]`; shipit no longer
    # builds it in Python AND a leaked parent value is scrubbed so it cannot shadow the
    # child Tree's per-activation value (agy ERROR). A parent SCCACHE_GCS_KEY (the cache
    # backend credential) must survive so the child keeps hitting the shared cache.
    monkeypatch.setenv("CARGO_TARGET_DIR", "/parent/tree/target")
    monkeypatch.setenv("SCCACHE_BASEDIRS", "/parent/tree")
    monkeypatch.setenv("CARGO_INCREMENTAL", "0")
    monkeypatch.setenv("SCCACHE_GCS_KEY", "creds")
    env = create_mod.provision_env()
    assert "CARGO_TARGET_DIR" not in env
    assert "SCCACHE_BASEDIRS" not in env
    assert "CARGO_INCREMENTAL" not in env
    assert env["SCCACHE_GCS_KEY"] == "creds"


def test_provision_skips_install_when_repo_not_onboarded(tmp_path: Path, monkeypatch):
    # #205: a `.shipit.toml` that carries only consumer policy (no [shipit]/[managed]
    # block) is NOT onboarded. `_provision` must NOT run `shipit install` — doing so
    # would onboard the repo fresh on every spawn and commit the onboarding artifacts
    # into the Tree branch. The pixi step (its manifest present) still runs.
    dest = tmp_path / "tree"
    dest.mkdir()
    (dest / config.CONFIG_NAME).write_text('[secrets]\nGH_PAT = { env = "X" }\n')
    (dest / create_mod.PIXI_MANIFEST).write_text("# stub\n")

    calls: list[list[str]] = []
    monkeypatch.setattr(create_mod, "run_provision", lambda cmd, **k: calls.append(cmd))
    monkeypatch.setattr(create_mod, "_st_dev", lambda p: 1)

    create_mod._provision(dest, trees_root=tmp_path / "trees")
    assert calls == [["pixi", "install"]]


def test_provision_runs_install_when_repo_onboarded(tmp_path: Path, monkeypatch):
    # An ALREADY-ONBOARDED `.shipit.toml` (it carries the [shipit]/[managed] block)
    # DOES get the managed-set reconcile — reconciling an existing managed set is the
    # legitimate #170 behavior.
    dest = tmp_path / "tree"
    dest.mkdir()
    (dest / config.CONFIG_NAME).write_text('[shipit]\nversion = "seed"\n\n[managed]\n')

    calls: list[list[str]] = []
    monkeypatch.setattr(create_mod, "run_provision", lambda cmd, **k: calls.append(cmd))
    monkeypatch.setattr(create_mod, "_st_dev", lambda p: 1)

    create_mod._provision(dest, trees_root=tmp_path / "trees")
    assert calls == [["shipit", "install", ".", "--local"]]


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

    env = create_mod.provision_env()

    # Every leaked project/environment PIXI_* pointer is gone.
    assert "PIXI_PROJECT_MANIFEST" not in env
    assert "PIXI_PROJECT_ROOT" not in env
    assert "PIXI_ENVIRONMENT_NAME" not in env
    assert "PIXI_EXE" not in env
    # Cache var and unrelated vars are preserved.
    assert env["PIXI_CACHE_DIR"] == "/home/me/.cache/rattler"
    assert env["PATH"] == "/usr/bin:/bin"


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
        env=create_mod.provision_env(),
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
