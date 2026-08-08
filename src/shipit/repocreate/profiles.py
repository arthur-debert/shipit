"""``repocreate/profiles`` — the closed Creation-profile registry.

See docs/adr/0063-creation-profiles-are-closed.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from . import tomlio
from .errors import CreationError
from .names import ProjectName
from .templates import render_text


@dataclass(frozen=True)
class OwnedFile:
    """``path`` is repo-relative POSIX; ``text`` is fully rendered."""

    path: str
    text: str
    executable: bool = False


@dataclass(frozen=True)
class ArtifactDecl:
    """A profile's Artifact claim: a name and its single build target."""

    name: str
    toolchain: str
    package: str


@dataclass(frozen=True)
class RustScaffold:
    """A produced Rust application tree: its files and the Artifact they declare."""

    owned_files: tuple[OwnedFile, ...]
    artifact: ArtifactDecl


@dataclass(frozen=True)
class Contribution:
    """``owned_files`` are exclusive to this profile; the rest are contributions
    the planner renders once into the shared manifests.
    """

    owned_files: tuple[OwnedFile, ...] = ()
    pixi_dependencies: tuple[tuple[str, str], ...] = ()
    gitignore_lines: tuple[str, ...] = ()
    artifacts: tuple[ArtifactDecl, ...] = ()


_LIB_RS = """\
//! {{ lib_pkg }} — the library half of the {{ cli_pkg }} workspace.
//!
//! Exposes the greeting the CLI prints. Keeping the value here lets the
//! black-box test exercise the binary, this library, and their wiring
//! together (docs/spec/repo-new.md).

/// Returns the canonical hello-world greeting.
pub fn greeting() -> &'static str {
    "Hello, world!"
}
"""

_MAIN_RS = """\
//! {{ cli_pkg }} — the CLI half of the workspace.
//!
//! Prints the greeting obtained from `{{ lib_crate }}`, demonstrating the
//! intended CLI-consumes-library dependency direction.

fn main() {
    println!("{}", {{ lib_crate }}::greeting());
}
"""

_TEST_RS = """\
//! Black-box CLI test: run the built `{{ cli_pkg }}` executable and assert its
//! output. This is the project's single test (docs/spec/repo-new.md) — it
//! exercises the binary, its `{{ lib_pkg }}` dependency, and the configured
//! Rust test runner together, asserting only observable output.

use std::process::Command;

#[test]
fn prints_hello_world() {
    let output = Command::new(env!("CARGO_BIN_EXE_{{ cli_pkg }}"))
        .output()
        .expect("failed to run the {{ cli_pkg }} executable");
    assert!(output.status.success(), "{{ cli_pkg }} exited non-zero");
    let stdout = String::from_utf8(output.stdout).expect("stdout was not UTF-8");
    assert_eq!(stdout.trim_end(), "Hello, world!");
}
"""


def _workspace_manifest(name: ProjectName) -> str:
    """The VIRTUAL workspace-root ``Cargo.toml`` — a workspace, not a package."""
    return tomlio.dumps(
        {
            "workspace": {
                "resolver": "3",
                "members": [
                    f"crates/{name.cli_pkg}",
                    f"crates/{name.lib_pkg}",
                ],
            },
            "workspace.package": {
                "version": "0.1.0",
                "edition": "2024",
                "license": "MIT",
            },
        }
    )


def _cli_manifest(name: ProjectName) -> str:
    """The CLI member ``Cargo.toml``; its only dependency is a path dep on the lib."""
    return tomlio.dumps(
        {
            "package": {
                "name": name.cli_pkg,
                "version.workspace": True,
                "edition.workspace": True,
                "license.workspace": True,
            },
            "dependencies": {
                name.lib_pkg: tomlio.Inline({"path": f"../{name.lib_pkg}"}),
            },
            "bin": [{"name": name.cli_pkg, "path": "src/main.rs"}],
        }
    )


def _lib_manifest(name: ProjectName) -> str:
    return tomlio.dumps(
        {
            "package": {
                "name": name.lib_pkg,
                "version.workspace": True,
                "edition.workspace": True,
                "license.workspace": True,
            },
            "lib": {"name": name.lib_crate, "path": "src/lib.rs"},
        }
    )


def minimal_scaffold(name: ProjectName) -> RustScaffold:
    """The profile's own hello-world application tree — the default producer."""
    ctx = {
        "cli_pkg": name.cli_pkg,
        "lib_pkg": name.lib_pkg,
        "cli_crate": name.cli_crate,
        "lib_crate": name.lib_crate,
    }
    cli = name.cli_pkg
    lib = name.lib_pkg
    return RustScaffold(
        owned_files=(
            OwnedFile("Cargo.toml", _workspace_manifest(name)),
            OwnedFile(f"crates/{cli}/Cargo.toml", _cli_manifest(name)),
            OwnedFile(f"crates/{cli}/src/main.rs", render_text(_MAIN_RS, ctx)),
            OwnedFile(f"crates/{cli}/tests/cli.rs", render_text(_TEST_RS, ctx)),
            OwnedFile(f"crates/{lib}/Cargo.toml", _lib_manifest(name)),
            OwnedFile(f"crates/{lib}/src/lib.rs", render_text(_LIB_RS, ctx)),
        ),
        artifact=ArtifactDecl(name=cli, toolchain="rust", package=cli),
    )


@dataclass(frozen=True)
class RustProfile:
    """The ``rust`` Creation profile: a virtual two-member Cargo workspace.

    ``scaffold``, when given, supplies the application tree an alternate producer
    already built, in place of :func:`minimal_scaffold`; the rest of the
    contribution is the same either way. See
    docs/adr/0086-alternate-scaffold-producers-inside-a-profile.md.
    """

    key: ClassVar[str] = "rust"

    scaffold: RustScaffold | None = None

    def contribute(self, name: ProjectName) -> Contribution:
        produced = (
            self.scaffold if self.scaffold is not None else minimal_scaffold(name)
        )
        return Contribution(
            owned_files=produced.owned_files,
            pixi_dependencies=(("cargo-nextest", "*"),),
            gitignore_lines=("/target/",),
            artifacts=(produced.artifact,),
        )


#: The closed registry, keyed by ``--stack`` value.
_REGISTRY: dict[str, RustProfile] = {"rust": RustProfile()}


def require_rust_only(stacks: tuple[str, ...], option: str) -> None:
    """Refuse ``option`` unless the request selects the ``rust`` profile alone."""
    if stacks != ("rust",):
        selected = " ".join(f"--stack {stack}" for stack in stacks) or "no --stack"
        raise CreationError(
            f"{option} requires exactly `--stack rust`; the request selected {selected}"
        )


def resolve_profiles(stacks: tuple[str, ...]) -> tuple[RustProfile, ...]:
    """At least one stack is mandatory; unknown and duplicate values both refuse."""
    if not stacks:
        raise CreationError(
            "at least one --stack is required (v1 supports: "
            f"{', '.join(sorted(_REGISTRY))})"
        )
    seen: set[str] = set()
    resolved: list[RustProfile] = []
    for stack in stacks:
        if stack in seen:
            raise CreationError(f"duplicate --stack {stack!r}")
        seen.add(stack)
        profile = _REGISTRY.get(stack)
        if profile is None:
            raise CreationError(
                f"unknown --stack {stack!r}; v1 supports: "
                f"{', '.join(sorted(_REGISTRY))}"
            )
        resolved.append(profile)
    return tuple(resolved)
