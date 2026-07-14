"""Tests for the ``shipit repo new`` repository-creation domain (GEN01-WS01).

Layered like the module (``docs/spec/repo-new.md``; ADR-0055–0063):

- pure value tests — name validation/derivation, the TOML renderer, the strict
  text renderer, profile resolution, and plan composition/conflict detection;
- orchestrator tests — the effectful flow with INJECTED effect seams (managed
  install, pixi provision, staged Checks) and REAL Git, observing the published
  Repo's files, branch, single ``Initial commit``, clean tree, and Check
  ordering as OUTCOMES, never by asserting private helper calls (the aligned
  public test seam);
- verb tests — the thin CLI parser/renderer and the ``error:`` + exit-1 mapping.

The real-toolchain certification — an actual ``pixi install`` + Rust build +
``pixi run lint/test/build`` end to end — is deliberately gated behind
``SHIPIT_REPO_NEW_E2E`` so the default ``pixi run test`` stays fast; the effect
seams make the orchestration fully exercisable without it.
"""

from __future__ import annotations

import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from shipit import git
from shipit.repocreate import (
    CreationError,
    build_plan,
    create_repo,
    resolve_profiles,
    tomlio,
    validate_name,
)
from shipit.repocreate import create as create_mod
from shipit.repocreate.profiles import RustProfile
from shipit.repocreate.templates import render_text

# --------------------------------------------------------------------------
# names
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["hello", "my-tool", "a", "a1", "web-app-2"])
def test_validate_name_accepts_canonical_kebab_case(name):
    assert validate_name(name).value == name


def test_project_name_derives_packages_and_crate_identifiers():
    n = validate_name("my-tool")
    assert n.cli_pkg == "my-tool"
    assert n.lib_pkg == "libmy-tool"
    assert n.cli_crate == "my_tool"
    assert n.lib_crate == "libmy_tool"


@pytest.mark.parametrize(
    "bad", ["", "Hello", "my_tool", "-x", "x-", "a--b", "1abc", "a.b", "a b"]
)
def test_validate_name_refuses_non_kebab(bad):
    with pytest.raises(CreationError):
        validate_name(bad)


# --------------------------------------------------------------------------
# tomlio — the one format-aware structured renderer (ADR-0058)
# --------------------------------------------------------------------------


def test_tomlio_renders_scalars_arrays_and_tables():
    text = tomlio.dumps(
        {
            "workspace": {
                "name": "hello",
                "channels": ["conda-forge"],
                "platforms": ["linux-64", "osx-arm64"],
            }
        }
    )
    assert "[workspace]" in text
    assert 'name = "hello"' in text
    assert 'channels = ["conda-forge"]' in text
    assert 'platforms = ["linux-64", "osx-arm64"]' in text


def test_tomlio_renders_nested_and_inline_tables_and_dotted_keys():
    text = tomlio.dumps(
        {
            "package": {"name": "hello", "version.workspace": True},
            "dependencies": {"lib": tomlio.Inline({"path": "../lib"})},
        }
    )
    assert "version.workspace = true" in text
    assert 'lib = { path = "../lib" }' in text


def test_tomlio_renders_bool_and_array_of_inline_tables():
    text = tomlio.dumps(
        {"artifacts": {"hello": {"build": [tomlio.Inline({"toolchain": "rust"})]}}}
    )
    assert "[artifacts.hello]" in text
    assert 'build = [{ toolchain = "rust" }]' in text


def test_tomlio_escapes_strings():
    assert tomlio.dumps({"t": {"k": 'a"b\\c'}}) == '[t]\nk = "a\\"b\\\\c"\n'


def test_tomlio_escapes_control_characters():
    # A literal newline/tab must become a TOML escape, never a raw control
    # character that breaks the basic string (and thus TOML parsing). DEL
    # (U+007F) is the case where TOML's escaping requirement diverges from
    # JSON's: `json.dumps` leaves it literal, so `_quote` escapes it by hand.
    assert tomlio.dumps({"t": {"k": "a\nb\tc\x7f"}}) == '[t]\nk = "a\\nb\\tc\\u007f"\n'


def test_tomlio_rejects_unserializable_value():
    with pytest.raises(TypeError):
        tomlio.dumps({"t": {"k": object()}})


# --------------------------------------------------------------------------
# templates — strict text rendering (ADR-0058)
# --------------------------------------------------------------------------


def test_render_text_substitutes_known_placeholders():
    assert render_text("hi {{ name }}", {"name": "x"}) == "hi x"


def test_render_text_raises_on_undefined_variable():
    with pytest.raises(CreationError):
        render_text("hi {{ missing }}", {"name": "x"})


def test_render_text_fails_closed_on_malformed_placeholder():
    # A placeholder the identifier pattern rejects (hyphen) is not substituted;
    # rather than shipping a literal brace pair into a generated file, render
    # fails loud so a template typo can never reach a Repo.
    with pytest.raises(CreationError):
        render_text("pkg {{ cli-pkg }}", {"cli-pkg": "x"})


def test_render_text_allows_brace_pairs_in_context_values():
    # The malformed-brace scan runs over the TEMPLATE (minus valid placeholders),
    # not the rendered output: a substituted value may legitimately contain
    # `{{`/`}}` (e.g. a code snippet), and that must not be rejected.
    out = render_text("body {{ snippet }}", {"snippet": "let x = vec![{{1}}];"})
    assert out == "body let x = vec![{{1}}];"


# --------------------------------------------------------------------------
# profiles — the closed registry (ADR-0056/0063)
# --------------------------------------------------------------------------


def test_resolve_profiles_requires_at_least_one_stack():
    with pytest.raises(CreationError):
        resolve_profiles(())


def test_resolve_profiles_refuses_unknown_stack():
    with pytest.raises(CreationError):
        resolve_profiles(("go",))


def test_resolve_profiles_refuses_duplicate_stack():
    with pytest.raises(CreationError):
        resolve_profiles(("rust", "rust"))


def test_rust_profile_contributes_workspace_deps_ignore_and_artifact():
    c = RustProfile().contribute(validate_name("hello"))
    paths = {f.path for f in c.owned_files}
    assert "Cargo.toml" in paths
    assert "crates/hello/Cargo.toml" in paths
    assert "crates/hello/src/main.rs" in paths
    assert "crates/hello/tests/cli.rs" in paths
    assert "crates/libhello/Cargo.toml" in paths
    assert "crates/libhello/src/lib.rs" in paths
    assert ("cargo-nextest", "*") in c.pixi_dependencies
    assert "/target/" in c.gitignore_lines
    assert c.artifacts[0].name == "hello" and c.artifacts[0].package == "hello"


# --------------------------------------------------------------------------
# plan — central composition + conflict detection (ADR-0057)
# --------------------------------------------------------------------------


def _plan(name="hello", author="Ada Lovelace", year=2026):
    return build_plan(
        validate_name(name), resolve_profiles(("rust",)), author=author, year=year
    )


def test_plan_composes_universal_seed_and_profile_files():
    files = {f.path: f.text for f in _plan().files}
    assert set(files) >= {
        "README.md",
        "LICENSE",
        ".gitignore",
        ".github/workflows/ci.yml",
        "pixi.toml",
        ".shipit.toml",
        "Cargo.toml",
        "crates/hello/Cargo.toml",
        "crates/libhello/src/lib.rs",
    }


def test_plan_license_carries_author_and_year():
    text = {f.path: f.text for f in _plan(author="Grace H", year=1999).files}["LICENSE"]
    assert "Copyright (c) 1999 Grace H" in text


def test_plan_gitignore_has_universal_seed_plus_rust_target():
    text = {f.path: f.text for f in _plan().files}[".gitignore"]
    assert ".pixi/" in text and "node_modules/" in text
    assert "/target/" in text
    # Lockfiles are never ignored (spec §Proposed Shape).
    assert "Cargo.lock" not in text and "pixi.lock" not in text


def test_plan_pixi_manifest_declares_build_task_and_nextest():
    text = {f.path: f.text for f in _plan().files}["pixi.toml"]
    assert 'build = "./bin/shipit build"' in text
    assert "cargo-nextest" in text
    # The managed lint/test blocks are NOT duplicated by the scaffold.
    assert 'test = "./bin/shipit test"' not in text
    assert 'lint = "./bin/shipit lint"' not in text


def test_plan_detects_conflicting_owned_file():
    class _Clash:
        key = "clash"

        def contribute(self, name):
            from shipit.repocreate.profiles import Contribution, OwnedFile

            return Contribution(owned_files=(OwnedFile("README.md", "x"),))

    with pytest.raises(CreationError):
        build_plan(validate_name("hello"), (_Clash(),), author="a", year=2026)


# --------------------------------------------------------------------------
# create — the orchestrator, injected effect seams + real Git (ADR-0059/0062)
# --------------------------------------------------------------------------


@pytest.fixture
def git_identity(monkeypatch):
    """Give the child ``git commit`` a deterministic identity + isolated config."""
    for var, val in {
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test Author",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }.items():
        monkeypatch.setenv(var, val)


class _Recorder:
    """A fake effect seam recording invocation order and writing a marker file."""

    def __init__(self, order, label, *, writes=None, raises=None):
        self.order = order
        self.label = label
        self.writes = writes
        self.raises = raises

    def __call__(self, root: Path) -> None:
        self.order.append(self.label)
        if self.writes is not None:
            (root / self.writes).write_text("marker\n", encoding="utf-8")
        if self.raises is not None:
            raise self.raises


def _fake_create(parent, order, **overrides):
    kwargs = dict(
        installer=_Recorder(order, "install", writes="MANAGED.md"),
        provisioner=_Recorder(order, "provision", writes="pixi.lock"),
        verifier=_Recorder(order, "verify"),
        author_reader=lambda root: "Test Author",
        year=2026,
    )
    kwargs.update(overrides)
    return create_repo("hello", parent, ("rust",), **kwargs)


def test_create_publishes_verified_repo(tmp_path, git_identity):
    order: list[str] = []
    result = _fake_create(tmp_path, order)

    dest = tmp_path / "hello"
    assert result.destination == dest
    assert result.stacks == ("rust",)
    # The generated files landed at the destination.
    assert (dest / "Cargo.toml").is_file()
    assert (dest / "crates/hello/src/main.rs").is_file()
    assert (dest / "MANAGED.md").is_file()  # managed baseline installed
    assert (dest / "pixi.lock").is_file()  # pixi provisioned + locked
    # Git: on main, exactly one root commit named Initial commit, clean tree.
    assert git.current_branch(cwd=str(dest)) == "main"
    assert git.head_commit(cwd=str(dest)).value == result.initial_commit
    subjects = subprocess.run(
        ["git", "-C", str(dest), "log", "--pretty=%s"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert subjects == ["Initial commit"]  # exactly one root commit
    assert git.status_porcelain(cwd=str(dest)) == []
    # The three public Checks ran, install/provision before them, in order.
    assert order == ["install", "provision", "verify"]
    # No staging siblings survive under the parent (iterdir order is
    # filesystem-dependent, so compare as a sorted list).
    assert sorted(p.name for p in tmp_path.iterdir()) == ["hello"]


def test_create_accepts_empty_destination_directory(tmp_path, git_identity):
    (tmp_path / "hello").mkdir()
    result = _fake_create(tmp_path, [])
    assert result.destination == tmp_path / "hello"
    assert (tmp_path / "hello" / "Cargo.toml").is_file()


def test_create_published_repo_respects_umask(tmp_path, git_identity):
    # `mkdtemp` stages at 0o700; the published Repo must instead respect the
    # user's umask like `git init`/`cargo new` (0o755 under a 0o022 umask), not
    # ship `rwx------` and break shared workspaces / container mounts.
    old = os.umask(0o022)
    try:
        _fake_create(tmp_path, [])
    finally:
        os.umask(old)
    assert (tmp_path / "hello").stat().st_mode & 0o777 == 0o755


def test_create_cleans_staging_when_umask_stage_fails(
    tmp_path, git_identity, monkeypatch
):
    # The umask probe/chmod runs right after the staging sibling is created; a
    # filesystem error there must still remove the sibling. The try/cleanup guard
    # wraps it, so no partial `.shipit-repo-new-*` directory leaks.
    def deny(self, mode):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "chmod", deny)
    with pytest.raises(OSError):
        _fake_create(tmp_path, [])
    assert list(tmp_path.iterdir()) == []


def test_create_reports_destination_through_a_symlink_parent(tmp_path, git_identity):
    # A symlink parent is accepted, but the reported destination stays
    # `<parent>/<name>` (a path *through* the link), not the resolved real path.
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    result = _fake_create(link, [])
    assert result.destination == link / "hello"
    # It nonetheless materializes behind the link.
    assert (real / "hello" / "Cargo.toml").is_file()


def test_create_failed_check_rolls_back_and_leaves_destination_absent(
    tmp_path, git_identity
):
    order: list[str] = []
    with pytest.raises(CreationError):
        _fake_create(
            tmp_path,
            order,
            verifier=_Recorder(order, "verify", raises=CreationError("lint failed")),
        )
    # Nothing published; no staging sibling left behind.
    assert not (tmp_path / "hello").exists()
    assert list(tmp_path.iterdir()) == []


def test_create_refuses_missing_parent(tmp_path):
    with pytest.raises(CreationError):
        _fake_create(tmp_path / "nope", [])


def test_create_refuses_non_empty_destination(tmp_path, git_identity):
    (tmp_path / "hello").mkdir()
    (tmp_path / "hello" / "keep").write_text("x", encoding="utf-8")
    with pytest.raises(CreationError):
        _fake_create(tmp_path, [])


def test_create_refuses_file_destination(tmp_path):
    (tmp_path / "hello").write_text("x", encoding="utf-8")
    with pytest.raises(CreationError):
        _fake_create(tmp_path, [])


def test_create_refuses_symlink_destination(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / "hello").symlink_to(target)
    with pytest.raises(CreationError):
        _fake_create(tmp_path, [])


def test_create_maps_uninspectable_destination_to_creation_error(tmp_path, monkeypatch):
    # A destination that exists but cannot be listed (e.g. not readable) makes
    # `iterdir` raise `PermissionError`; the stat-based probes can raise the same
    # on `EACCES`. Either way the refusal must stay a handled CreationError (the
    # verb's `error:` + exit-1 contract), never a raw traceback.
    dest = tmp_path / "hello"
    dest.mkdir()

    def deny(self):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "iterdir", deny)
    with pytest.raises(CreationError):
        create_mod._assert_absent_or_empty(dest)


def test_default_author_raises_without_git_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(create_mod.git, "author_name", lambda *, cwd: None)
    monkeypatch.setattr(create_mod.git, "committer_name", lambda *, cwd: None)
    with pytest.raises(CreationError):
        create_mod.default_author(tmp_path)


def test_default_author_raises_when_only_committer_unresolved(tmp_path, monkeypatch):
    # git resolves author and committer INDEPENDENTLY: a setup with only
    # GIT_AUTHOR_* set resolves an author but no committer, and the Initial
    # commit needs both. `default_author` must catch that as a preflight
    # failure, never let creation proceed to a raw commit-time git error.
    monkeypatch.setattr(create_mod.git, "author_name", lambda *, cwd: "Ada Lovelace")
    monkeypatch.setattr(create_mod.git, "committer_name", lambda *, cwd: None)
    with pytest.raises(CreationError):
        create_mod.default_author(tmp_path)


def test_default_author_returns_name_when_both_resolve(tmp_path, monkeypatch):
    monkeypatch.setattr(create_mod.git, "author_name", lambda *, cwd: "Ada Lovelace")
    monkeypatch.setattr(create_mod.git, "committer_name", lambda *, cwd: "Ada Lovelace")
    assert create_mod.default_author(tmp_path) == "Ada Lovelace"


def _stub_install_pipeline(monkeypatch, *, hooks_activated, hooks_detail=""):
    """Stub the in-process install pipeline so ``default_installer`` can be
    driven without a real gather/reconcile/apply, forcing a chosen activation
    outcome from ``apply``."""
    from shipit.install import apply as apply_mod
    from shipit.install import reconcile as reconcile_mod
    from shipit.install import units as units_mod

    monkeypatch.setattr(reconcile_mod, "detect_toolchains", lambda root: ())
    monkeypatch.setattr(units_mod, "load_units", lambda *, toolchains: ())
    monkeypatch.setattr(reconcile_mod, "load_retired", lambda: ())
    monkeypatch.setattr(reconcile_mod, "load_retired_hooks", lambda: ())
    monkeypatch.setattr(reconcile_mod, "gather", lambda *a, **k: None)
    monkeypatch.setattr(reconcile_mod, "reconcile", lambda *a, **k: "PLAN")
    monkeypatch.setattr(
        apply_mod,
        "apply",
        lambda plan, mode: apply_mod.InstallResult(
            plan="PLAN",
            mode=mode,
            hooks_activated=hooks_activated,
            hooks_detail=hooks_detail,
        ),
    )


def test_default_installer_fails_closed_when_hooks_do_not_activate(
    tmp_path, monkeypatch
):
    # A degraded MODE_TREE activation returns hooks_activated=False; creation's
    # contract (active hooks before the initial commit) means that must abort,
    # not publish a Repo with dormant hooks.
    _stub_install_pipeline(
        monkeypatch, hooks_activated=False, hooks_detail="lefthook not found"
    )
    with pytest.raises(CreationError, match="hooks did not activate"):
        create_mod.default_installer(tmp_path)


@pytest.mark.parametrize("hooks_activated", [True, None])
def test_default_installer_accepts_activated_or_no_op_hooks(
    tmp_path, monkeypatch, hooks_activated
):
    # True (activated) and None (nothing to activate) are both success.
    _stub_install_pipeline(monkeypatch, hooks_activated=hooks_activated)
    create_mod.default_installer(tmp_path)  # does not raise


# --------------------------------------------------------------------------
# identity + ignore hygiene (GEN01-WS03)
#
# The universal seed's consumer-owned identity (README, LICENSE, Cargo metadata)
# and hygiene (.gitignore) surfaces, locked as CONTRACTS: the README's shape, the
# canonical MIT text with real attribution and no placeholder, the inherited Cargo
# metadata parsed as DATA (proving format-aware serialization, ADR-0058), and —
# per the acceptance criteria — the ignore behavior proven through REAL Git
# (`git add`/`git ls-files`/`git check-ignore`), not rendered-text matching alone.
# --------------------------------------------------------------------------


def _plan_files(name="hello", author="Ada Lovelace", year=2026):
    return {f.path: f.text for f in _plan(name=name, author=author, year=year).files}


def test_readme_names_project_lists_commands_without_badges_or_urls():
    readme = _plan_files(name="my-tool")["README.md"]
    # Names the project and lists the three canonical public commands.
    assert "# my-tool" in readme
    for command in ("pixi run lint", "pixi run test", "pixi run build"):
        assert command in readme
    # No badges and no repository URLs (spec §Proposed Shape): no image/badge
    # markdown, no shields, and no link back to a source-host repo.
    assert "![" not in readme
    assert "shields.io" not in readme
    assert "badge" not in readme.lower()
    assert "github.com" not in readme


def test_license_is_canonical_mit_with_attribution_and_no_placeholder():
    license_text = _plan_files(author="Grace Hopper", year=1999)["LICENSE"]
    # The canonical MIT text — its distinctive opening and warranty clauses.
    assert license_text.startswith("MIT License")
    assert "Permission is hereby granted, free of charge" in license_text
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in license_text
    # Real attribution: the local creation year and resolved Git author name.
    assert "Copyright (c) 1999 Grace Hopper" in license_text
    # No unrendered placeholder and no alternate-license prompt/choice (spec:
    # V1 offers no license selection — the one permitted license is MIT).
    assert "{{" not in license_text and "}}" not in license_text
    for alt in ("Apache", "GPL", "BSD", "choose", "SPDX"):
        assert alt not in license_text


def test_cargo_metadata_declares_versions_and_is_inherited_by_members():
    files = _plan_files(name="my-tool")
    # Parsing with a real TOML reader proves the manifests are serialized as
    # DATA (ADR-0058), not text templates that merely happen to look like TOML.
    workspace = tomllib.loads(files["Cargo.toml"])
    assert workspace["workspace"]["resolver"] == "3"
    package = workspace["workspace"]["package"]
    assert package == {"version": "0.1.0", "edition": "2024", "license": "MIT"}
    # Both members inherit version/edition/license from the workspace rather than
    # restating literals (`version.workspace = true` → {"workspace": True}).
    for member in ("crates/my-tool/Cargo.toml", "crates/libmy-tool/Cargo.toml"):
        inherited = tomllib.loads(files[member])["package"]
        assert inherited["version"] == {"workspace": True}
        assert inherited["edition"] == {"workspace": True}
        assert inherited["license"] == {"workspace": True}


def _stage_repo_with_representative_tree(root: Path) -> None:
    """Write the generated plan into ``root`` and synthesize the paths that real
    Rust/pixi/Node/Python/agent activity produces, so a single ``git add`` proves
    what the generated ``.gitignore`` keeps out of the index and what it lets in.
    """
    git.init_main(cwd=str(root))
    for path, text in _plan_files().items():
        dest = root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")

    def touch(rel: str) -> None:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")

    # Generated build/environment output + cross-stack caches that MUST be ignored.
    for ignored in (
        "target/debug/hello",  # Rust build (profile /target/)
        ".pixi/envs/default/bin/hello",  # pixi environment
        ".direnv/env",
        ".env",
        ".env.local",  # local environment files
        ".DS_Store",  # OS
        "notes.swp",
        "backup~",  # editor
        ".todos.db",
        ".claude/worktrees/w/scratch",  # agent worktree
        "node_modules/pkg/index.js",
        ".npm/cache",
        ".pnpm-store/x",  # Node
        "__pycache__/mod.cpython-313.pyc",
        "stale.pyc",
        ".venv/bin/python",
        "hello.egg-info/PKG-INFO",  # Python
        ".pytest_cache/x",
        ".mypy_cache/x",
        ".ruff_cache/x",
        ".coverage",
        "coverage/lcov.info",
        "htmlcov/index.html",
    ):
        touch(ignored)

    # Files that MUST stay tracked: source, ecosystem lockfiles, managed agent
    # config, the `.env.example` negation, and a broad product-output dir (`dist/`)
    # the seed deliberately does NOT guess.
    for tracked in (
        "Cargo.lock",
        "pixi.lock",  # ecosystem lockfiles are never ignored
        ".claude/agents/implementer.md",  # managed agent configuration
        ".env.example",  # re-included by the `!.env.example` negation
        "dist/hello",  # dist/ is not guessed, so it is tracked, not ignored
    ):
        touch(tracked)


def test_git_add_ignores_generated_output_and_tracks_source(tmp_path):
    # The acceptance contract proven through Git itself: after `git add -A`, the
    # index (git ls-files) must EXCLUDE every generated build/environment/cache
    # path and INCLUDE source, manifests, ecosystem lockfiles, `.shipit.toml`, and
    # managed agent configuration.
    _stage_repo_with_representative_tree(tmp_path)
    git.add_all(cwd=str(tmp_path))
    tracked = set(git.ls_files(cwd=str(tmp_path)))

    for path in (
        "target/debug/hello",
        ".pixi/envs/default/bin/hello",
        ".direnv/env",
        ".env",
        ".env.local",
        ".DS_Store",
        "notes.swp",
        "backup~",
        ".todos.db",
        ".claude/worktrees/w/scratch",
        "node_modules/pkg/index.js",
        ".npm/cache",
        ".pnpm-store/x",
        "__pycache__/mod.cpython-313.pyc",
        "stale.pyc",
        ".venv/bin/python",
        "hello.egg-info/PKG-INFO",
        ".pytest_cache/x",
        ".mypy_cache/x",
        ".ruff_cache/x",
        ".coverage",
        "coverage/lcov.info",
        "htmlcov/index.html",
    ):
        assert path not in tracked, f"{path} should be ignored but was tracked"

    for path in (
        "crates/hello/src/main.rs",
        "crates/hello/tests/cli.rs",
        "crates/libhello/src/lib.rs",
        "Cargo.toml",
        "pixi.toml",
        ".shipit.toml",
        "README.md",
        "LICENSE",
        ".gitignore",
        ".github/workflows/ci.yml",
        "Cargo.lock",
        "pixi.lock",
        ".claude/agents/implementer.md",
        ".env.example",
        "dist/hello",  # not guessed → tracked, proving no broad output ignore
    ):
        assert path in tracked, f"{path} should be tracked but was ignored"


def test_git_check_ignore_keeps_managed_config_and_honors_env_negation(tmp_path):
    # Targeted `git check-ignore` proof of the two subtle boundaries: the
    # `.claude/` split (managed agent config tracked, only `worktrees/` ignored)
    # and the `.env.*` / `!.env.example` negation.
    git.init_main(cwd=str(tmp_path))
    (tmp_path / ".gitignore").write_text(_plan_files()[".gitignore"], encoding="utf-8")

    def ignored(rel: str) -> bool:
        return (
            subprocess.run(
                ["git", "-C", str(tmp_path), "check-ignore", "-q", rel]
            ).returncode
            == 0
        )

    # Only the agent-worktree subtree is ignored under `.claude/`; managed config
    # (agents, skills) stays tracked so reconciliation keeps its authority.
    assert ignored(".claude/worktrees/w/scratch")
    assert not ignored(".claude/agents/implementer.md")
    assert not ignored(".claude/skills/coordinating.md")
    # `.env` and its variants are ignored, but the example template is re-included.
    assert ignored(".env")
    assert ignored(".env.local")
    assert not ignored(".env.example")
    # A broad product-output directory is NOT guessed by the consumer seed.
    assert not ignored("dist/hello")


# --------------------------------------------------------------------------
# verb — thin CLI parser/renderer + error mapping
# --------------------------------------------------------------------------


def test_run_new_renders_destination_and_commit(monkeypatch, capsys, tmp_path):
    from shipit.repocreate import CreationResult
    from shipit.verbs import repo as repo_verb

    monkeypatch.setattr(
        repo_verb,
        "create_repo",
        lambda name, parent, stacks: CreationResult(
            destination=tmp_path / name,
            initial_commit="abcdef1234567890",
            stacks=stacks,
        ),
    )
    rc = repo_verb.run_new(stacks=("rust",), name="hello", parent=tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert str(tmp_path / "hello") in out
    assert "abcdef123456" in out


def test_run_new_maps_creation_error_to_exit_one(capsys, tmp_path):
    from shipit.verbs import repo as repo_verb

    # Unknown stack fails in resolve_profiles before any effect.
    rc = repo_verb.run_new(stacks=("go",), name="hello", parent=tmp_path)
    assert rc == 1
    assert capsys.readouterr().err.startswith("error:")


# --------------------------------------------------------------------------
# real-toolchain certification — gated (heavy: pixi solve + Rust build)
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("SHIPIT_REPO_NEW_E2E"),
    reason="set SHIPIT_REPO_NEW_E2E=1 to run the full pixi+cargo certification",
)
def test_create_real_toolchain_end_to_end(tmp_path, git_identity):
    # A HYPHENATED canonical name exercises the crate/package naming boundary —
    # notably `CARGO_BIN_EXE_<bin>`, which Cargo sets under the binary-target
    # name verbatim (`my-tool`, dash preserved), the spelling the black-box test
    # references. A dashless name would never surface a hyphen regression.
    result = create_repo("my-tool", tmp_path, ("rust",))
    dest = result.destination
    assert (dest / "Cargo.toml").is_file()
    assert (dest / "pixi.lock").is_file()
    assert git.current_branch(cwd=str(dest)) == "main"
    # Re-run every public Check against the published Repo — the full
    # certification contract the module documents (lint, test, build), not just
    # build — proving the generated Repo stands on its own.
    for task in ("lint", "test", "build"):
        run = subprocess.run(
            ["pixi", "run", "--manifest-path", str(dest / "pixi.toml"), task],
            capture_output=True,
            text=True,
        )
        assert run.returncode == 0, f"pixi run {task} failed:\n{run.stderr}"
