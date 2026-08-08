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


@pytest.mark.parametrize(
    "name",
    [
        "a",
        "a1",
        "x9y",
        "a-b-c",
        "web-app-2",
        "testing",
        "builder",
        "fnord",
        "console",
        "async-worker",
    ],
)
def test_validate_name_accepts_grammar_edges(name):
    assert validate_name(name).value == name


@pytest.mark.parametrize(
    "reserved",
    [
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
        "test",
        "build",
        "deps",
        "examples",
        "incremental",
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
    with pytest.raises(CreationError, match="invalid project name"):
        validate_name(reserved)


def test_validate_name_reserved_message_names_the_offender():
    with pytest.raises(CreationError, match=r"'fn'"):
        validate_name("fn")


@pytest.mark.parametrize(
    "identifier",
    ["fn", "test", "build", "deps", "examples", "incremental", "con", "lpt3"],
)
def test_reject_if_reserved_covers_every_reservation_class(identifier):
    from shipit.repocreate.names import _reject_if_reserved

    with pytest.raises(CreationError):
        _reject_if_reserved(identifier, "the project name")


def test_reject_if_reserved_passes_a_safe_identifier():
    from shipit.repocreate.names import _reject_if_reserved

    _reject_if_reserved("libhello", "the derived library package")


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
    assert tomlio.dumps({"t": {"k": "a\nb\tc\x7f"}}) == '[t]\nk = "a\\nb\\tc\\u007f"\n'


def test_tomlio_rejects_unserializable_value():
    with pytest.raises(TypeError):
        tomlio.dumps({"t": {"k": object()}})


def test_render_text_substitutes_known_placeholders():
    assert render_text("hi {{ name }}", {"name": "x"}) == "hi x"


def test_render_text_raises_on_undefined_variable():
    with pytest.raises(CreationError):
        render_text("hi {{ missing }}", {"name": "x"})


def test_render_text_fails_closed_on_malformed_placeholder():
    with pytest.raises(CreationError):
        render_text("pkg {{ cli-pkg }}", {"cli-pkg": "x"})


def test_render_text_allows_brace_pairs_in_context_values():
    out = render_text("body {{ snippet }}", {"snippet": "let x = vec![{{1}}];"})
    assert out == "body let x = vec![{{1}}];"


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
    assert "Cargo.lock" not in text and "pixi.lock" not in text


def test_plan_pixi_manifest_declares_build_task_and_nextest():
    text = {f.path: f.text for f in _plan().files}["pixi.toml"]
    assert 'build = "./bin/shipit build"' in text
    assert "cargo-nextest" in text
    assert 'test = "./bin/shipit test"' not in text
    assert 'lint = "./bin/shipit lint"' not in text
    assert "lint-full" not in text
    assert "[feature.lint.tasks]" not in text


def _shipit_toml(**kw):
    text = {f.path: f.text for f in _plan(**kw).files}[".shipit.toml"]
    return tomllib.loads(text)


def test_plan_shipit_manifest_declares_one_cli_artifact_with_rust_build_target():
    artifacts = _shipit_toml()["artifacts"]
    assert list(artifacts) == ["hello"]
    assert artifacts["hello"]["build"] == [{"toolchain": "rust", "package": "hello"}]


def test_plan_artifact_carries_no_endpoint_bundle_signing_or_release_policy():
    cfg = _shipit_toml()
    artifact = cfg["artifacts"]["hello"]
    assert set(artifact) == {"build"}
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
    lanes = _shipit_toml()["lanes"]
    assert list(lanes) == ["lint", "test"]
    assert "build" not in lanes
    assert lanes["lint"]["run"] == "lint"
    assert lanes["test"]["run"] == "test"
    for name in ("lint", "test"):
        assert lanes[name]["required"] is True
        assert lanes[name]["local"] is True


def test_generated_lanes_parse_and_derive_lint_test_commit_push_checks():
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
    from shipit import config
    from shipit.install import splice
    from shipit.install import units as iunits
    from shipit.tools import lanes as lane_planner

    scaffold = {f.path: f.text for f in _plan().files}["pixi.toml"]
    installed = splice.splice_block(
        scaffold,
        iunits.data_bytes("pixi-lint-task-block.toml").decode("utf-8").strip("\n"),
        iunits.PIXI_LINT_TASK_OPEN,
        iunits.PIXI_LINT_TASK_CLOSE,
        anchor=iunits.PIXI_LINT_TASKS_ANCHOR,
    )
    installed += "\n[environments]\n" + iunits.data_bytes(
        "pixi-lint-env-block.toml"
    ).decode("utf-8")
    pixi = tomllib.loads(installed)
    task_envs = lane_planner.task_env_sets(pixi)
    assert task_envs["lint"] == ("lint",)

    parsed = config.load_lanes(_shipit_toml())
    jobs = lane_planner.plan(parsed, event="pr", task_envs=task_envs)
    by_name = {job.name: job for job in jobs}
    assert by_name["lint"].envs == ("lint",)
    assert by_name["test"].envs == ("default",)


def test_plan_ci_caller_is_valid_yaml_delegating_to_reusable_checks():
    import yaml

    text = {f.path: f.text for f in _plan().files}[".github/workflows/ci.yml"]
    doc = yaml.safe_load(text)
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


@pytest.fixture
def git_identity(monkeypatch):
    for var, val in {
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test Author",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }.items():
        monkeypatch.setenv(var, val)


class _Recorder:
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
    assert (dest / "Cargo.toml").is_file()
    assert (dest / "crates/hello/src/main.rs").is_file()
    assert (dest / "MANAGED.md").is_file()
    assert (dest / "pixi.lock").is_file()
    assert git.current_branch(cwd=str(dest)) == "main"
    assert git.head_commit(cwd=str(dest)).value == result.initial_commit
    subjects = subprocess.run(
        ["git", "-C", str(dest), "log", "--pretty=%s"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert subjects == ["Initial commit"]
    assert git.status_porcelain(cwd=str(dest)) == []
    assert order == ["install", "provision", "verify"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["hello"]


def test_create_publishes_only_the_committed_relocatable_tree(tmp_path, git_identity):
    order: list[str] = []
    captured: dict[str, Path] = {}

    def verify_writing_staging_path_artifacts(root: Path) -> None:
        order.append("verify")
        captured["staging"] = root
        for rel in ("target/debug/hello", ".pixi/envs/default/bin/hello"):
            artifact = root / rel
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(str(root), encoding="utf-8")

    result = _fake_create(
        tmp_path, order, verifier=verify_writing_staging_path_artifacts
    )
    dest = result.destination
    staging = captured["staging"]

    assert not (dest / "target").exists()
    assert not (dest / ".pixi").exists()
    for path in dest.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            body = path.read_text(encoding="utf-8", errors="ignore")
            assert str(staging) not in body, f"{path} still references {staging}"
    assert (dest / "Cargo.toml").is_file()
    assert (dest / "crates/hello/src/main.rs").is_file()
    assert (dest / "pixi.lock").is_file()
    assert git.head_commit(cwd=str(dest)).value == result.initial_commit
    assert git.status_porcelain(cwd=str(dest)) == []


def test_create_relocates_baked_hook_paths_out_of_staging(tmp_path, git_identity):
    order: list[str] = []
    captured: dict[str, Path] = {}

    def install_baking_staging_hook(root: Path) -> None:
        order.append("install")
        captured["staging"] = root
        hooks = root / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        (hooks / "pre-commit").write_text(
            f'call_lefthook() {{ "{root}/.pixi/envs/lint/bin/lefthook" "$@"; }}\n',
            encoding="utf-8",
        )
        (hooks / "pre-push").write_text(
            f'call_lefthook() {{ "{root}/.pixi/envs/lint/bin/lefthook" "$@"; }}\n',
            encoding="utf-8",
        )
        (hooks / "pre-rebase.sample").write_text(
            "#!/bin/sh\nexit 0\n", encoding="utf-8"
        )

    result = _fake_create(tmp_path, order, installer=install_baking_staging_hook)
    dest = result.destination
    staging = captured["staging"]

    for slot in ("pre-commit", "pre-push"):
        body = (dest / ".git" / "hooks" / slot).read_text(encoding="utf-8")
        assert str(staging) not in body
        assert f"{dest}/.pixi/envs/lint/bin/lefthook" in body
    assert (dest / ".git" / "hooks" / "pre-rebase.sample").read_text(
        encoding="utf-8"
    ) == "#!/bin/sh\nexit 0\n"


def test_create_relocates_hook_paths_to_absolute_under_relative_parent(
    tmp_path, monkeypatch, git_identity
):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "sub").mkdir()
    monkeypatch.chdir(workdir)

    def install_baking_absolute_staging_hook(root: Path) -> None:
        abs_staging = Path.cwd() / root
        hooks = root / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        (hooks / "pre-commit").write_text(
            f'call_lefthook() {{ "{abs_staging}/.pixi/envs/lint/bin/lefthook" "$@"; }}\n',
            encoding="utf-8",
        )

    order: list[str] = []
    result = create_repo(
        "hello",
        Path("sub"),
        ("rust",),
        installer=install_baking_absolute_staging_hook,
        provisioner=_Recorder(order, "provision", writes="pixi.lock"),
        verifier=_Recorder(order, "verify"),
        author_reader=lambda root: "Test Author",
        year=2026,
    )

    assert result.destination == Path("sub") / "hello"
    abs_dest = workdir / "sub" / "hello"
    body = (result.destination / ".git" / "hooks" / "pre-commit").read_text(
        encoding="utf-8"
    )
    assert f"{abs_dest}/.pixi/envs/lint/bin/lefthook" in body
    assert ".shipit-repo-new-" not in body


def test_create_accepts_empty_destination_directory(tmp_path, git_identity):
    (tmp_path / "hello").mkdir()
    result = _fake_create(tmp_path, [])
    assert result.destination == tmp_path / "hello"
    assert (tmp_path / "hello" / "Cargo.toml").is_file()


def test_create_published_repo_respects_umask(tmp_path, git_identity):
    old = os.umask(0o022)
    try:
        _fake_create(tmp_path, [])
    finally:
        os.umask(old)
    assert (tmp_path / "hello").stat().st_mode & 0o777 == 0o755


def test_create_cleans_staging_when_umask_stage_fails(
    tmp_path, git_identity, monkeypatch
):
    def deny(self, mode):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "chmod", deny)
    with pytest.raises(OSError):
        _fake_create(tmp_path, [])
    assert list(tmp_path.iterdir()) == []


def test_create_reports_destination_through_a_symlink_parent(tmp_path, git_identity):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    result = _fake_create(link, [])
    assert result.destination == link / "hello"
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
    monkeypatch.setattr(create_mod.git, "author_name", lambda *, cwd: "Ada Lovelace")
    monkeypatch.setattr(create_mod.git, "committer_name", lambda *, cwd: None)
    with pytest.raises(CreationError):
        create_mod.default_author(tmp_path)


def test_default_author_returns_name_when_both_resolve(tmp_path, monkeypatch):
    monkeypatch.setattr(create_mod.git, "author_name", lambda *, cwd: "Ada Lovelace")
    monkeypatch.setattr(create_mod.git, "committer_name", lambda *, cwd: "Ada Lovelace")
    assert create_mod.default_author(tmp_path) == "Ada Lovelace"


def _stub_install_pipeline(monkeypatch, *, hooks_activated, hooks_detail=""):
    from shipit.install import apply as apply_mod
    from shipit.install import reconcile as reconcile_mod
    from shipit.install import units as units_mod

    monkeypatch.setattr(reconcile_mod, "detect_toolchains", lambda root: frozenset())
    monkeypatch.setattr(units_mod, "load_units", lambda **kwargs: ())
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
    _stub_install_pipeline(
        monkeypatch, hooks_activated=False, hooks_detail="lefthook not found"
    )
    with pytest.raises(CreationError, match="hooks did not activate"):
        create_mod.default_installer(tmp_path)


@pytest.mark.parametrize("hooks_activated", [True, None])
def test_default_installer_accepts_activated_or_no_op_hooks(
    tmp_path, monkeypatch, hooks_activated
):
    _stub_install_pipeline(monkeypatch, hooks_activated=hooks_activated)
    create_mod.default_installer(tmp_path)


def test_default_installer_delivers_the_endpoint_gated_conda_packager(
    tmp_path, monkeypatch
):
    from shipit import config
    from shipit.install import apply as apply_mod
    from shipit.install import units as units_mod

    monkeypatch.setattr(
        apply_mod,
        "_activate_hooks",
        lambda root: execrun.ExecResult(
            argv=("lefthook", "install"), rc=0, stdout="", stderr="", duration_ms=1
        ),
    )
    (tmp_path / "grammar.js").write_text("module.exports = grammar({});\n")
    (tmp_path / ".shipit.toml").write_text(
        "[toolchains]\n"
        '"." = "tree-sitter"\n'
        "[artifacts.grammar]\n"
        'build = ["tree-sitter"]\n'
        'bundle = { composition = "tarball", leg = "tree-sitter", payload = [{ path = "src", required = true }] }\n'
        'endpoints = ["gh-release", "conda"]\n'
    )

    create_mod.default_installer(tmp_path)

    text = (tmp_path / "pixi.toml").read_text(encoding="utf-8")
    assert units_mod.PIXI_CONDA_PACKAGER_OPEN in text
    managed = config.load_managed(config.load(tmp_path / ".shipit.toml"))
    assert units_mod.PIXI_CONDA_PACKAGER_KEY in managed


@pytest.mark.parametrize(
    "kwarg,label", [("installer", "install"), ("provisioner", "provision")]
)
def test_create_failed_stage_rolls_back_and_publishes_nothing(
    tmp_path, git_identity, kwarg, label
):
    order: list[str] = []
    with pytest.raises(CreationError):
        _fake_create(
            tmp_path,
            order,
            **{kwarg: _Recorder(order, label, raises=CreationError(f"{label} failed"))},
        )
    assert not (tmp_path / "hello").exists()
    assert list(tmp_path.iterdir()) == []


def test_create_commit_failure_preserves_git_policy_and_prevents_publication(
    tmp_path, git_identity, monkeypatch
):
    from shipit.execrun import ExecError

    def failing_commit(message, *, cwd, no_verify=False):
        assert no_verify is False
        raise ExecError(
            ["git", "commit", "-m", message],
            rc=128,
            stderr="error: gpg failed to sign the data",
        )

    monkeypatch.setattr(create_mod.git, "commit_all", failing_commit)
    with pytest.raises(ExecError) as exc:
        _fake_create(tmp_path, [])
    assert "git commit" in str(exc.value)
    assert not (tmp_path / "hello").exists()
    assert list(tmp_path.iterdir()) == []


def test_create_failure_leaves_pre_existing_empty_destination_empty(
    tmp_path, git_identity
):
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
    assert list(dest.iterdir()) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["hello"]


def test_create_refuses_destination_that_appeared_during_creation(
    tmp_path, git_identity
):
    dest = tmp_path / "hello"
    order: list[str] = []

    def racing_verify(root: Path) -> None:
        order.append("verify")
        dest.mkdir()
        (dest / "other").write_text("concurrent", encoding="utf-8")

    with pytest.raises(CreationError):
        _fake_create(tmp_path, order, verifier=racing_verify)
    assert (dest / "other").read_text(encoding="utf-8") == "concurrent"
    assert not (dest / "Cargo.toml").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["hello"]


def test_create_cleanup_failure_is_reported_alongside_primary_and_never_publishes(
    tmp_path, git_identity, monkeypatch
):
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
    assert not (tmp_path / "hello").exists()
    notes = getattr(exc.value, "__notes__", [])
    assert any("could not be removed" in note for note in notes)
    leaked = [
        p.name for p in tmp_path.iterdir() if p.name.startswith(".shipit-repo-new-")
    ]
    assert leaked


def _run_new_with_seams(monkeypatch, tmp_path, capsys, *, verifier):
    from shipit.verbs import repo as repo_verb

    order: list[str] = []

    def wired(name, parent, stacks, **kwargs):
        return create_repo(
            name,
            parent,
            stacks,
            **kwargs,
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
    assert "pixi run lint" in err
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
    assert (dest / "other").read_text(encoding="utf-8") == "concurrent"
    assert not (dest / "Cargo.toml").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["hello"]


def test_command_reports_cleanup_failure_alongside_primary_on_stderr(
    tmp_path, capsys, git_identity, monkeypatch
):
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
    assert "lint failed" in err
    assert "could not be removed" in err
    leaked = [p for p in tmp_path.iterdir() if p.name.startswith(".shipit-repo-new-")]
    assert leaked
    assert leaked[0].name in err
    assert not (tmp_path / "hello").exists()


def _plan_files(name="hello", author="Ada Lovelace", year=2026):
    return {f.path: f.text for f in _plan(name=name, author=author, year=year).files}


def test_readme_names_project_lists_commands_without_badges_or_urls():
    readme = _plan_files(name="my-tool")["README.md"]
    assert "# my-tool" in readme
    for command in ("pixi run lint", "pixi run test", "pixi run build"):
        assert command in readme
    assert "![" not in readme
    assert "shields.io" not in readme
    assert "badge" not in readme.lower()
    assert "github.com" not in readme


def test_license_is_canonical_mit_with_attribution_and_no_placeholder():
    license_text = _plan_files(author="Grace Hopper", year=1999)["LICENSE"]
    assert license_text.startswith("MIT License")
    assert "Permission is hereby granted, free of charge" in license_text
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in license_text
    assert "Copyright (c) 1999 Grace Hopper" in license_text
    assert "{{" not in license_text and "}}" not in license_text
    for alt in ("Apache", "GPL", "BSD", "choose", "SPDX"):
        assert alt not in license_text


def test_cargo_metadata_declares_versions_and_is_inherited_by_members():
    files = _plan_files(name="my-tool")
    workspace = tomllib.loads(files["Cargo.toml"])
    assert workspace["workspace"]["resolver"] == "3"
    package = workspace["workspace"]["package"]
    assert package == {"version": "0.1.0", "edition": "2024", "license": "MIT"}
    for member in ("crates/my-tool/Cargo.toml", "crates/libmy-tool/Cargo.toml"):
        inherited = tomllib.loads(files[member])["package"]
        assert inherited["version"] == {"workspace": True}
        assert inherited["edition"] == {"workspace": True}
        assert inherited["license"] == {"workspace": True}


def _stage_repo_with_representative_tree(root: Path) -> None:
    git.init_main(cwd=str(root))
    for path, text in _plan_files().items():
        dest = root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")

    def touch(rel: str) -> None:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")

    for ignored in (
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
        touch(ignored)

    for tracked in (
        "Cargo.lock",
        "pixi.lock",
        ".claude/agents/implementer.md",
        ".env.example",
        "dist/hello",
    ):
        touch(tracked)


def test_git_add_ignores_generated_output_and_tracks_source(tmp_path):
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
        "dist/hello",
    ):
        assert path in tracked, f"{path} should be tracked but was ignored"


def test_git_check_ignore_keeps_managed_config_and_honors_env_negation(tmp_path):
    git.init_main(cwd=str(tmp_path))
    (tmp_path / ".gitignore").write_text(_plan_files()[".gitignore"], encoding="utf-8")

    def ignored(rel: str) -> bool:
        return (
            subprocess.run(
                ["git", "-C", str(tmp_path), "check-ignore", "-q", rel]
            ).returncode
            == 0
        )

    assert ignored(".claude/worktrees/w/scratch")
    assert not ignored(".claude/agents/implementer.md")
    assert not ignored(".claude/skills/coordinating.md")
    assert ignored(".env")
    assert ignored(".env.local")
    assert not ignored(".env.example")
    assert not ignored("dist/hello")


def test_run_new_renders_destination_and_commit(monkeypatch, capsys, tmp_path):
    from shipit.repocreate import CreationResult
    from shipit.verbs import repo as repo_verb

    monkeypatch.setattr(
        repo_verb,
        "create_repo",
        lambda name, parent, stacks, **kwargs: CreationResult(
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

    rc = repo_verb.run_new(stacks=("go",), name="hello", parent=tmp_path)
    assert rc == 1
    assert capsys.readouterr().err.startswith("error:")


def _reject_via_command(tmp_path, capsys, *, stacks, name, seed=None):
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
    assert not missing.exists()


def test_command_refuses_non_empty_destination(tmp_path, capsys):
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


_LEAKED_PARENT_STATE = {
    "PIXI_PROJECT_MANIFEST": "/parent/pixi.toml",
    "PIXI_PROJECT_ROOT": "/parent",
    "PIXI_ENVIRONMENT_NAME": "default",
    "CONDA_PREFIX": "/parent/.pixi/envs/default",
    "CONDA_DEFAULT_ENV": "parent-env",
    "CONDA_SHLVL": "2",
}


def _capture_exec(monkeypatch):
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return execrun.ExecResult(
            argv=tuple(argv), rc=0, stdout="", stderr="", duration_ms=1
        )

    monkeypatch.setattr(execrun, "run", fake_run)
    return calls


def _seed_leaked_parent_state(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    for var, val in _LEAKED_PARENT_STATE.items():
        monkeypatch.setenv(var, val)


def test_default_verifier_scrubs_parent_state_and_selects_staged_tasks(
    tmp_path, monkeypatch
):
    _seed_leaked_parent_state(monkeypatch)
    calls = _capture_exec(monkeypatch)

    create_mod.default_verifier(tmp_path)

    manifest = str(tmp_path / "pixi.toml")
    assert [argv[-1] for argv, _ in calls] == ["lint", "test", "build"]
    for argv, kwargs in calls:
        assert argv[:4] == ["pixi", "run", "--manifest-path", manifest]
        assert kwargs["cwd"] == str(tmp_path)
        assert kwargs["check"] is False
        assert kwargs["timeout"] == create_mod._LONG_TIMEOUT
        assert kwargs["replace_env"] is True
        env = kwargs["env"]
        for leaked in _LEAKED_PARENT_STATE:
            assert leaked not in env, f"{leaked} leaked into the certification child"
        assert env["PATH"] == "/usr/bin"


def test_default_provisioner_scrubs_parent_state_and_locks_staged_env(
    tmp_path, monkeypatch
):
    _seed_leaked_parent_state(monkeypatch)
    calls = _capture_exec(monkeypatch)

    create_mod.default_provisioner(tmp_path)

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == ["pixi", "install"]
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["replace_env"] is True
    assert kwargs["timeout"] == pixienv.INSTALL_TIMEOUT
    for leaked in _LEAKED_PARENT_STATE:
        assert leaked not in kwargs["env"]
    assert kwargs["env"]["PATH"] == "/usr/bin"


def test_default_verifier_retains_failing_check_output_and_stops(tmp_path, monkeypatch):
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
    assert "staged Check `pixi run test` failed (rc=101)" in message
    assert "the Repo was not published" in message
    assert diagnostic in message
    assert "Compiling libhello" in message
    assert seen == ["lint", "test"]


def test_default_verifier_redacts_secrets_in_failing_check_output(
    tmp_path, monkeypatch
):
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
    assert "dumping env:" in message
    assert "leaked PEM:" in message


def test_failing_check_redacts_secret_straddling_tail_boundary(tmp_path, monkeypatch):
    secret = "tok_boundary_str4ddl3_s3cr3t_v4lue"
    split = len(secret) // 2
    trailing = "y" * (execrun.TAIL_CHARS - (len(secret) - split))
    stdout = f"dumping env: {secret}{trailing}"
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
    assert secret not in message
    assert secret[split:] not in message
    assert redact.MASK in message


def test_create_failed_check_leaves_no_repo_and_reports_output(tmp_path, git_identity):
    def failing_verifier(root: Path) -> None:
        raise CreationError(
            "staged Check `pixi run test` failed (rc=101); the Repo was not "
            "published\nstderr:\nerror: no such command: `nextest`"
        )

    with pytest.raises(CreationError, match="was not published"):
        _fake_create(tmp_path, [], verifier=failing_verifier)
    assert not (tmp_path / "hello").exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(
    not os.environ.get("SHIPIT_REPO_NEW_E2E"),
    reason="set SHIPIT_REPO_NEW_E2E=1 to run the full pixi+cargo certification",
)
def test_create_real_toolchain_end_to_end(tmp_path, git_identity):
    result = create_repo("my-tool", tmp_path, ("rust",))
    dest = result.destination
    assert (dest / "Cargo.toml").is_file()
    assert (dest / "pixi.lock").is_file()
    assert "cargo-nextest" in (dest / "pixi.lock").read_text(encoding="utf-8")
    assert git.current_branch(cwd=str(dest)) == "main"
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
    assert git.status_porcelain(cwd=str(dest)) == []
    for shim in (dest / ".git" / "hooks").iterdir():
        if shim.is_file():
            body = shim.read_text(encoding="utf-8", errors="ignore")
            assert ".shipit-repo-new-" not in body, f"{shim} references staging"
