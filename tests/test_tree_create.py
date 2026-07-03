"""Integration smoke for ``tree.create.create`` — ONE real-git happy path.

Asserts the EXTERNAL result of a real ``git clone --reference … --dissociate``:
the new Tree is a fully-independent clone (no ``alternates``), sits on the planned
branch, its ``origin`` points at the remote, and the READY summary is correct.
The clone-strategy details are otherwise covered by the pure ``layout`` unit tests.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest

from shipit import config, execrun, gh, git, pixienv
from shipit.identity import Sha
from shipit.tree import create as create_mod
from shipit.tree import layout, provision
from shipit.tree.create import create, create_from_source
from shipit.identity import repo_from_slug
from shipit.tree.layout import TreeSpec
from shipit.verbs import install as install_mod
from shipit.execrun import ExecError


def _hooks_ok() -> execrun.ExecResult:
    """A canned successful lefthook-activation ExecResult for the injected boundary."""
    return execrun.ExecResult(
        argv=("lefthook", "install"), rc=0, stdout="", stderr="", duration_ms=1
    )


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
    """A real upstream repo (stands in for the GitHub URL) with one commit on main.

    It carries an ONBOARDED ``.shipit.toml`` (a ``[shipit]``/``[managed]`` block) because
    provisioning now FAILS CLOSED on a non-onboarded repo (#210): a repo shipit cuts Trees
    from is onboarded by definition, so the shared fixture reflects that steady state. The
    clone-mechanics tests stub the provisioning subprocess itself via :func:`_stub_provision`.
    """
    repo = tmp_path / "remote"
    repo.mkdir()
    _git(["init"], cwd=repo)
    (repo / "README.md").write_text("hello tree\n")
    (repo / ".shipit.toml").write_text('[shipit]\nversion = "seed"\n\n[managed]\n')
    _git(["add", "."], cwd=repo)
    _git(["commit", "-m", "init"], cwd=repo)
    _git(["branch", "-M", "main"], cwd=repo)
    return repo


def _pixi_result() -> execrun.ExecResult:
    """A canned successful ``pixi install`` ExecResult for the stubbed adapter step."""
    return execrun.ExecResult(
        argv=("pixi", "install"), rc=0, stdout="", stderr="", duration_ms=1
    )


def _stub_provision(monkeypatch):
    """No-op the provisioning subprocess boundary.

    ``create`` runs ``shipit install`` / ``npm ci`` through :func:`run_provision`
    and ``pixi install`` through the pixi adapter (:func:`shipit.pixienv.install`);
    clone-mechanics tests exercise the REAL git clone (from the onboarded ``remote``
    fixture, so provisioning is not fail-closed) but must never spawn those real
    subprocesses.
    """
    monkeypatch.setattr(create_mod, "run_provision", lambda *a, **k: None)
    monkeypatch.setattr(create_mod.pixienv, "install", lambda *a, **k: _pixi_result())


@pytest.fixture
def reference(tmp_path: Path, remote: Path) -> Path:
    """A local checkout of the remote — the ``--reference`` object donor."""
    ref = tmp_path / "ref"
    _git(["clone", str(remote), str(ref)], cwd=tmp_path)
    return ref


def _spec(tmp_path: Path) -> TreeSpec:
    return TreeSpec(
        repo=repo_from_slug("acme/widget"),
        agent_hash="abcd1234",
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
    tmp_path: Path, remote: Path, reference: Path, monkeypatch
):
    _stub_provision(monkeypatch)
    # create_from_source clones from the URL the reference checkout already uses.
    spec = _spec(tmp_path)
    tree = create_from_source(spec, source_repo=str(reference))

    dest = Path(tree.path)
    assert _git(["remote", "get-url", "origin"], cwd=dest) == str(remote)
    assert not (dest / ".git" / "objects" / "info" / "alternates").exists()


def test_tree_satisfies_the_critical_isolation_invariants(
    tmp_path: Path, remote: Path, reference: Path, monkeypatch
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
    _stub_provision(monkeypatch)
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


def test_create_hardens_the_tree_as_a_reference_donor(
    tmp_path: Path, remote: Path, reference: Path, monkeypatch
):
    """#353 suspenders, against REAL git: every minted Tree carries the four
    local-config knobs that stop it growing a split commit-graph chain — so a
    session Tree is always a safe ``--reference`` donor for its children's
    clones (both write flags AND the auto-gc/maintenance knobs; the live
    diagnosis proved the write flags alone are regenerated by ``gc --auto``)."""
    _stub_provision(monkeypatch)
    tree = create(_spec(tmp_path), source_repo=str(reference), github_url=str(remote))
    dest = Path(tree.path)

    for key, value in git.SAFE_DONOR_CONFIG:
        assert _git(["config", "--local", "--get", key], cwd=dest) == value


def test_clone_dissociated_survives_a_commit_graph_bearing_reference(
    tmp_path: Path, remote: Path, reference: Path
):
    """#353 belt, against REAL git: a reference repo carrying a split
    commit-graph chain (the exact poison — ``.git/objects/info/commit-graphs/``)
    must not be able to kill ``clone_dissociated``. On an affected git (2.54)
    the referenced clone dies at clone-time checkout and the no-reference retry
    saves it; on an unaffected git the first clone just succeeds — either way
    the call's contract holds: a populated, independent clone."""
    # Grow the reference a few commits, then write the split chain that poisons
    # it as a donor on git 2.54.
    for i in range(3):
        (reference / f"file{i}.txt").write_text(f"{i}\n")
        _git(["add", "."], cwd=reference)
        _git(["commit", "-m", f"c{i}"], cwd=reference)
    _git(["commit-graph", "write", "--reachable", "--split"], cwd=reference)
    chain = reference / ".git" / "objects" / "info" / "commit-graphs"
    assert chain.exists(), "fixture must model the poisoned donor"

    dest = tmp_path / "clone-under-test"
    git.clone_dissociated(str(remote), str(dest), reference=str(reference))

    # A real, checked-out, independent clone came back — whichever path ran.
    assert (dest / "README.md").read_text() == "hello tree\n"
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

    monkeypatch.setattr(git, "push", no_push)
    monkeypatch.setattr(gh, "pr_create", no_pr)
    monkeypatch.setattr(git, "switch_create", no_switch)

    # Drive the real install through the provisioning boundary in local mode; skip
    # the real pixi/npm spawns (the managed set writes a pixi.toml tasks block, so
    # the adapter's install step must be stubbed too). The injected lefthook
    # boundary keeps it hermetic.
    def fake_provision(cmd, *, cwd, env):
        if cmd[:2] == ["shipit", "install"]:
            assert "--local" in cmd
            rc = install_mod.run(
                str(cwd), local=True, activate_hooks=lambda root: _hooks_ok()
            )
            assert rc == 0

    monkeypatch.setattr(create_mod, "run_provision", fake_provision)
    monkeypatch.setattr(create_mod.pixienv, "install", lambda *a, **k: _pixi_result())

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

    # #232: the install commit's identity was recorded in .git/shipit-provision.json
    # so the ephemeral gc floor (and the WorktreeRemove fast path) can exclude
    # exactly it from the unpushed read.
    install_sha = _git(["rev-parse", "HEAD"], cwd=dest)
    assert provision.read_provision_shas(dest) == frozenset({Sha(install_sha)})

    # The 3 isolation invariants still hold (unchanged by this WS).
    assert (dest / ".git").is_dir()
    assert not (dest / ".git" / "objects" / "info" / "alternates").exists()
    assert ".claude" not in dest.parts


def test_create_writes_no_provision_record_when_install_is_a_noop(
    tmp_path: Path, remote: Path, reference: Path, monkeypatch
):
    # Steady state (#232): the managed-set reconcile makes NO commit, so no
    # provision record is written — the absent record is the norm, and the gc
    # exclusion set stays empty.
    _stub_provision(monkeypatch)  # provisioning runs nothing, so HEAD never moves
    tree = create(_spec(tmp_path), source_repo=str(reference), github_url=str(remote))
    dest = Path(tree.path)
    assert not provision.record_path(dest).exists()
    assert provision.read_provision_shas(dest) == frozenset()


def test_create_rolls_back_partial_tree_on_failure(
    tmp_path: Path, remote: Path, reference: Path, monkeypatch
):
    # If a post-clone step fails, the half-built leaf must not survive — otherwise
    # the next run trips over a partial directory.
    spec = _spec(tmp_path)

    def boom(*args, **kwargs):
        raise ExecError(["gh"], rc=1, stderr="checkout blew up")

    monkeypatch.setattr(git, "checkout_new_branch", boom)

    with pytest.raises(ExecError):
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


def test_create_fails_closed_and_rolls_back_on_a_non_onboarded_source(
    tmp_path: Path, remote: Path, reference: Path, monkeypatch
):
    # #210 end-to-end: cloning a Tree from a repo with NO managed block fails closed at
    # provisioning — `create` raises the clean ValueError AND rolls back the half-built
    # leaf, so a non-onboarded source never leaves a partial Tree on disk. (The `remote`
    # fixture is onboarded by default; strip its marker to make it non-onboarded.)
    (remote / ".shipit.toml").unlink()
    _git(["commit", "-am", "de-onboard"], cwd=remote)

    # Fail LOUD if provisioning is even attempted: the fail-closed check must raise
    # BEFORE any provisioning subprocess. Stubbing `run_provision` to blow up means a
    # regressed impl (one that DIDN'T fail closed) surfaces here as "provisioning ran"
    # instead of silently spawning a real `shipit install` / `pixi install` during the
    # unit run.
    def _must_not_provision(*_a, **_k):
        raise AssertionError(
            "fail-closed breached: provisioning ran on a non-onboarded repo"
        )

    monkeypatch.setattr(create_mod, "run_provision", _must_not_provision)
    monkeypatch.setattr(create_mod.pixienv, "install", _must_not_provision)

    spec = _spec(tmp_path)
    with pytest.raises(ValueError, match="not onboarded — run `shipit install`"):
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

    monkeypatch.setattr(git, "clone_dissociated", boom)

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

    monkeypatch.setattr(git, "clone_dissociated", fake_clone)
    monkeypatch.setattr(git, "configure_safe_reference_donor", lambda **k: None)
    monkeypatch.setattr(git, "fetch", lambda **k: None)
    monkeypatch.setattr(git, "checkout_new_branch", lambda *a, **k: None)


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

    # The pixi step runs through the pixi adapter (PROC02-WS02), not run_provision:
    # capture it into the SAME call list so the step ORDER stays assertable.
    def fake_pixi_install(root, *, env=None, **_k):
        calls.append((["pixi", "install"], Path(root), env))
        return _pixi_result()

    monkeypatch.setattr(create_mod.pixienv, "install", fake_pixi_install)
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

    # Step 4: shipit install (LOCAL-ONLY, #170), then pixi install (through the
    # pixi adapter), then npm ci — each in the Tree dir. The install MUST carry
    # --local so Tree provisioning never switches branches, pushes, or opens a PR
    # (no origin pollution).
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
    # An onboarded repo with pixi.toml but NO package.json runs the managed-set reconcile
    # + `pixi install`, and SKIPS `npm ci` — each dep step still gated on its manifest
    # existing. (The install step no longer skips: an onboarded repo always reconciles;
    # a non-onboarded one fails closed — see test_provision_fails_closed_*.)
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
    assert calls == [["shipit", "install", ".", "--local"], ["pixi", "install"]]


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


def test_provision_fails_closed_when_repo_not_onboarded(tmp_path: Path, monkeypatch):
    # #210 (revisiting #206): a `.shipit.toml` that carries only consumer policy (no
    # [shipit]/[managed] block) is NOT onboarded. `_provision` now FAILS CLOSED — it
    # raises a clean ValueError pointing at `shipit install`, rather than silently
    # skipping the reconcile and running the other dep steps. Nothing is provisioned.
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

    with pytest.raises(ValueError, match="not onboarded — run `shipit install`"):
        create_mod._provision(dest, trees_root=tmp_path / "trees")
    # Fail-closed: NOT even the manifest-gated `pixi install` runs on a non-onboarded repo.
    assert calls == []


def test_provision_is_clean_noop_reconcile_when_repo_onboarded(
    tmp_path: Path, monkeypatch
):
    # An ALREADY-ONBOARDED `.shipit.toml` (it carries the [shipit]/[managed] block)
    # provisions cleanly: the managed-set reconcile runs (a no-op once the set is
    # current) — reconciling an existing managed set is the legitimate #170 behavior,
    # and the onboarded path is exactly the one fail-closed protects.
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
    # run_provision must hand execrun.run the env with replace_env=True, so a parent
    # PIXI_PROJECT_MANIFEST in os.environ cannot creep back in via a merge.
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
    # Every provisioning step carries the explicit generous bound (WS03): the
    # runner's 5-minute default would kill a cold `pixi install` / `npm ci`,
    # and WS01's `timeout=None` stopgap let a wedged step hang forever.
    assert captured["timeout"] == create_mod.PROVISION_TIMEOUT


def test_run_provision_narrates_step_timing_at_info(
    tmp_path: Path, monkeypatch, caplog
):
    # WS03: provisioning steps are TIMED — beyond the runner's DEBUG Exec record,
    # each step's duration lands as an INFO record on the tree logger, so
    # Tree-birth timing is readable from the domain log.
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
    # The pixi step now runs through the pixi adapter (PROC02-WS02) rather than
    # run_provision, but it must land in the domain log the same way: one INFO
    # record with argv + duration via _narrate_step. Patching the one Exec seam
    # (execrun.run) proves the adapter call feeds the narration end to end.
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
    # The documented "no provisioning logs" gap, closed (WS03): a failed
    # provisioning Exec propagates the runner's single transport error AND leaves
    # one durable ERROR record carrying the tails of BOTH streams — stdout is
    # where pixi/npm write their real diagnostics. Driven through a real child so
    # the wiring (run_provision -> execrun -> record) is proven end to end.
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
    # Onboarded (a [shipit] block) so provisioning reaches the pixi step and its
    # cache-filesystem check — a non-onboarded repo would fail closed first (#210).
    _mock_git_boundary(monkeypatch, manifests=[".shipit.toml", "pixi.toml"])
    monkeypatch.setattr(create_mod, "run_provision", lambda *a, **k: None)
    monkeypatch.setattr(create_mod.pixienv, "install", lambda *a, **k: _pixi_result())

    cache = tmp_path / "cache"
    cache.mkdir()
    # Where the cache lives is pixi-adapter knowledge now (PROC02-WS02).
    monkeypatch.setattr(create_mod.pixienv, "cache_dir", lambda: cache)
    # Cache on a different device than everything else (the Trees root).
    monkeypatch.setattr(create_mod, "_st_dev", lambda p: 9 if Path(p) == cache else 1)

    with caplog.at_level("WARNING", logger="shipit.tree"):
        create(_spec(tmp_path), source_repo=str(source), github_url="url")

    assert any("#119" in r.message for r in caplog.records)
