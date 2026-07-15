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


# --------------------------------------------------------------------------
# CLI Artifact + generic CI policy (GEN01-WS04) — the consumer-owned
# `.shipit.toml` [lanes]/[artifacts] tables and the thin stack-neutral CI
# caller (spec §CI, §Proposed Shape; ADR-0039/0040/0060/0061).
# --------------------------------------------------------------------------


def _shipit_toml(**kw):
    """The parsed generated ``.shipit.toml`` for the default plan."""
    text = {f.path: f.text for f in _plan(**kw).files}[".shipit.toml"]
    return tomllib.loads(text)


def test_plan_shipit_manifest_declares_one_cli_artifact_with_rust_build_target():
    # AC1: one Artifact named after the project, one Rust build target whose
    # package is the CLI package.
    artifacts = _shipit_toml()["artifacts"]
    assert list(artifacts) == ["hello"]
    assert artifacts["hello"]["build"] == [{"toolchain": "rust", "package": "hello"}]


def test_plan_artifact_carries_no_endpoint_bundle_signing_or_release_policy():
    # AC2: the Artifact declaration is a bare build-target claim — no endpoint,
    # Bundle, signing, publishing, or release policy anywhere in `.shipit.toml`.
    cfg = _shipit_toml()
    artifact = cfg["artifacts"]["hello"]
    assert set(artifact) == {"build"}  # only the build target, nothing else
    target = artifact["build"][0]
    assert set(target) == {"toolchain", "package"}
    forbidden = (
        "endpoint",
        "bundle",
        "sign",
        "signing",
        "publish",
        "publishing",
        "release",
    )

    def _keys(node):
        # Every mapping key anywhere in the parsed manifest — policy lives in
        # keys/tables, so traverse the structure instead of stringifying it
        # (a value like a `release-tool` project name must not trip the check).
        if isinstance(node, dict):
            for key, value in node.items():
                yield key.lower()
                yield from _keys(value)
        elif isinstance(node, list):
            for item in node:
                yield from _keys(item)

    manifest_keys = list(_keys(cfg))
    for word in forbidden:
        offenders = [key for key in manifest_keys if word in key]
        assert not offenders, (
            f"unexpected {word!r} policy key in .shipit.toml: {offenders}"
        )


def test_plan_shipit_manifest_declares_required_lint_and_test_lanes_only():
    # AC5: required lint and test lanes, no default PR (or any) build lane.
    lanes = _shipit_toml()["lanes"]
    assert list(lanes) == ["lint", "test"]  # exactly these, in order
    assert "build" not in lanes
    for name in ("lint", "test"):
        assert lanes[name]["run"] == name
        assert lanes[name]["required"] is True
        assert lanes[name]["local"] is True


def test_generated_lanes_parse_and_derive_lint_test_commit_push_checks():
    # AC5 proven through the real config loader + lane planner, not string
    # matching: the generated policy is a valid Lane/Tool declaration whose
    # required∩local commit/push checks and merge-blocking PR matrix are exactly
    # lint + test.
    from shipit import config
    from shipit.tools import lanes as lane_planner

    parsed = config.load_lanes(_shipit_toml())
    assert [lane.name for lane in parsed] == ["lint", "test"]
    assert all(lane.required for lane in parsed)
    assert [lane.name for lane in lane_planner.commit_push_checks(parsed)] == [
        "lint",
        "test",
    ]
    jobs = lane_planner.plan(parsed, event="pr")
    assert [(job.name, job.required) for job in jobs] == [
        ("lint", True),
        ("test", True),
    ]


def test_plan_ci_caller_is_valid_yaml_delegating_to_reusable_checks():
    # AC4 + AC6: the generated caller is structurally valid YAML that delegates
    # to shipit's reusable checks workflow by floating major ref, with no Cargo
    # command or other Rust-specific execution logic.
    import yaml

    text = {f.path: f.text for f in _plan().files}[".github/workflows/ci.yml"]
    doc = yaml.safe_load(text)  # raises on malformed YAML
    checks = doc["jobs"]["checks"]
    assert checks["uses"] == "arthur-debert/shipit/.github/workflows/wf-checks.yml@v1"
    lowered = text.lower()
    for token in ("cargo", "rustc", "rustup", "cross build"):
        assert token not in lowered, f"CI caller must not name {token!r}"


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
