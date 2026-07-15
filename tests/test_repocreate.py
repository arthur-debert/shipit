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

from shipit import execrun, git, pixienv, redact
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


# --- names: request-grammar edges (WS02) --------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "a",  # one-letter name
        "a1",  # a digit right after the first letter
        "x9y",  # a digit mid-segment
        "a-b-c",  # a multi-segment kebab name
        "web-app-2",  # a digit-tailed segment
        # Names that merely CONTAIN a reservation as a substring are fine — the
        # reservation check matches whole derived identifiers, not fragments.
        "testing",  # not the reserved `test`
        "builder",  # not the reserved `build`
        "fnord",  # not the keyword `fn`
        "console",  # not the Windows device `con`
        "async-worker",  # a segment resembling a keyword, but the whole name isn't
    ],
)
def test_validate_name_accepts_grammar_edges(name):
    assert validate_name(name).value == name


@pytest.mark.parametrize(
    "reserved",
    [
        # Rust keywords (whichever survive the kebab grammar as a bare name).
        "fn",
        "async",
        "await",
        "match",
        "type",
        "self",
        "super",
        "crate",
        "move",
        "mut",
        "use",
        "enum",
        "trait",
        "loop",
        "dyn",
        # Rust's built-in `test` crate.
        "test",
        # Cargo build-directory artifact-name collisions (hard errors --bin).
        "build",
        "deps",
        "examples",
        "incremental",
        # Windows reserved device names — unusable as a directory on Windows.
        "con",
        "prn",
        "aux",
        "nul",
        "com1",
        "com9",
        "lpt1",
        "lpt9",
    ],
)
def test_validate_name_refuses_cargo_reserved(reserved):
    # A name that passes the kebab grammar can still be one the managed Cargo
    # toolchain (or a Windows checkout) refuses; those are rejected here with an
    # actionable error rather than silently rewritten (spec §Risks).
    with pytest.raises(CreationError, match="invalid project name"):
        validate_name(reserved)


def test_validate_name_reserved_message_names_the_offender():
    # The rejection is actionable: it names the exact offending identifier so an
    # operator can fix the request without reading the spec.
    with pytest.raises(CreationError, match=r"'fn'"):
        validate_name("fn")


@pytest.mark.parametrize(
    "identifier",
    ["fn", "test", "build", "deps", "examples", "incremental", "con", "lpt3"],
)
def test_reject_if_reserved_covers_every_reservation_class(identifier):
    # The one reservation gate is exercised directly across all four classes —
    # keyword, the `test` crate, artifact-dir collision, and Windows device —
    # so the derived-identifier callers (library package, crate identifiers) are
    # covered even though no valid `lib<name>` derivation lands on a reservation.
    from shipit.repocreate.names import _reject_if_reserved

    with pytest.raises(CreationError):
        _reject_if_reserved(identifier, "the project name")


def test_reject_if_reserved_passes_a_safe_identifier():
    from shipit.repocreate.names import _reject_if_reserved

    _reject_if_reserved("libhello", "the derived library package")  # does not raise


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
    # The lint lane's provisioned twin IS seeded, in the lint feature (so the
    # CI lane resolves onto the lint env — see the lane-env regression below).
    assert 'lint-full = "./bin/shipit lint"' in text
    assert "[feature.lint.tasks]" in text


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
    # The lint lane runs the `lint-full` twin (provisions the lint env); the
    # test lane rides the managed `test` task directly (its tooling is default).
    assert lanes["lint"]["run"] == "lint-full"
    assert lanes["test"]["run"] == "test"
    for name in ("lint", "test"):
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


def test_generated_lint_lane_provisions_the_lint_env_not_default():
    # Regression (GEN01-WS07 QA, #930): the generated hosted-CI lint lane MUST
    # run in the `lint` pixi env — where shfmt/prettier/markdownlint/actionlint/
    # shellcheck/yamllint and the managed rust lint toolchain are provisioned —
    # not the default env (rust only). `shipit ci plan` keys a lane's env off the
    # feature that declares its `run` task; the earlier lane pointed at the
    # managed bare `lint` task (default `[tasks]`) so the runner installed only
    # the default env and every non-rust lint binary was missing (`FileNotFound`,
    # rc 127) — green locally only because dev machines carry those tools on PATH
    # and the hook forces `-e lint`. The lane now runs the `lint-full` twin
    # declared in `[feature.lint.tasks]`, which resolves onto the `lint` env.
    from shipit import config
    from shipit.tools import lanes as lane_planner

    pixi = tomllib.loads({f.path: f.text for f in _plan().files}["pixi.toml"])
    task_envs = lane_planner.task_env_sets(pixi)
    assert task_envs["lint-full"] == ("lint",)  # the twin lives in the lint env

    parsed = config.load_lanes(_shipit_toml())
    jobs = lane_planner.plan(parsed, event="pr", task_envs=task_envs)
    by_name = {job.name: job for job in jobs}
    assert by_name["lint"].envs == ("lint",)  # not ("default",) — the defect
    assert by_name["test"].envs == ("default",)  # test tooling is default-env


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


def test_create_publishes_only_the_committed_relocatable_tree(tmp_path, git_identity):
    # Relocatability regression (#942): staged certification (ADR-0062) builds
    # the Rust workspace and materializes the pixi environment in the temporary
    # sibling, and BOTH embed that staging path as an absolute location — Cargo
    # bakes `CARGO_BIN_EXE_<bin>` into the compiled black-box test, and the
    # conda-based `.pixi` env hard-codes its prefix. Publication (ADR-0059) must
    # carry ONLY the committed tree, so no such ignored artifact can make the
    # published Repo resolve the vanished staging path after the atomic rename.
    order: list[str] = []
    captured: dict[str, Path] = {}

    def verify_writing_staging_path_artifacts(root: Path) -> None:
        order.append("verify")
        captured["staging"] = root
        # Ignored build/environment output whose CONTENT embeds the staging path,
        # exactly as a real Rust build + pixi provision bakes it in.
        for rel in ("target/debug/hello", ".pixi/envs/default/bin/hello"):
            artifact = root / rel
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(str(root), encoding="utf-8")

    result = _fake_create(
        tmp_path, order, verifier=verify_writing_staging_path_artifacts
    )
    dest = result.destination
    staging = captured["staging"]

    # The published destination carries none of the ignored certification state:
    # the strip runs against the abstract root, not a `target`/`.pixi` allowlist.
    assert not (dest / "target").exists()
    assert not (dest / ".pixi").exists()
    # And NOTHING under the destination references the vanished staging path, so
    # no carried artifact can resolve it (the `.git` store keeps relative paths).
    for path in dest.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            body = path.read_text(encoding="utf-8", errors="ignore")
            assert str(staging) not in body, f"{path} still references {staging}"
    # The committed tree is intact and clean — stripping touched nothing tracked.
    assert (dest / "Cargo.toml").is_file()
    assert (dest / "crates/hello/src/main.rs").is_file()
    assert (dest / "pixi.lock").is_file()
    assert git.head_commit(cwd=str(dest)).value == result.initial_commit
    assert git.status_porcelain(cwd=str(dest)) == []


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
# failure-atomic publication + Git policy (GEN01-WS05)
#
# The atomic-publish contract (ADR-0059) and the untouched Git policy (ADR-0062;
# spec §"Design Decisions" and §Risks): every stage that can fail — managed
# install, lockfile generation, the lint/test/build Checks, and the root commit
# (missing identity, invalid signing, any other commit failure) — must return
# non-zero, publish NOTHING, and leave the requested destination in its preflight
# state (absent stays absent; a pre-existing empty directory stays empty) with no
# `.shipit-repo-new-*` sibling leaked. The pre-publish recheck refuses a
# destination that content appeared in mid-creation without replacing or merging.
# A cleanup failure is reported ALONGSIDE the primary failure and still never
# publishes. Exercised at both the effect seam and the public command, through
# observable filesystem, exit-status, output, and Git outcomes.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwarg,label", [("installer", "install"), ("provisioner", "provision")]
)
def test_create_failed_stage_rolls_back_and_publishes_nothing(
    tmp_path, git_identity, kwarg, label
):
    # A failure at the managed-install or lockfile-generation stage aborts before
    # the commit ever runs: the destination is never created and the staging
    # sibling is removed, so no initial commit lands at the requested path.
    # (The verify/Check stage is covered by
    # `test_create_failed_check_rolls_back_and_leaves_destination_absent`.)
    order: list[str] = []
    with pytest.raises(CreationError):
        _fake_create(
            tmp_path,
            order,
            **{kwarg: _Recorder(order, label, raises=CreationError(f"{label} failed"))},
        )
    assert not (tmp_path / "hello").exists()
    assert list(tmp_path.iterdir()) == []  # no destination, no leaked sibling


def test_create_commit_failure_preserves_git_policy_and_prevents_publication(
    tmp_path, git_identity, monkeypatch
):
    # A commit failure — an INVALID SIGNING config or any other `git commit`
    # error — must prevent publication WITHOUT creation synthesizing an
    # identity, disabling signing, or bypassing hooks. (A missing identity is
    # not a commit-time failure: `create_repo` preflights it via `author_reader`
    # BEFORE any effect, raising `CreationError` earlier — see `default_author`.)
    # Creation reports the underlying git error unchanged (ADR-0062) and rolls
    # back: the destination stays absent and the staging sibling is removed.
    from shipit.execrun import ExecError

    def failing_commit(message, *, cwd, no_verify=False):
        # `no_verify` must stay False — creation never bypasses the hooks that a
        # signing/identity policy runs through.
        assert no_verify is False
        raise ExecError(
            ["git", "commit", "-m", message],
            rc=128,
            stderr="error: gpg failed to sign the data",
        )

    monkeypatch.setattr(create_mod.git, "commit_all", failing_commit)
    with pytest.raises(ExecError) as exc:
        _fake_create(tmp_path, [])
    # The git error is surfaced unchanged (its own argv/message), not reworded
    # into a synthetic success or a weakened error.
    assert "git commit" in str(exc.value)
    assert not (tmp_path / "hello").exists()
    assert list(tmp_path.iterdir()) == []


def test_create_failure_leaves_pre_existing_empty_destination_empty(
    tmp_path, git_identity
):
    # The other half of the rollback contract: when the destination pre-existed
    # as an EMPTY directory at preflight, a handled failure leaves it present and
    # still empty (not removed, not populated with the partial staged Repo).
    dest = tmp_path / "hello"
    dest.mkdir()
    order: list[str] = []
    with pytest.raises(CreationError):
        _fake_create(
            tmp_path,
            order,
            verifier=_Recorder(order, "verify", raises=CreationError("lint failed")),
        )
    assert dest.is_dir()
    assert list(dest.iterdir()) == []  # still empty
    # Only the untouched empty destination remains — no leaked staging sibling.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["hello"]


def test_create_refuses_destination_that_appeared_during_creation(
    tmp_path, git_identity
):
    # The concurrent-publication race: a rival creator populates the destination
    # AFTER preflight, while the staged Repo is being verified. The pre-publish
    # recheck (ADR-0059) must refuse rather than rename over it — the concurrent
    # content is neither replaced nor merged into, and the staging sibling is
    # removed.
    dest = tmp_path / "hello"
    order: list[str] = []

    def racing_verify(root: Path) -> None:
        order.append("verify")
        dest.mkdir()
        (dest / "other").write_text("concurrent", encoding="utf-8")

    with pytest.raises(CreationError):
        _fake_create(tmp_path, order, verifier=racing_verify)
    # The rival's content survives intact; the staged Repo was NOT published over
    # it (no Cargo.toml appeared), and no staging sibling leaked.
    assert (dest / "other").read_text(encoding="utf-8") == "concurrent"
    assert not (dest / "Cargo.toml").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["hello"]


def test_create_cleanup_failure_is_reported_alongside_primary_and_never_publishes(
    tmp_path, git_identity, monkeypatch
):
    # When rollback itself fails, the primary failure still propagates (nothing is
    # published) AND the cleanup failure rides alongside it as a note (ADR-0059),
    # so a leaked `.shipit-repo-new-*` sibling is surfaced rather than silently
    # orphaned or mistaken for success.
    def failing_rmtree(path, ignore_errors=False):
        raise OSError(39, "Directory not empty")

    monkeypatch.setattr(create_mod.shutil, "rmtree", failing_rmtree)
    order: list[str] = []
    with pytest.raises(CreationError) as exc:
        _fake_create(
            tmp_path,
            order,
            verifier=_Recorder(order, "verify", raises=CreationError("lint failed")),
        )
    # Nothing published: the destination never came into being.
    assert not (tmp_path / "hello").exists()
    # The cleanup failure is attached to the in-flight primary failure.
    notes = getattr(exc.value, "__notes__", [])
    assert any("could not be removed" in note for note in notes)
    # The sibling did leak (cleanup could not remove it) — exactly what the note
    # reports, and never a published Repo.
    leaked = [
        p.name for p in tmp_path.iterdir() if p.name.startswith(".shipit-repo-new-")
    ]
    assert leaked


# --- WS05 through the public command ------------------------------------------
#
# The verb wraps `create_repo` in the shared `error:`+exit-1 shell (ADR-0030).
# These drive the real `run_new` for a POST-preflight failure — a failed Check
# and the concurrent-publication race — with injected effect seams (the same
# `repo_verb.create_repo` monkeypatch the happy-path verb test uses), observing
# exit status, the `error:` line, and the untouched filesystem.


def _run_new_with_seams(monkeypatch, tmp_path, capsys, *, verifier):
    """Drive the real `repo new` command with injected effect seams.

    Neutralizes install/provision so no toolchain is needed and routes the given
    ``verifier`` through the real `create_repo` (and thus the real staging,
    commit, atomic-publish, and rollback path). Returns ``(rc, stderr)``.
    """
    from shipit.verbs import repo as repo_verb

    order: list[str] = []

    def wired(name, parent, stacks):
        return create_repo(
            name,
            parent,
            stacks,
            installer=_Recorder(order, "install", writes="MANAGED.md"),
            provisioner=_Recorder(order, "provision", writes="pixi.lock"),
            verifier=verifier,
            author_reader=lambda root: "Test Author",
            year=2026,
        )

    monkeypatch.setattr(repo_verb, "create_repo", wired)
    rc = repo_verb.run_new(stacks=("rust",), name="hello", parent=tmp_path)
    return rc, capsys.readouterr().err


def test_command_reports_failed_check_and_leaves_destination_absent(
    tmp_path, capsys, git_identity, monkeypatch
):
    rc, err = _run_new_with_seams(
        monkeypatch,
        tmp_path,
        capsys,
        verifier=_Recorder(
            [], "verify", raises=CreationError("staged Check `pixi run lint` failed")
        ),
    )
    assert rc == 1
    assert err.startswith("error:")
    assert "pixi run lint" in err  # the failing stage is named in the output
    assert not (tmp_path / "hello").exists()
    assert list(tmp_path.iterdir()) == []


def test_command_refuses_concurrent_destination_and_preserves_it(
    tmp_path, capsys, git_identity, monkeypatch
):
    dest = tmp_path / "hello"

    def racing_verify(root: Path) -> None:
        dest.mkdir()
        (dest / "other").write_text("concurrent", encoding="utf-8")

    rc, err = _run_new_with_seams(monkeypatch, tmp_path, capsys, verifier=racing_verify)
    assert rc == 1
    assert err.startswith("error:")
    # The concurrent content is preserved, never overwritten or merged into.
    assert (dest / "other").read_text(encoding="utf-8") == "concurrent"
    assert not (dest / "Cargo.toml").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["hello"]


def test_command_reports_cleanup_failure_alongside_primary_on_stderr(
    tmp_path, capsys, git_identity, monkeypatch
):
    # The cleanup-failure report must reach the USER, not just the raw exception:
    # when rollback's `rmtree` fails, `create_repo` attaches the report as a note
    # (ADR-0059), and the shared CLI error shell folds `__notes__` into the single
    # `error:` line — otherwise the leaked `.shipit-repo-new-*` sibling would be
    # silently orphaned on the public `repo new` path (only the primary error
    # would show). This is the public-path counterpart to the effect-seam test
    # `test_create_cleanup_failure_is_reported_alongside_primary_and_never_publishes`.
    def failing_rmtree(path, ignore_errors=False):
        raise OSError(39, "Directory not empty")

    monkeypatch.setattr(create_mod.shutil, "rmtree", failing_rmtree)
    rc, err = _run_new_with_seams(
        monkeypatch,
        tmp_path,
        capsys,
        verifier=_Recorder([], "verify", raises=CreationError("lint failed")),
    )
    assert rc == 1
    assert err.startswith("error:")
    # Both the primary failure and the cleanup report ride the one stderr line.
    assert "lint failed" in err
    assert "could not be removed" in err
    # The leaked sibling is named in the output — exactly the path the user must
    # clean up by hand — and it really did leak (rmtree could not remove it).
    leaked = [p for p in tmp_path.iterdir() if p.name.startswith(".shipit-repo-new-")]
    assert leaked
    assert leaked[0].name in err
    assert not (tmp_path / "hello").exists()


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


# --- verb: the rejected request matrix through the public command (WS02) ------
#
# Every usage-shaped rejection — an unresolvable stack selection, an invalid or
# reserved name, or an unusable parent/destination — is raised in the module's
# preflight BEFORE any effect seam runs (resolve_profiles → validate_name →
# _preflight, all ahead of the staging mkdtemp). So the REAL `create_repo` can
# drive these tests with no pixi/Rust toolchain: the observable outcome is exit
# 1, an `error:` line, and a parent directory left exactly as it was found.


def _reject_via_command(tmp_path, capsys, *, stacks, name, seed=None):
    """Run the real ``repo new`` command for a request expected to be refused.

    ``seed`` optionally prepares destination content (``{relpath: kind}`` where
    kind is ``"dir"``, ``"file"``, or a symlink target ``Path``). Returns the
    exit code and captured stderr and asserts the parent's top-level entries are
    unchanged by the refused run — no new destination, no leaked staging
    sibling.
    """
    from shipit.verbs import repo as repo_verb

    if seed:
        for rel, kind in seed.items():
            target = tmp_path / rel
            if kind == "dir":
                target.mkdir()
            elif kind == "file":
                target.write_text("keep", encoding="utf-8")
            else:
                target.symlink_to(kind)
    before = sorted(p.name for p in tmp_path.iterdir())

    rc = repo_verb.run_new(stacks=stacks, name=name, parent=tmp_path)
    err = capsys.readouterr().err

    assert rc == 1
    assert err.startswith("error:")
    # The refusal touched nothing: same entries, no `.shipit-repo-new-*` sibling.
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    return rc, err


def test_command_refuses_missing_stack(tmp_path, capsys):
    _reject_via_command(tmp_path, capsys, stacks=(), name="hello")


def test_command_refuses_unknown_stack(tmp_path, capsys):
    _reject_via_command(tmp_path, capsys, stacks=("go",), name="hello")


def test_command_refuses_duplicate_stack(tmp_path, capsys):
    _reject_via_command(tmp_path, capsys, stacks=("rust", "rust"), name="hello")


@pytest.mark.parametrize("bad", ["Hello", "my_tool", "1abc", "a--b", "a b"])
def test_command_refuses_invalid_name(tmp_path, capsys, bad):
    _reject_via_command(tmp_path, capsys, stacks=("rust",), name=bad)


@pytest.mark.parametrize("reserved", ["test", "fn", "build", "con"])
def test_command_refuses_cargo_reserved_name(tmp_path, capsys, reserved):
    _reject_via_command(tmp_path, capsys, stacks=("rust",), name=reserved)


def test_command_refuses_missing_parent(tmp_path, capsys):
    from shipit.verbs import repo as repo_verb

    missing = tmp_path / "nope"
    rc = repo_verb.run_new(stacks=("rust",), name="hello", parent=missing)
    assert rc == 1
    assert capsys.readouterr().err.startswith("error:")
    # A missing parent is never created to hold the refused Repo.
    assert not missing.exists()


def test_command_refuses_non_empty_destination(tmp_path, capsys):
    # A destination directory holding even one entry is refused and its content
    # is preserved intact.
    (tmp_path / "hello").mkdir()
    _reject_via_command(
        tmp_path, capsys, stacks=("rust",), name="hello", seed={"hello/keep": "file"}
    )
    assert (tmp_path / "hello" / "keep").read_text(encoding="utf-8") == "keep"


def test_command_refuses_file_destination(tmp_path, capsys):
    _reject_via_command(
        tmp_path, capsys, stacks=("rust",), name="hello", seed={"hello": "file"}
    )
    assert (tmp_path / "hello").is_file()


def test_command_refuses_symlink_destination(tmp_path, capsys):
    target = tmp_path / "elsewhere"
    target.mkdir()
    _reject_via_command(
        tmp_path, capsys, stacks=("rust",), name="hello", seed={"hello": target}
    )
    assert (tmp_path / "hello").is_symlink()


# --------------------------------------------------------------------------
# isolated user-shell certification (GEN01-WS06)
#
# The certification seam certifies the staged Repo through the SAME public pixi
# task interface a contributor uses, from a child rooted in the staged Repo,
# with inherited pixi project-selection state scrubbed so it cannot redirect
# resolution back to the invoking checkout (ADR-0062, spec §Proposed Shape).
# These drive `default_provisioner`/`default_verifier` through a captured Exec
# runner: the seam builds the real argv/env, so the assertions prove manifest,
# environment, tasks, and lockfile selection WITHOUT a pixi/Rust toolchain.
# --------------------------------------------------------------------------


#: A parent dev session's leaked pixi/Conda activation state. A naive child would
#: resolve THIS (the invoking checkout's manifest + env), redirecting the
#: certification away from the staged Repo — exactly what the scrub must prevent.
_LEAKED_PARENT_STATE = {
    "PIXI_PROJECT_MANIFEST": "/parent/pixi.toml",
    "PIXI_PROJECT_ROOT": "/parent",
    "PIXI_ENVIRONMENT_NAME": "default",
    "CONDA_PREFIX": "/parent/.pixi/envs/default",
    "CONDA_DEFAULT_ENV": "parent-env",
    "CONDA_SHLVL": "2",
}


def _capture_exec(monkeypatch):
    """Patch the shared Exec seam with a recorder that reports every call OK.

    ``pixienv.run_task``/``install`` resolve ``execrun.run`` at call time, so
    patching the module attribute intercepts the child the certification would
    spawn. Returns the list every ``(argv, kwargs)`` lands in.
    """
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return execrun.ExecResult(
            argv=tuple(argv), rc=0, stdout="", stderr="", duration_ms=1
        )

    monkeypatch.setattr(execrun, "run", fake_run)
    return calls


def _seed_leaked_parent_state(monkeypatch):
    """Seed the leaked parent activation vars plus a PATH that MUST survive."""
    monkeypatch.setenv("PATH", "/usr/bin")
    for var, val in _LEAKED_PARENT_STATE.items():
        monkeypatch.setenv(var, val)


def test_default_verifier_scrubs_parent_state_and_selects_staged_tasks(
    tmp_path, monkeypatch
):
    # Conflicting parent pixi/Conda activation is seeded; certification must run
    # the three public tasks against the STAGED manifest under a scrubbed env.
    _seed_leaked_parent_state(monkeypatch)
    calls = _capture_exec(monkeypatch)

    create_mod.default_verifier(tmp_path)

    manifest = str(tmp_path / "pixi.toml")
    # Exactly the three canonical public TASKS, in order, each addressed at the
    # staged manifest (`pixi run --manifest-path <staged>/pixi.toml <task>`) — the
    # user's public interface, not a Tool implementation call.
    assert [argv[-1] for argv, _ in calls] == ["lint", "test", "build"]
    for argv, kwargs in calls:
        assert argv[:4] == ["pixi", "run", "--manifest-path", manifest]
        # The child is rooted in the staged Repo (AC: working directory is the
        # staged Repo), reads the rc as a verdict (check=False), and carries the
        # long-runner bound so a first-activation re-solve is not killed.
        assert kwargs["cwd"] == str(tmp_path)
        assert kwargs["check"] is False
        assert kwargs["timeout"] == create_mod._LONG_TIMEOUT
        # The child env is the COMPLETE scrubbed snapshot (replace_env), so no
        # leaked parent project pointer can creep back via a merge over os.environ.
        assert kwargs["replace_env"] is True
        env = kwargs["env"]
        for leaked in _LEAKED_PARENT_STATE:
            assert leaked not in env, f"{leaked} leaked into the certification child"
        assert env["PATH"] == "/usr/bin"  # user-level vars survive the scrub


def test_default_provisioner_scrubs_parent_state_and_locks_staged_env(
    tmp_path, monkeypatch
):
    # Provisioning resolves + locks the STAGED Repo's environment; the same
    # leaked parent state must not bind the solve to the invoking checkout, and
    # `pixi install` runs IN the staged Repo so it writes that Repo's own lockfile.
    _seed_leaked_parent_state(monkeypatch)
    calls = _capture_exec(monkeypatch)

    create_mod.default_provisioner(tmp_path)

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == ["pixi", "install"]
    assert kwargs["cwd"] == str(tmp_path)  # writes <staged>/pixi.lock, not parent's
    assert kwargs["replace_env"] is True
    assert kwargs["timeout"] == pixienv.INSTALL_TIMEOUT
    for leaked in _LEAKED_PARENT_STATE:
        assert leaked not in kwargs["env"]
    assert kwargs["env"]["PATH"] == "/usr/bin"


def test_default_verifier_retains_failing_check_output_and_stops(tmp_path, monkeypatch):
    # A mocked verifier could conceal a missing generated dependency; the real
    # staged Check surfaces the failing command's OWN diagnostics (AC7). Here the
    # `test` task fails as a missing `cargo-nextest` would, and lint has already
    # passed — proving the failure carries creation-stage context + the tool's
    # output and aborts BEFORE build, the initial commit, and publication.
    diagnostic = "error: no such command: `nextest`"
    seen: list[str] = []

    def fake_run(argv, **kwargs):
        task = argv[-1]
        seen.append(task)
        if task == "test":
            return execrun.ExecResult(
                argv=tuple(argv),
                rc=101,
                stdout="Compiling libhello v0.1.0\n",
                stderr=diagnostic + "\n",
                duration_ms=1,
            )
        return execrun.ExecResult(
            argv=tuple(argv), rc=0, stdout="", stderr="", duration_ms=1
        )

    monkeypatch.setattr(execrun, "run", fake_run)

    with pytest.raises(CreationError) as excinfo:
        create_mod.default_verifier(tmp_path)

    message = str(excinfo.value)
    # Creation-stage context + the failing command + its rc.
    assert "staged Check `pixi run test` failed (rc=101)" in message
    assert "the Repo was not published" in message
    # The failing command's OWN output is retained (both streams' tails).
    assert diagnostic in message
    assert "Compiling libhello" in message
    # Stopped at the first failure: build never ran.
    assert seen == ["lint", "test"]


def test_default_verifier_redacts_secrets_in_failing_check_output(
    tmp_path, monkeypatch
):
    # The staged Check inherits the caller's scrubbed-but-still-secret-bearing
    # environment, so a failing tool that echoes a registered secret must NOT
    # leak it onto the CreationError / `error:` CLI surface. The retained tails
    # are masked with the SAME redactor an ExecError applies to its own streams,
    # over BOTH stdout and stderr.
    stdout_secret = "tok_stdout_5up3rs3cr3t"
    stderr_secret = "tok_stderr_p3mk3yl34k"

    def fake_run(argv, **kwargs):
        task = argv[-1]
        if task == "test":
            return execrun.ExecResult(
                argv=tuple(argv),
                rc=101,
                stdout=f"dumping env: {stdout_secret}\n",
                stderr=f"leaked PEM: {stderr_secret}\n",
                duration_ms=1,
            )
        return execrun.ExecResult(
            argv=tuple(argv), rc=0, stdout="", stderr="", duration_ms=1
        )

    monkeypatch.setattr(execrun, "run", fake_run)

    redact.register_secret(stdout_secret)
    redact.register_secret(stderr_secret)
    try:
        with pytest.raises(CreationError) as excinfo:
            create_mod.default_verifier(tmp_path)
    finally:
        redact.clear_registered_secrets()

    message = str(excinfo.value)
    assert stdout_secret not in message
    assert stderr_secret not in message
    assert redact.MASK in message
    # The surrounding, non-secret diagnostics still surface for the reader.
    assert "dumping env:" in message
    assert "leaked PEM:" in message


def test_failing_check_redacts_secret_straddling_tail_boundary(tmp_path, monkeypatch):
    # Redaction runs on the WHOLE stream before the tail is sliced, so a secret
    # that crosses the TAIL_CHARS boundary is masked intact. If the tail were
    # sliced FIRST, only a suffix fragment of the secret would remain in the
    # bounded window — the exact-value matcher could no longer recognize it, and
    # that fragment would leak onto the CreationError / `error:` CLI surface.
    secret = "tok_boundary_str4ddl3_s3cr3t_v4lue"
    # Place the secret so the TAIL_CHARS window opens in its MIDDLE: a lead pushes
    # the secret's prefix out of the window, and trailing padding sized so the
    # last TAIL_CHARS begin exactly at secret[split] keeps only its suffix.
    split = len(secret) // 2
    trailing = "y" * (execrun.TAIL_CHARS - (len(secret) - split))
    stdout = f"dumping env: {secret}{trailing}"
    # Sanity: slicing the raw stream to the tail keeps only a suffix fragment of
    # the secret, proving this input genuinely straddles the boundary.
    tail_fragment = stdout[-execrun.TAIL_CHARS :]
    assert secret not in tail_fragment
    assert secret[split:] in tail_fragment

    def fake_run(argv, **kwargs):
        task = argv[-1]
        if task == "test":
            return execrun.ExecResult(
                argv=tuple(argv), rc=101, stdout=stdout, stderr="", duration_ms=1
            )
        return execrun.ExecResult(
            argv=tuple(argv), rc=0, stdout="", stderr="", duration_ms=1
        )

    monkeypatch.setattr(execrun, "run", fake_run)

    redact.register_secret(secret)
    try:
        with pytest.raises(CreationError) as excinfo:
            create_mod.default_verifier(tmp_path)
    finally:
        redact.clear_registered_secrets()

    message = str(excinfo.value)
    # No fragment of the secret survives — not the whole value, not the suffix
    # that fell inside the tail window.
    assert secret not in message
    assert secret[split:] not in message
    assert redact.MASK in message


def test_create_failed_check_leaves_no_repo_and_reports_output(tmp_path, git_identity):
    # End to end through the orchestrator: a failing verifier (carrying the
    # certification's real message) rolls back and publishes nothing, so a hidden
    # missing dependency can never reach a committed, published Repo.
    def failing_verifier(root: Path) -> None:
        raise CreationError(
            "staged Check `pixi run test` failed (rc=101); the Repo was not "
            "published\nstderr:\nerror: no such command: `nextest`"
        )

    with pytest.raises(CreationError, match="was not published"):
        _fake_create(tmp_path, [], verifier=failing_verifier)
    assert not (tmp_path / "hello").exists()
    assert list(tmp_path.iterdir()) == []


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
    # The committed lockfile provisions Rust AND cargo-nextest (AC: no ambient
    # Cargo/rustup/`cargo install` is required) — the generated test task fails
    # with "no such command: nextest" if the dependency is not locked in.
    assert (dest / "pixi.lock").is_file()
    assert "cargo-nextest" in (dest / "pixi.lock").read_text(encoding="utf-8")
    assert git.current_branch(cwd=str(dest)) == "main"
    # Re-run every public Check against the published Repo through the SAME
    # public interface a contributor uses — the full certification contract the
    # module documents (lint, test, build). `test` executes the generated
    # black-box CLI test (CLI-to-library wiring) and `build` compiles the primary
    # product (AC6), on a FRESH environment materialized from the committed
    # lockfile — so mocked command execution cannot conceal a missing dependency.
    # Scrub the inherited `PIXI_PROJECT_*` pointers before re-running: this test
    # itself runs under `pixi run test`, which injects manifest/env-root vars that
    # bind `pixi run` to the PARENT manifest and would override `cwd`, resolving
    # the parent Repo's tasks instead of the generated one (the exact leak class
    # `pixienv.scrub_env` guards — matching how `default_verifier` certifies).
    scrubbed = pixienv.scrub_env(dict(os.environ))
    for task in ("lint", "test", "build"):
        run = subprocess.run(
            ["pixi", "run", task],
            cwd=str(dest),
            capture_output=True,
            text=True,
            env=scrubbed,
        )
        assert run.returncode == 0, f"pixi run {task} failed:\n{run.stderr}"
    # Fresh-environment acceptance leaves the Git tree clean (AC5): running the
    # three public Checks writes build/environment output (target/, .pixi/) that
    # the generated .gitignore must keep out of the index, so certification does
    # not dirty the committed Repo.
    assert git.status_porcelain(cwd=str(dest)) == []
