"""`repo new --standout-wizard`: the Standout wizard as an alternate Rust scaffold producer.

The unit tests drive a FAKE `standout` executable placed on `PATH`, so the seam,
the import, and the Artifact derivation all run for real. One end-to-end test
drives the released wizard itself; it runs only when `SHIPIT_STANDOUT_WIZARD`
names the certified version on `PATH`.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tomllib
from pathlib import Path

import pytest

from shipit import execrun, git
from shipit.repocreate import CreationError, build_plan, create_repo, validate_name
from shipit.repocreate import create as create_mod
from shipit.repocreate import standout as standout_mod
from shipit.repocreate.profiles import (
    ArtifactDecl,
    OwnedFile,
    RustProfile,
    RustScaffold,
    minimal_scaffold,
    require_rust_only,
)

#: The one wizard release this file's fixtures and prompt sequence are verified
#: against. The binary carries no `--version`, so the end-to-end runner asserts the
#: version through `SHIPIT_STANDOUT_WIZARD` rather than interrogating the executable.
CERTIFIED_VERSION = "7.10.1"

_ROOT_MANIFEST = """\
[workspace]
resolver = "2"
members = [
    "crates/{lib}",
    "crates/{exe}",
]
"""

_CLI_MANIFEST = """\
[package]
name = "{exe}"
version = "0.1.0"
edition = "2021"

[dependencies]
standout = "{version}"
{lib} = {{ package = "{lib}", path = "../{lib}" }}

[dev-dependencies]
standout-test = "{version}"
"""

_LIB_MANIFEST = """\
[package]
name = "{lib}"
version = "0.1.0"
edition = "2021"

[dependencies]
thiserror = "2"
"""


def standout_tree(project: str = "hello", exe: str | None = None) -> dict[str, str]:
    """The released wizard's generated file set, verified against `CERTIFIED_VERSION`."""
    exe = exe or project
    lib = f"{project}lib"
    fields = {
        "project": project,
        "exe": exe,
        "lib": lib,
        "version": CERTIFIED_VERSION,
    }
    return {
        "Cargo.toml": _ROOT_MANIFEST.format(**fields),
        f"crates/{exe}/Cargo.toml": _CLI_MANIFEST.format(**fields),
        f"crates/{exe}/README.md": f"# {exe}\n",
        f"crates/{exe}/src/main.rs": "fn main() {}\n#[cfg(test)]\nmod tests {}\n",
        f"crates/{exe}/src/cli.rs": "// cli\n",
        f"crates/{exe}/src/handlers.rs": "// handlers\n",
        f"crates/{exe}/src/templates/process.jinja": "{{ summary }}\n",
        f"crates/{exe}/src/styles/{project}.css": ".row { color: red; }\n",
        f"crates/{lib}/Cargo.toml": _LIB_MANIFEST.format(**fields),
        f"crates/{lib}/src/lib.rs": "pub fn process() {}\n",
    }


def write_tree(root: Path, files: dict[str, str | bytes]) -> Path:
    for rel, content in files.items():
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            dest.write_bytes(content)
        else:
            dest.write_text(content, encoding="utf-8")
    return root


def install_wizard(tmp_path: Path, monkeypatch, body: str) -> Path:
    """Put a fake `standout` on ``PATH``; ``body`` is the shell it runs in the scratch dir."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "standout"
    script.write_text(
        '#!/bin/sh\ntest "$1" = "new-project" || exit 64\n' + body + "\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return bindir


def generating_wizard(
    tmp_path: Path,
    monkeypatch,
    files: dict[str, str | bytes] | None = None,
    *,
    project: str = "hello",
) -> Path:
    """A fake wizard that copies a prepared tree in as ``<scratch>/<project>``."""
    source = write_tree(
        tmp_path / "wizard-source", standout_tree() if files is None else files
    )
    return install_wizard(tmp_path, monkeypatch, f"cp -R '{source}' './{project}'")


@contextlib.contextmanager
def piped_stdin(answers: str):
    """Put ``answers`` on the process's real fd 0, the stream the seam inherits."""
    reader, writer = os.pipe()
    os.write(writer, answers.encode("utf-8"))
    os.close(writer)
    saved = os.dup(0)
    try:
        os.dup2(reader, 0)
        yield
    finally:
        os.dup2(saved, 0)
        os.close(saved)
        os.close(reader)


def produce(tmp_path: Path, name: str = "hello") -> RustScaffold:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    return standout_mod.produce(validate_name(name), scratch)


# --- the wizard seam and the import -----------------------------------------


def test_produce_imports_the_generated_tree_and_derives_the_artifact(
    tmp_path, monkeypatch
):
    generating_wizard(tmp_path, monkeypatch)
    scaffold = produce(tmp_path)

    assert {file.path for file in scaffold.owned_files} == set(standout_tree())
    assert scaffold.artifact == ArtifactDecl(
        name="hello", toolchain="rust", package="hello"
    )


def test_produce_imports_contents_without_a_nested_project_directory(
    tmp_path, monkeypatch
):
    generating_wizard(tmp_path, monkeypatch)
    paths = {file.path for file in produce(tmp_path).owned_files}
    assert not any(path.startswith("hello/") for path in paths)


def test_produce_preserves_the_standout_file_bodies_verbatim(tmp_path, monkeypatch):
    generating_wizard(tmp_path, monkeypatch)
    files = {file.path: file.text for file in produce(tmp_path).owned_files}
    assert files["crates/hello/src/styles/hello.css"] == ".row { color: red; }\n"
    assert 'resolver = "2"' in files["Cargo.toml"]
    assert 'edition = "2021"' in files["crates/hello/Cargo.toml"]
    assert "crates/hellolib/src/lib.rs" in files


def test_produce_derives_the_artifact_from_a_non_default_executable_name(
    tmp_path, monkeypatch
):
    generating_wizard(tmp_path, monkeypatch, standout_tree(exe="mytool"))
    scaffold = produce(tmp_path)

    assert scaffold.artifact == ArtifactDecl(
        name="mytool", toolchain="rust", package="mytool"
    )
    paths = {file.path for file in scaffold.owned_files}
    assert "crates/mytool/src/main.rs" in paths
    assert "crates/hellolib/src/lib.rs" in paths


def test_produce_accepts_an_explicit_bin_table_without_a_main_rs(tmp_path, monkeypatch):
    files = standout_tree()
    del files["crates/hello/src/main.rs"]
    files["crates/hello/Cargo.toml"] += (
        '\n[[bin]]\nname = "hello"\npath = "src/run.rs"\n'
    )
    files["crates/hello/src/run.rs"] = "fn main() {}\n"
    generating_wizard(tmp_path, monkeypatch, files)

    assert produce(tmp_path).artifact.package == "hello"


def test_produce_reports_cancellation_when_the_wizard_writes_nothing(
    tmp_path, monkeypatch
):
    install_wizard(tmp_path, monkeypatch, "exit 0")
    with pytest.raises(CreationError, match="cancelled"):
        produce(tmp_path)


def test_produce_reports_a_failing_wizard(tmp_path, monkeypatch):
    install_wizard(tmp_path, monkeypatch, "exit 1")
    with pytest.raises(CreationError, match="exited 1"):
        produce(tmp_path)


def test_produce_carries_the_generated_file_modes_not_the_caller_access(
    tmp_path, monkeypatch
):
    files = standout_tree()
    files["scripts/release.sh"] = "#!/bin/sh\n"
    files["scripts/group-only.sh"] = "#!/bin/sh\n"
    source = write_tree(tmp_path / "wizard-source", files)
    (source / "scripts/release.sh").chmod(0o755)
    # 0o455 carries an execute bit the OWNER lacks, so `os.access(X_OK)` reads False
    # here while the mode does not: the one portable split between the two predicates.
    (source / "scripts/group-only.sh").chmod(0o455)
    install_wizard(tmp_path, monkeypatch, f"cp -R '{source}' './hello'")

    modes = {file.path: file.executable for file in produce(tmp_path).owned_files}
    assert modes["scripts/release.sh"] is True
    assert modes["scripts/group-only.sh"] is True
    assert modes["Cargo.toml"] is False


def test_produce_resolves_a_relative_path_entry_before_entering_the_scratch_dir(
    tmp_path, monkeypatch
):
    source = write_tree(tmp_path / "wizard-source", standout_tree())
    bindir = install_wizard(tmp_path, monkeypatch, f"cp -R '{source}' './hello'")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join([bindir.name, "/usr/bin", "/bin"]))

    assert produce(tmp_path).artifact.package == "hello"


def test_produce_refuses_without_the_executable_on_path(tmp_path, monkeypatch):
    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(CreationError, match="cargo install standout"):
        produce(tmp_path)


def test_produce_refuses_a_project_name_mismatch(tmp_path, monkeypatch):
    generating_wizard(tmp_path, monkeypatch, project="other")
    with pytest.raises(CreationError, match="does not match the requested Repo name"):
        produce(tmp_path)


def test_produce_refuses_more_than_one_top_level_entry(tmp_path, monkeypatch):
    source = write_tree(tmp_path / "wizard-source", standout_tree())
    install_wizard(tmp_path, monkeypatch, f"cp -R '{source}' './hello'\nmkdir ./stray")
    with pytest.raises(CreationError, match="more than one top-level entry"):
        produce(tmp_path)


def test_produce_refuses_a_top_level_file(tmp_path, monkeypatch):
    install_wizard(tmp_path, monkeypatch, "echo x > ./hello")
    with pytest.raises(CreationError, match="not a directory"):
        produce(tmp_path)


def test_produce_refuses_an_empty_project_directory(tmp_path, monkeypatch):
    install_wizard(tmp_path, monkeypatch, "mkdir ./hello")
    with pytest.raises(CreationError, match="empty project directory"):
        produce(tmp_path)


def test_produce_refuses_a_symlink_entry(tmp_path, monkeypatch):
    generating_wizard(tmp_path, monkeypatch)
    (tmp_path / "wizard-source" / "link.rs").symlink_to("Cargo.toml")
    with pytest.raises(CreationError, match="is a symlink"):
        produce(tmp_path)


def test_produce_refuses_a_symlink_directory_escaping_the_root(tmp_path, monkeypatch):
    generating_wizard(tmp_path, monkeypatch)
    (tmp_path / "wizard-source" / "escape").symlink_to(tmp_path)
    with pytest.raises(CreationError, match="is a symlink"):
        produce(tmp_path)


def test_produce_refuses_non_utf8_content(tmp_path, monkeypatch):
    files: dict[str, str | bytes] = dict(standout_tree())
    files["crates/hello/src/blob.rs"] = b"\xff\xfe\x00binary"
    generating_wizard(tmp_path, monkeypatch, files)
    with pytest.raises(CreationError, match="not valid UTF-8"):
        produce(tmp_path)


def test_produce_refuses_a_git_internal_path(tmp_path, monkeypatch):
    files = dict(standout_tree())
    files[".git/config"] = "[core]\n"
    generating_wizard(tmp_path, monkeypatch, files)
    with pytest.raises(CreationError, match="Git-internal path"):
        produce(tmp_path)


# --- Artifact derivation refusals -------------------------------------------


def test_produce_refuses_a_malformed_root_manifest(tmp_path, monkeypatch):
    files = dict(standout_tree())
    files["Cargo.toml"] = "[workspace\nmembers = ]\n"
    generating_wizard(tmp_path, monkeypatch, files)
    with pytest.raises(CreationError, match="not valid TOML"):
        produce(tmp_path)


def test_produce_refuses_a_root_manifest_without_workspace_members(
    tmp_path, monkeypatch
):
    files = dict(standout_tree())
    files["Cargo.toml"] = '[package]\nname = "hello"\n'
    generating_wizard(tmp_path, monkeypatch, files)
    with pytest.raises(CreationError, match="no `workspace.members`"):
        produce(tmp_path)


def test_produce_refuses_a_missing_member_manifest(tmp_path, monkeypatch):
    files = dict(standout_tree())
    del files["crates/hellolib/Cargo.toml"]
    generating_wizard(tmp_path, monkeypatch, files)
    with pytest.raises(CreationError, match="no crates/hellolib/Cargo.toml"):
        produce(tmp_path)


def test_produce_refuses_a_member_manifest_without_a_package_name(
    tmp_path, monkeypatch
):
    files = dict(standout_tree())
    files["crates/hellolib/Cargo.toml"] = '[package]\nversion = "0.1.0"\n'
    generating_wizard(tmp_path, monkeypatch, files)
    with pytest.raises(CreationError, match="no `package.name`"):
        produce(tmp_path)


def test_produce_refuses_zero_binary_members(tmp_path, monkeypatch):
    files = dict(standout_tree())
    del files["crates/hello/src/main.rs"]
    generating_wizard(tmp_path, monkeypatch, files)
    with pytest.raises(CreationError, match="exactly one binary package"):
        produce(tmp_path)


def test_produce_refuses_several_binary_members(tmp_path, monkeypatch):
    files = dict(standout_tree())
    files["crates/hellolib/src/main.rs"] = "fn main() {}\n"
    generating_wizard(tmp_path, monkeypatch, files)
    with pytest.raises(CreationError, match="found hello, hellolib"):
        produce(tmp_path)


# --- the profile carries a produced scaffold --------------------------------


def test_rust_profile_without_a_scaffold_contributes_the_minimal_workspace():
    name = validate_name("hello")
    contribution = RustProfile().contribute(name)
    expected = minimal_scaffold(name)

    assert contribution.owned_files == expected.owned_files
    assert contribution.artifacts == (expected.artifact,)
    assert "crates/hello/tests/cli.rs" in {f.path for f in contribution.owned_files}


def test_rust_profile_with_a_scaffold_replaces_the_files_and_keeps_the_rest():
    scaffold = RustScaffold(
        owned_files=(OwnedFile("Cargo.toml", "[workspace]\n"),),
        artifact=ArtifactDecl(name="mytool", toolchain="rust", package="mytool"),
    )
    contribution = RustProfile(scaffold=scaffold).contribute(validate_name("hello"))

    assert contribution.owned_files == scaffold.owned_files
    assert contribution.artifacts == (scaffold.artifact,)
    assert contribution.pixi_dependencies == (("cargo-nextest", "*"),)
    assert contribution.gitignore_lines == ("/target/",)


def test_plan_over_a_produced_scaffold_keeps_the_universal_seed(tmp_path, monkeypatch):
    generating_wizard(tmp_path, monkeypatch, standout_tree(exe="mytool"))
    profile = RustProfile(scaffold=produce(tmp_path))
    plan = build_plan(validate_name("hello"), (profile,), author="Ada", year=2026)
    files = {file.path: file.text for file in plan.files}

    assert set(files) >= {"README.md", "LICENSE", ".gitignore", "pixi.toml"}
    assert not any(path.startswith("crates/libhello") for path in files)
    manifest = tomllib.loads(files[".shipit.toml"])
    assert manifest["artifacts"] == {
        "mytool": {"build": [{"toolchain": "rust", "package": "mytool"}]}
    }
    assert "/target/" in files[".gitignore"]
    assert "cargo-nextest" in files["pixi.toml"]


@pytest.mark.parametrize("stacks", [(), ("go",), ("rust", "node"), ("node",)])
def test_require_rust_only_refuses_any_other_selection(stacks):
    with pytest.raises(CreationError, match="requires exactly `--stack rust`"):
        require_rust_only(stacks, "--standout-wizard")


def test_require_rust_only_accepts_the_rust_profile_alone():
    require_rust_only(("rust",), "--standout-wizard")


# --- the orchestrator -------------------------------------------------------


@pytest.fixture
def git_identity(monkeypatch):
    for var, val in {
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test Author",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }.items():
        monkeypatch.setenv(var, val)


def _stage(root: Path) -> None:
    (root / "MANAGED.md").write_text("managed\n", encoding="utf-8")


def _lock(root: Path) -> None:
    (root / "pixi.lock").write_text("locked\n", encoding="utf-8")


def _create(parent: Path, **overrides):
    kwargs = dict(
        standout_wizard=True,
        installer=_stage,
        provisioner=_lock,
        verifier=lambda root: None,
        author_reader=lambda root: "Test Author",
        year=2026,
    )
    kwargs.update(overrides)
    return create_repo("hello", parent, ("rust",), **kwargs)


def test_create_with_the_wizard_publishes_the_generated_workspace(
    tmp_path, monkeypatch, git_identity
):
    generating_wizard(tmp_path, monkeypatch, standout_tree(exe="mytool"))
    dest_parent = tmp_path / "parent"
    dest_parent.mkdir()

    result = _create(dest_parent)
    dest = result.destination

    assert dest == dest_parent / "hello"
    assert not (dest / "hello").exists()
    for rel in (
        "crates/mytool/src/main.rs",
        "crates/mytool/src/cli.rs",
        "crates/mytool/src/handlers.rs",
        "crates/mytool/src/templates/process.jinja",
        "crates/mytool/src/styles/hello.css",
        "crates/mytool/README.md",
        "crates/hellolib/src/lib.rs",
        "README.md",
        "LICENSE",
        "pixi.toml",
        ".shipit.toml",
    ):
        assert (dest / rel).is_file(), rel
    assert not (dest / "crates" / "libhello").exists()
    assert not (dest / "crates" / "hello" / "tests").exists()
    assert git.current_branch(cwd=str(dest)) == "main"
    assert git.status_porcelain(cwd=str(dest)) == []
    assert sorted(p.name for p in dest_parent.iterdir()) == ["hello"]


def test_created_repo_declares_the_generated_executable_and_the_rust_toolchain(
    tmp_path, monkeypatch, git_identity
):
    generating_wizard(tmp_path, monkeypatch, standout_tree(exe="mytool"))
    dest_parent = tmp_path / "parent"
    dest_parent.mkdir()

    from shipit.install.reconcile import detect_toolchains

    dest = _create(dest_parent).destination
    manifest = tomllib.loads((dest / ".shipit.toml").read_text(encoding="utf-8"))
    assert manifest["artifacts"] == {
        "mytool": {"build": [{"toolchain": "rust", "package": "mytool"}]}
    }
    assert "cargo-nextest" in (dest / "pixi.toml").read_text(encoding="utf-8")
    assert "/target/" in (dest / ".gitignore").read_text(encoding="utf-8")
    assert detect_toolchains(dest) == frozenset({"rust"})

    from shipit import config

    assert config.derive_toolchains(dest) == ((".", "rust"),)


def test_create_without_the_wizard_never_runs_a_producer(tmp_path, git_identity):
    def never(name, scratch):
        raise AssertionError("the producer ran on the default path")

    result = create_repo(
        "hello",
        tmp_path,
        ("rust",),
        producer=never,
        installer=_stage,
        provisioner=_lock,
        verifier=lambda root: None,
        author_reader=lambda root: "Test Author",
        year=2026,
    )
    dest = result.destination
    assert (dest / "crates/hello/tests/cli.rs").is_file()
    assert (dest / "crates/libhello/src/lib.rs").is_file()


def test_create_removes_the_producer_scratch_directory_on_success(
    tmp_path, git_identity
):
    seen: dict[str, Path] = {}

    def recording(name, scratch):
        seen["scratch"] = scratch
        assert scratch.is_dir()
        return minimal_scaffold(name)

    _create(tmp_path, producer=recording)
    assert not seen["scratch"].exists()


def test_create_removes_the_producer_scratch_directory_when_it_refuses(
    tmp_path, git_identity
):
    seen: dict[str, Path] = {}

    def refusing(name, scratch):
        seen["scratch"] = scratch
        raise CreationError("the wizard was cancelled")

    with pytest.raises(CreationError, match="cancelled"):
        _create(tmp_path, producer=refusing)
    assert not seen["scratch"].exists()
    assert not (tmp_path / "hello").exists()
    assert list(tmp_path.iterdir()) == []


@contextlib.contextmanager
def rmtree_always_fails(monkeypatch):
    """Every removal fails; whatever it refused to remove is cleaned on the way out."""
    real_rmtree = shutil.rmtree
    refused: list[str] = []

    def failing_rmtree(path, ignore_errors=False):
        refused.append(path)
        raise OSError(39, "Directory not empty")

    monkeypatch.setattr(create_mod.shutil, "rmtree", failing_rmtree)
    try:
        yield
    finally:
        for path in refused:
            real_rmtree(path, ignore_errors=True)


def test_create_fails_when_the_producer_scratch_directory_survives(
    tmp_path, git_identity, monkeypatch
):
    with rmtree_always_fails(monkeypatch):
        with pytest.raises(CreationError, match="scaffold scratch directory"):
            _create(tmp_path, producer=lambda name, scratch: minimal_scaffold(name))
    assert not (tmp_path / "hello").exists()


def test_create_reports_a_surviving_scratch_directory_alongside_the_producer_failure(
    tmp_path, git_identity, monkeypatch
):
    def refusing(name, scratch):
        raise CreationError("the wizard was cancelled")

    with rmtree_always_fails(monkeypatch):
        with pytest.raises(CreationError, match="cancelled") as exc:
            _create(tmp_path, producer=refusing)
    notes = getattr(exc.value, "__notes__", [])
    assert any("scaffold scratch directory" in note for note in notes)


def test_create_with_the_wizard_leaves_nothing_when_a_check_fails(
    tmp_path, monkeypatch, git_identity
):
    generating_wizard(tmp_path, monkeypatch)
    dest_parent = tmp_path / "parent"
    dest_parent.mkdir()

    def failing(root: Path) -> None:
        raise CreationError("staged Check `pixi run test` failed; not published")

    with pytest.raises(CreationError, match="not published"):
        _create(dest_parent, verifier=failing)
    assert not (dest_parent / "hello").exists()
    assert list(dest_parent.iterdir()) == []


@pytest.mark.parametrize("stacks", [(), ("go",), ("rust", "rust")])
def test_create_refuses_the_wizard_flag_outside_the_rust_stack(
    tmp_path, git_identity, stacks
):
    def never(name, scratch):
        raise AssertionError("the producer ran despite an invalid stack selection")

    with pytest.raises(CreationError, match="requires exactly `--stack rust`"):
        create_repo("hello", tmp_path, stacks, standout_wizard=True, producer=never)
    assert list(tmp_path.iterdir()) == []


def test_command_refuses_the_wizard_flag_outside_the_rust_stack(tmp_path, capsys):
    from shipit.verbs import repo as repo_verb

    rc = repo_verb.run_new(
        stacks=("go",), name="hello", parent=tmp_path, standout_wizard=True
    )
    assert rc == 1
    assert capsys.readouterr().err.startswith("error:")
    assert list(tmp_path.iterdir()) == []


def test_command_forwards_the_wizard_flag(tmp_path, monkeypatch):
    from shipit.repocreate import CreationResult
    from shipit.verbs import repo as repo_verb

    seen: dict[str, object] = {}

    def spy(name, parent, stacks, **kwargs):
        seen.update(kwargs)
        return CreationResult(
            destination=parent / name, initial_commit="abc123", stacks=stacks
        )

    monkeypatch.setattr(repo_verb, "create_repo", spy)
    assert (
        repo_verb.run_new(
            stacks=("rust",), name="hello", parent=tmp_path, standout_wizard=True
        )
        == 0
    )
    assert seen["standout_wizard"] is True


def test_new_command_exposes_the_flag_in_short_help():
    from click.testing import CliRunner

    from shipit.verbs.repo import repo

    out = " ".join(CliRunner().invoke(repo, ["new", "--help"]).output.split())
    assert "--standout-wizard" in out
    assert "`standout new-project` wizard" in out
    assert "Requires --stack rust." in out


# --- the interactive Exec ---------------------------------------------------


def test_run_interactive_inherits_the_streams_and_returns_the_rc(tmp_path, capfd):
    script = tmp_path / "talker.sh"
    script.write_text(
        '#!/bin/sh\nread -r line\necho "heard $line"\nexit 3\n', encoding="utf-8"
    )
    script.chmod(0o755)
    # A pipe on fd 0 proves inheritance: `execrun.run` would hand the child devnull.
    with piped_stdin("ping\n"):
        rc = execrun.run_interactive([str(script)], cwd=tmp_path)
    assert rc == 3
    assert "heard ping" in capfd.readouterr().out


def test_run_interactive_normalizes_a_missing_binary(tmp_path):
    with pytest.raises(execrun.ExecError) as excinfo:
        execrun.run_interactive([str(tmp_path / "nope")])
    assert excinfo.value.cause == execrun.CAUSE_MISSING_BINARY


# --- end-to-end against the released wizard ---------------------------------

#: Answers for the wizard's prompt sequence, verified against the binary: project
#: name, executable name, command, description, input count, input name, type,
#: cardinality, sources, result shape, record fields, then the confirmation.
REAL_WIZARD_ANSWERS = (
    "hello\nmytool\nprocess\nProcess one document\n\ndocument\n\n\n\n\n\nyes\n"
)


@pytest.mark.skipif(
    os.environ.get("SHIPIT_STANDOUT_WIZARD") != CERTIFIED_VERSION,
    reason=(
        f"needs standout {CERTIFIED_VERSION} on PATH: install it with `cargo install "
        f"standout --version {CERTIFIED_VERSION} --locked`, then set "
        f"SHIPIT_STANDOUT_WIZARD={CERTIFIED_VERSION}"
    ),
)
def test_released_standout_wizard_scaffold_imports_and_certifies(tmp_path):
    """Certify the wizard CONTRACT against the real binary: run it, import it, plan it."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with piped_stdin(REAL_WIZARD_ANSWERS):
        scaffold = standout_mod.produce(validate_name("hello"), scratch)
    paths = {file.path for file in scaffold.owned_files}

    assert paths == {
        "Cargo.toml",
        "crates/hellolib/Cargo.toml",
        "crates/hellolib/src/lib.rs",
        "crates/mytool/Cargo.toml",
        "crates/mytool/README.md",
        "crates/mytool/src/cli.rs",
        "crates/mytool/src/handlers.rs",
        "crates/mytool/src/main.rs",
        "crates/mytool/src/styles/hello.css",
        "crates/mytool/src/templates/process.jinja",
    }
    assert not any(path.startswith("hello/") for path in paths)
    assert scaffold.artifact == ArtifactDecl(
        name="mytool", toolchain="rust", package="mytool"
    )

    plan = build_plan(
        validate_name("hello"),
        (RustProfile(scaffold=scaffold),),
        author="Ada Lovelace",
        year=2026,
    )
    planned = {file.path: file.text for file in plan.files}
    assert 'resolver = "2"' in planned["Cargo.toml"]
    assert "standout" in planned["crates/mytool/Cargo.toml"]
    assert tomllib.loads(planned[".shipit.toml"])["artifacts"] == {
        "mytool": {"build": [{"toolchain": "rust", "package": "mytool"}]}
    }
