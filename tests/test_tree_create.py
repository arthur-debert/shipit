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
from shipit.execrun import ExecError
from shipit.identity import repo_from_slug
from shipit.install import units as iunits
from shipit.install.apply import COMMIT_MESSAGE as INSTALL_COMMIT_MESSAGE
from shipit.tree import create as create_mod
from shipit.tree import layout, provision
from shipit.tree.create import create, create_from_source
from shipit.tree.layout import TreeSpec

# A valid FULL git sha (40 hex) standing in for a real Shipit pin. The pin gate
# (config.shipit_pin) validates the value as a Sha, so a fixture pin must be a
# real sha shape, not a sentinel like "seed" (ADR-0033). "5eed…" is a mnemonic
# all-hex sha.
_PIN = "5eed" * 10
_PINNED_MANIFEST = f'[shipit]\nversion = "{_PIN}"\n\n[managed]\n'


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

    It carries a PINNED ``.shipit.toml`` (a ``[shipit].version`` pin) because
    provisioning FAILS CLOSED on a pinless base (ADR-0033): a repo shipit cuts Trees
    from is bootstrapped by definition, so the shared fixture reflects that steady
    state. The clone-mechanics tests stub the provisioning subprocess itself via
    :func:`_stub_provision`.
    """
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
    """A canned successful ``pixi install`` ExecResult for the stubbed adapter step."""
    return execrun.ExecResult(
        argv=("pixi", "install"), rc=0, stdout="", stderr="", duration_ms=1
    )


def _stub_provision(monkeypatch):
    """No-op the provisioning subprocess boundary.

    ``create`` runs the frozen node install (#543) through :func:`run_provision`
    and ``pixi install`` through the pixi adapter (:func:`shipit.pixienv.install`);
    clone-mechanics tests exercise the REAL git clone (from the onboarded ``remote``
    fixture, so provisioning is not fail-closed) but must never spawn those real
    subprocesses.
    """
    monkeypatch.setattr(create_mod, "run_provision", lambda *a, **k: None)
    monkeypatch.setattr(create_mod.pixienv, "install", lambda *a, **k: _pixi_result())
    # The #443 hook-activation step runs through the SAME pixi adapter — stub it
    # too so a Tree that carries lefthook.yml never spawns a real `pixi run`.
    monkeypatch.setattr(create_mod.pixienv, "run_in_env", lambda *a, **k: _hooks_ok())


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


def _poison_split_chain(reference: Path) -> Path:
    """Split commit-graph chain — the donor state #353 first met in the wild."""
    _git(["commit-graph", "write", "--reachable", "--split"], cwd=reference)
    return reference / ".git" / "objects" / "info" / "commit-graphs"


def _poison_plain_commit_graph(reference: Path) -> Path:
    """A plain, non-split ``objects/info/commit-graph`` file — #372 narrowed the
    trigger to ANY commit-graph in the reference, not just the split chain."""
    _git(["commit-graph", "write", "--reachable"], cwd=reference)
    return reference / ".git" / "objects" / "info" / "commit-graph"


def _poison_multi_pack_index(reference: Path) -> Path:
    """A multi-pack-index (needs packs first). The #372 diagnosis found the MIDX
    incidental — a MIDX-only donor must ALSO clone clean on the first attempt."""
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
    """#353/#372 belt, against REAL git: a reference repo carrying commit-graph
    or MIDX state must not be able to kill ``clone_dissociated`` — and since the
    ``-c core.commitGraph=false`` fix (#372) the referenced clone must succeed
    on the FIRST attempt (no degraded full-clone retry), keeping the
    near-instant ``--reference`` borrow. On git 2.54 the stock command dies at
    clone-time checkout for the commit-graph donors (stale in-process graph
    state after ``--dissociate`` severs the alternate), so a regression here
    fails loud via the no-WARNING assertion."""
    # Grow the reference a few commits, then poison it as a donor.
    for i in range(3):
        (reference / f"file{i}.txt").write_text(f"{i}\n")
        _git(["add", "."], cwd=reference)
        _git(["commit", "-m", f"c{i}"], cwd=reference)
    artifact = poison(reference)
    assert artifact.exists(), "fixture must model the poisoned donor"

    # `file://` forces the real pack transport: a plain local-path clone
    # hardlinks objects and never consults the reference's commit-graph, so it
    # cannot reproduce the failure (verified: the stock command passes with a
    # path URL and dies with file:// on git 2.54).
    dest = tmp_path / "clone-under-test"
    with caplog.at_level(logging.WARNING, logger="shipit.git"):
        git.clone_dissociated(remote.as_uri(), str(dest), reference=str(reference))

    # A real, checked-out, independent clone came back...
    assert (dest / "README.md").read_text() == "hello tree\n"
    assert not (dest / ".git" / "objects" / "info" / "alternates").exists()
    # ...on the FIRST attempt: the #353 full-clone retry never fired. Filter by
    # logger so an unrelated warning elsewhere cannot flake this assertion.
    assert not [
        r
        for r in caplog.records
        if r.name == "shipit.git" and r.levelno >= logging.WARNING
    ]
    # And nothing about the -c fix leaked into the new clone's persistent config.
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
    """#509, against REAL git: when the ``--reference`` donor is a git LINKED
    worktree (the shape ``Agent(isolation: worktree)`` hands the review funnel as
    a PR's source workdir), the stock ``clone --reference`` refuses it — ``fatal:
    reference repository '<path>' as a linked checkout is not supported yet`` —
    and the read-only review clone died at launch, silently losing the local
    (codex/agy) review. ``clone_dissociated`` must dereference the worktree to its
    shared common gitdir (a normal, valid ``--reference`` source) so the borrow
    still works and the clone comes back independent (ADR-0014: no alternates)."""
    # A real linked worktree off the reference checkout, on its own branch.
    linked = tmp_path / "linked-wt"
    _git(["worktree", "add", "-b", "wt-branch", str(linked)], cwd=reference)
    # Sanity: it is a LINKED worktree (its `.git` is a pointer FILE, not a dir),
    # so this fixture really models the shape git 2.54 refuses as a reference.
    assert (linked / ".git").is_file()

    dest = tmp_path / "clone-under-test"
    with caplog.at_level(logging.INFO, logger="shipit.git"):
        # file:// forces the real pack transport, matching the funnel's clone.
        git.clone_dissociated(remote.as_uri(), str(dest), reference=str(linked))

    # A real, checked-out, INDEPENDENT clone came back (no "linked checkout is
    # not supported" ExecError), carrying the upstream content...
    assert (dest / "README.md").read_text() == "hello tree\n"
    # ...and --dissociate held: no alternates link back to the deref'd donor.
    assert not (dest / ".git" / "objects" / "info" / "alternates").exists()
    # The deref was narrated so the trail shows the worktree was dereferenced.
    assert any(
        r.name == "shipit.git"
        and r.levelno == logging.INFO
        and str(linked) in r.getMessage()
        for r in caplog.records
    )


def test_resolve_reference_donor_derefs_worktree_but_passes_normal_through(
    tmp_path: Path, reference: Path
):
    """The #509 helper in isolation, against REAL git dirs:

    - a NORMAL checkout is returned VERBATIM (its per-worktree gitdir IS the
      common dir), so the fast common path is never perturbed; and
    - a LINKED worktree is dereferenced to its shared common gitdir — the
      repo's ``.git`` — which is what ``--reference`` can actually borrow from.
    """
    # Normal checkout → unchanged.
    assert git._resolve_reference_donor(str(reference)) == str(reference)

    # A non-git path → unchanged (the probe fails; the normal path is untouched).
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    assert git._resolve_reference_donor(str(plain)) == str(plain)

    # Linked worktree → the shared common gitdir (the reference's `.git`).
    linked = tmp_path / "linked-wt2"
    _git(["worktree", "add", "-b", "wt2", str(linked)], cwd=reference)
    resolved = git._resolve_reference_donor(str(linked))
    assert Path(resolved) == (reference / ".git").resolve()


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


def test_create_mutates_nothing_managed_zero_commits_on_a_pinned_base(
    tmp_path: Path, remote: Path, reference: Path, monkeypatch
):
    # ADR-0033 end-to-end against REAL git: a Tree cut from a PINNED base carries
    # ZERO `chore(shipit)` commits — provisioning performs NO managed-set mutation
    # at all. HEAD stays exactly the base commit, the working tree stays clean,
    # and nothing pushes / opens a PR / switches branches. (The TRE03-era
    # `shipit install --local` step and its drift-window reconcile commit are
    # deleted; the pin makes Tree and tool coherent by construction.)
    def no_push(*a, **k):
        raise AssertionError("tree create provisioning must NOT push to origin")

    def no_pr(*a, **k):
        raise AssertionError("tree create provisioning must NOT open a PR")

    def no_switch(*a, **k):
        raise AssertionError("tree create provisioning must NOT switch branches")

    monkeypatch.setattr(git, "push", no_push)
    monkeypatch.setattr(gh, "pr_create", no_pr)
    monkeypatch.setattr(git, "switch_create", no_switch)

    # Any provisioning subprocess that is NOT a dep step is the bug: only the
    # manifest-gated dep installs may run, never a `shipit install`.
    def no_managed_mutation(cmd, **_k):
        raise AssertionError(f"provisioning ran an unexpected step: {cmd}")

    monkeypatch.setattr(create_mod, "run_provision", no_managed_mutation)
    monkeypatch.setattr(create_mod.pixienv, "install", lambda *a, **k: _pixi_result())
    monkeypatch.setattr(create_mod.pixienv, "run_in_env", lambda *a, **k: _hooks_ok())

    base_sha = _git(["rev-parse", "main"], cwd=remote)
    tree = create(_spec(tmp_path), source_repo=str(reference), github_url=str(remote))
    dest = Path(tree.path)

    # HEAD is on the PLANNED branch, at EXACTLY the base commit: zero commits made.
    assert _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=dest) == "issues/123/work"
    assert _git(["rev-parse", "HEAD"], cwd=dest) == base_sha
    assert INSTALL_COMMIT_MESSAGE not in _git(["log", "--format=%s"], cwd=dest)
    # Nothing left uncommitted by provisioning either.
    assert _git(["status", "--porcelain"], cwd=dest) == ""

    # The 3 isolation invariants still hold (unchanged by this WS).
    assert (dest / ".git").is_dir()
    assert not (dest / ".git" / "objects" / "info" / "alternates").exists()
    assert ".claude" not in dest.parts


def test_create_writes_no_provision_record(
    tmp_path: Path, remote: Path, reference: Path, monkeypatch
):
    # ADR-0033: provisioning never commits, so the #232 provision record's writer
    # is retired — NO Tree born after the pin carries `.git/shipit-provision.json`,
    # and the gc exclusion set reads empty.
    _stub_provision(monkeypatch)
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


def test_create_fails_closed_and_rolls_back_on_a_pinless_source(
    tmp_path: Path, remote: Path, reference: Path, monkeypatch
):
    # ADR-0033 end-to-end: cloning a Tree from a base with NO [shipit].version pin
    # fails closed at provisioning — `create` raises the clean ValueError naming the
    # bootstrap install AND rolls back the half-built leaf, so a pinless source never
    # leaves a partial Tree on disk. (The `remote` fixture is pinned by default;
    # strip its manifest to make it pinless.)
    (remote / ".shipit.toml").unlink()
    _git(["commit", "-am", "de-pin"], cwd=remote)

    # Fail LOUD if provisioning is even attempted: the fail-closed check must raise
    # BEFORE any provisioning subprocess. Stubbing the boundaries to blow up means a
    # regressed impl (one that DIDN'T fail closed) surfaces here as "provisioning ran"
    # instead of silently spawning a real `pixi install` during the unit run.
    def _must_not_provision(*_a, **_k):
        raise AssertionError("fail-closed breached: provisioning ran on a pinless base")

    monkeypatch.setattr(create_mod, "run_provision", _must_not_provision)
    monkeypatch.setattr(create_mod.pixienv, "install", _must_not_provision)

    spec = _spec(tmp_path)
    with pytest.raises(ValueError, match="no \\[shipit\\].version pin"):
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
            # A stub `.shipit.toml` carries the Shipit pin ([shipit].version) so
            # `_provision`'s fail-closed pin gate passes — that gate is
            # `config.shipit_pin`, not mere file presence (ADR-0033). JSON
            # manifests must PARSE: the node-deps step reads package.json for
            # the packageManager pin (#543).
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
    monkeypatch.setattr(git, "checkout_new_branch", lambda *a, **k: None)
    monkeypatch.setattr(git, "submodule_update_init", lambda **k: None)


def test_create_copies_treeinclude_and_provisions_deps(tmp_path: Path, monkeypatch):
    # The source checkout carries the gitignored-but-needed files + the allow-list.
    source = tmp_path / "source"
    source.mkdir()
    (source / ".treeinclude").write_text(".env\nmodels/\n")
    (source / ".env").write_text("TOKEN=1")
    (source / "models").mkdir()
    (source / "models" / "saml.bin").write_text("BIN")

    _mock_git_boundary(
        monkeypatch,
        # package-lock.json is the npm detection signal for the node-deps step
        # (#543): a bare package.json with no packageManager pin and no
        # recognized lockfile now fails loud instead of running `npm ci` blind.
        manifests=[".shipit.toml", "pixi.toml", "package.json", "package-lock.json"],
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

    # Step 4: pixi install (through the pixi adapter), then npm ci — each in the
    # Tree dir. NO `shipit install` step: provisioning mutates nothing managed
    # (ADR-0033 — the pin keeps Tree and tool coherent by construction).
    assert [c[0] for c in calls] == [
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


def test_create_initializes_submodules_after_checkout_before_provision(
    tmp_path: Path, monkeypatch
):
    # #485: a dissociated clone leaves submodules as empty gitlinks, so the write path
    # MUST run `git submodule update --init --recursive` — after the branch is cut and
    # before provisioning, matching CI's `submodules: recursive`. Assert the seam is
    # issued (fake the git boundary; no live network) and that it runs before any
    # provisioning step.
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
        git, "checkout_new_branch", lambda *a, **k: order.append("checkout")
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

    # Submodule init happened, and strictly between checkout and provisioning.
    assert order == ["checkout", "submodule", "provision"]


def test_create_rolls_back_when_submodule_init_fails(
    tmp_path: Path, remote: Path, reference: Path, monkeypatch
):
    # #485 fail-loud: a submodule fetch that fails (auth/network) must abort
    # materialization and roll the half-built leaf back — never leave a Tree with a
    # silently empty submodule dir the suite would fail on later.
    _stub_provision(monkeypatch)

    def boom(**_k):
        raise ExecError(["git", "submodule"], rc=1, stderr="auth failed")

    monkeypatch.setattr(git, "submodule_update_init", boom)

    with pytest.raises(ExecError):
        create(_spec(tmp_path), source_repo=str(reference), github_url=str(remote))

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


def test_create_skips_provisioning_steps_whose_manifest_is_absent(
    tmp_path: Path, monkeypatch
):
    # A pinned repo with pixi.toml but NO package.json runs `pixi install` and
    # SKIPS `npm ci` — each dep step gated on its manifest existing. No install
    # step runs at all (ADR-0033: provisioning mutates nothing managed; a
    # pinless base fails closed — see test_provision_fails_closed_*).
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


# --------------------------------------------------------------------------
# #543 — the node-deps step is package-manager-aware: `npm ci` hard-fails on a
# pnpm/yarn repo, and the svelte prettier leg then fails open SILENTLY (#498/
# #542 plugin-load carve-out), so detection must pick the matching frozen
# install and fail LOUD when it cannot decide.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pin", "argv"),
    [
        ("npm@11.4.2", ["npm", "ci"]),
        ("pnpm@10.29.3", ["pnpm", "install", "--frozen-lockfile"]),
        # Yarn's frozen flag is version-dependent (#545): Berry (v2+) takes
        # `--immutable`, classic (v1) only `--frozen-lockfile` — a `yarn@1.x`
        # corepack pin is a valid, common project and must not hard-fail.
        ("yarn@4.9.1", ["yarn", "install", "--immutable"]),
        ("yarn@1.22.22", ["yarn", "install", "--frozen-lockfile"]),
    ],
)
def test_node_install_argv_honours_the_packagemanager_pin(
    tmp_path: Path, pin: str, argv: list[str]
):
    # The corepack `packageManager` pin is the repo's own declaration — the
    # AUTHORITATIVE signal, honoured for each supported manager.
    (tmp_path / "package.json").write_text(f'{{"packageManager": "{pin}"}}\n')
    assert create_mod.node_install_argv(tmp_path) == argv


def test_node_install_argv_rejects_a_yarn_pin_with_no_numeric_major(tmp_path: Path):
    # A yarn packageManager pin whose version has no numeric major is malformed
    # for corepack: fail loud rather than guess a frozen flag (#545).
    (tmp_path / "package.json").write_text('{"packageManager": "yarn@stable"}\n')
    with pytest.raises(ValueError, match="unparseable yarn version"):
        create_mod.node_install_argv(tmp_path)


def test_node_install_argv_pin_wins_over_a_conflicting_lockfile(tmp_path: Path):
    # Precedence: the packageManager pin beats whatever lockfile happens to be
    # on disk — a stray package-lock.json in a pnpm repo must not resurrect the
    # very `npm ci` that #543 exists to kill.
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
    # No packageManager pin → the (single) lockfile names its manager. The
    # bannerless yarn.lock stub reads as Berry (v2+) → `--immutable` (#545).
    (tmp_path / "package.json").write_text("{}\n")
    (tmp_path / lockfile).write_text("stub\n")
    assert create_mod.node_install_argv(tmp_path) == argv


def test_node_install_argv_reads_the_yarn_v1_banner_without_a_pin(tmp_path: Path):
    # Lockfile-only yarn (no packageManager pin): the `# yarn lockfile v1` banner
    # is what tells a classic lockfile (`--frozen-lockfile`) from a Berry one
    # (`--immutable`) — a v1 repo without a pin is common and must not hard-fail
    # by getting Berry's flag (#545).
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
    # package.json with NO pin and NO recognized lockfile: refusing to guess is
    # the point of #543 — a wrong install fails open downstream, silently.
    (tmp_path / "package.json").write_text("{}\n")
    with pytest.raises(ValueError, match="no recognized lockfile"):
        create_mod.node_install_argv(tmp_path)


def test_node_install_argv_fails_loud_on_ambiguous_lockfiles(tmp_path: Path):
    # Two lockfiles and no pin is a misconfigured repo, not a coin toss.
    (tmp_path / "package.json").write_text("{}\n")
    (tmp_path / "package-lock.json").write_text("{}\n")
    (tmp_path / "yarn.lock").write_text("stub\n")
    with pytest.raises(ValueError, match="multiple lockfiles"):
        create_mod.node_install_argv(tmp_path)


def test_node_install_argv_fails_loud_on_an_unknown_manager(tmp_path: Path):
    # An unrecognized packageManager (e.g. bun) fails loud rather than falling
    # back to a lockfile that contradicts the repo's own declaration.
    (tmp_path / "package.json").write_text('{"packageManager": "bun@1.2.3"}\n')
    with pytest.raises(ValueError, match="unsupported packageManager"):
        create_mod.node_install_argv(tmp_path)


def test_node_install_argv_fails_loud_on_unparseable_manifest(tmp_path: Path):
    # A package.json that does not parse cannot name a manager: fail loud, do
    # not skip the node-deps step (the silent-skip is the #543 fail-open).
    (tmp_path / "package.json").write_text("# not json\n")
    with pytest.raises(ValueError, match="unparseable package.json"):
        create_mod.node_install_argv(tmp_path)


def test_node_install_argv_fails_loud_on_a_non_object_manifest(tmp_path: Path):
    # A package.json that parses but is not a JSON object is malformed — do not
    # silently drop to the lockfile heuristic; fail loud like an unparseable one.
    (tmp_path / "package.json").write_text("[1, 2, 3]\n")
    (tmp_path / "package-lock.json").write_text("{}\n")
    with pytest.raises(ValueError, match="not an object"):
        create_mod.node_install_argv(tmp_path)


def test_provision_runs_the_pnpm_frozen_install_on_a_pnpm_repo(
    tmp_path: Path, monkeypatch
):
    # End to end through `_provision` (#543, the simple-gal-ui shape): a pinned
    # pnpm repo (packageManager pin + pnpm-lock.yaml, no package-lock.json)
    # provisions with `pnpm install --frozen-lockfile`, never `npm ci`.
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
    # The loud failure rides the atomicity contract: an undecidable node
    # manifest aborts the materialization and the half-built leaf is removed —
    # never a Tree whose node deps were silently skipped (#543).
    source = tmp_path / "source"
    source.mkdir()
    _mock_git_boundary(monkeypatch, manifests=[".shipit.toml", "package.json"])
    monkeypatch.setattr(create_mod, "_st_dev", lambda p: 1)

    with pytest.raises(ValueError, match="no recognized lockfile"):
        create(_spec(tmp_path), source_repo=str(source), github_url="url")
    leaf = (
        tmp_path
        / "trees"
        / "acme"
        / "widget"
        / "issues"
        / "123"
        / "work-smoke-abcd1234"
    )
    assert not leaf.exists()


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


def test_provision_fails_closed_when_base_is_pinless(tmp_path: Path, monkeypatch):
    # ADR-0033's one surviving guard: a `.shipit.toml` that carries only consumer
    # policy (no [shipit].version pin) is PINLESS — its bin/shipit has no build to
    # exec. `_provision` FAILS CLOSED with a clean ValueError naming the bootstrap
    # install, rather than provisioning a Tree every in-Tree verb would die in.
    # Nothing is provisioned.
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
    # Fail-closed: NOT even the manifest-gated `pixi install` runs on a pinless base.
    assert calls == []


def test_provision_runs_no_step_at_all_on_a_pinned_manifestless_repo(
    tmp_path: Path, monkeypatch
):
    # A PINNED `.shipit.toml` with no pixi.toml / package.json provisions ZERO
    # subprocesses: the managed-set install step is deleted outright (ADR-0033 —
    # provisioning mutates nothing managed), and every dep step is gated on its
    # manifest existing.
    dest = tmp_path / "tree"
    dest.mkdir()
    (dest / config.CONFIG_NAME).write_text(_PINNED_MANIFEST)

    calls: list[list[str]] = []
    monkeypatch.setattr(create_mod, "run_provision", lambda cmd, **k: calls.append(cmd))
    monkeypatch.setattr(create_mod, "_st_dev", lambda p: 1)

    create_mod._provision(dest, trees_root=tmp_path / "trees")
    assert calls == []


# --------------------------------------------------------------------------
# #443 Finding A — a provisioned Tree comes up ARMED: git hooks do not clone,
# so when the clone carries the managed lefthook.yml, provisioning activates
# them (`lefthook install` through the Tree's OWN pixi lint env).
# --------------------------------------------------------------------------


def _provision_stubs(monkeypatch) -> list[tuple[list[str], Path, object]]:
    """Stub all three provisioning boundaries into ONE ordered call list."""
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
    # The steady-state spawn/tree-create case (#443): the clone already carries
    # the managed set (lefthook.yml + the pixi blocks) and provisioning mutates
    # nothing managed (ADR-0033) — so provisioning itself must run `lefthook
    # install`, via the lint env where the managed blocks pin lefthook, right
    # after the env provision and before the npm step.
    dest = tmp_path / "tree"
    dest.mkdir()
    (dest / config.CONFIG_NAME).write_text(_PINNED_MANIFEST)
    (dest / pixienv.MANIFEST_NAME).write_text("# stub\n")
    (dest / iunits.LEFTHOOK_FILE).write_text("# stub\n")
    (dest / "package.json").write_text("{}\n")
    (dest / "package-lock.json").write_text("{}\n")  # the npm signal (#543)

    calls = _provision_stubs(monkeypatch)
    create_mod._provision(dest, trees_root=tmp_path / "trees")

    assert [c[0] for c in calls] == [
        ["pixi", "install"],
        ["pixi", "run", "-e", "lint", "lefthook", "install"],
        ["npm", "ci"],
    ]
    # Every step — activation included — runs in the Tree with the SAME scrubbed
    # provisioning env (never a merge back over os.environ).
    assert all(cwd == dest for _, cwd, _ in calls)
    envs = [env for _, _, env in calls]
    assert all(env is envs[0] and env is not None for env in envs)


def test_provision_skips_hook_activation_without_a_lefthook_config(
    tmp_path: Path, monkeypatch
):
    # No lefthook.yml → nothing to activate: the step is gated on its manifest
    # existing, like every other dep step.
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
    # A lefthook.yml with NO pixi.toml has no lint env to activate through — the
    # activation rides the pixi branch (the lint env IS a pixi env), so it is
    # skipped with the rest of the pixi steps rather than spawning a doomed
    # `pixi run` in a manifest-less checkout.
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
    # Unlike apply's opportunistic activation, the Tree step is CHECKED: a Tree
    # that cannot arm its hooks is a failed materialization (#443 — a disarmed
    # Tree is exactly the bug), so the ExecError propagates like any other
    # provisioning failure and `create` rolls the half-built leaf back.
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
    # Atomicity: the half-built leaf was rolled back.
    leaf = (
        tmp_path
        / "trees"
        / "acme"
        / "widget"
        / "issues"
        / "123"
        / "work-smoke-abcd1234"
    )
    assert not leaf.exists()


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
