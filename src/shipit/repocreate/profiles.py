"""The closed Creation-profile registry (ADR-0056, ADR-0063).

Each ``repo new --stack <key>`` value resolves to exactly one Creation profile:
a shipit-owned, reviewed contributor of the initial project files and
declarations for one toolchain. Profiles are inputs to creation only — the
finished Repo persists its path-to-toolchain map and Artifacts, never a stack
or whole-Repo Kind (ADR-0056) — and the registry is CLOSED (ADR-0063): adding
Rust's future peers is a reviewed shipit change with packaged resources and
fixtures, not runtime plugin discovery.

A profile returns a :class:`Contribution` — the structured claims the central
planner (:mod:`.plan`) composes into one Repo (ADR-0057). A profile owns the
files exclusive to its toolchain (Cargo manifests, Rust source, the black-box
test) and CONTRIBUTES to the shared, planner-rendered manifests (pixi
dependencies, ``.gitignore`` lines, Artifact declarations); it never splices a
shared manifest itself. Exclusively-owned structured files (Cargo manifests)
are built as values and serialized through :mod:`.tomlio`; owned text (Rust
source, the test) is templated through :mod:`.templates`. This is the WS01
tracer: the one supported profile is ``rust``.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import tomlio
from .errors import CreationError
from .names import ProjectName
from .templates import render_text


@dataclass(frozen=True)
class OwnedFile:
    """One consumer-owned file a profile (or the universal seed) writes.

    ``path`` is repo-relative POSIX; ``text`` is the fully rendered content
    (structured files are already serialized). ``executable`` marks scripts.
    """

    path: str
    text: str
    executable: bool = False


@dataclass(frozen=True)
class ArtifactDecl:
    """A profile's Artifact claim, rendered once into the shared ``.shipit.toml``.

    ``name`` is the Artifact and ``toolchain``/``package`` its single Rust build
    target (ADR-0057: the profile declares the primary product; the planner
    serializes the ``[artifacts.<name>]`` table). No endpoint, bundle, or
    signing policy — the tracer declaration only gives build an unambiguous
    target (``docs/spec/repo-new.md`` §Proposed Shape).
    """

    name: str
    toolchain: str
    package: str


@dataclass(frozen=True)
class Contribution:
    """One profile's structured claims to the central planner (ADR-0057).

    ``owned_files`` are files exclusive to this profile; ``pixi_dependencies``
    and ``gitignore_lines`` are contributions the planner renders once into the
    shared pixi manifest and ``.gitignore``; ``artifacts`` are the Artifact
    declarations the planner writes into ``.shipit.toml``.
    """

    owned_files: tuple[OwnedFile, ...] = ()
    pixi_dependencies: tuple[tuple[str, str], ...] = ()
    gitignore_lines: tuple[str, ...] = ()
    artifacts: tuple[ArtifactDecl, ...] = ()


# --------------------------------------------------------------------------
# Rust profile source — owned TEXT (templated) and owned STRUCTURED (via tomlio)
# --------------------------------------------------------------------------

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
    """The virtual workspace-root ``Cargo.toml`` (a workspace, not a package).

    Resolver 3, edition 2024, version 0.1.0, and MIT licence live in
    ``[workspace.package]`` and are inherited by both members
    (``docs/spec/repo-new.md`` §Design Decisions). Member paths mirror package
    names at ``crates/<name>`` and ``crates/lib<name>``.
    """
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
    """The CLI member ``Cargo.toml``: the ``<name>`` package/binary, whose only
    runtime dependency is a path dependency on ``lib<name>``."""
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
    """The library member ``Cargo.toml``: the library-only ``lib<name>`` package."""
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


class RustProfile:
    """The ``rust`` Creation profile: a virtual two-member Cargo workspace."""

    key = "rust"

    def contribute(self, name: ProjectName) -> Contribution:
        """Build this profile's structured claim for ``name``.

        Owns the workspace + member manifests (structured, via
        :mod:`.tomlio`), the CLI/library source and the one black-box test
        (text, via :mod:`.templates`); contributes ``cargo-nextest`` to the
        default pixi env, ``/target/`` to ``.gitignore``, and the CLI Artifact
        declaration.
        """
        ctx = {
            "cli_pkg": name.cli_pkg,
            "lib_pkg": name.lib_pkg,
            "cli_crate": name.cli_crate,
            "lib_crate": name.lib_crate,
        }
        cli = name.cli_pkg
        lib = name.lib_pkg
        owned = (
            OwnedFile("Cargo.toml", _workspace_manifest(name)),
            OwnedFile(f"crates/{cli}/Cargo.toml", _cli_manifest(name)),
            OwnedFile(f"crates/{cli}/src/main.rs", render_text(_MAIN_RS, ctx)),
            OwnedFile(f"crates/{cli}/tests/cli.rs", render_text(_TEST_RS, ctx)),
            OwnedFile(f"crates/{lib}/Cargo.toml", _lib_manifest(name)),
            OwnedFile(f"crates/{lib}/src/lib.rs", render_text(_LIB_RS, ctx)),
        )
        return Contribution(
            owned_files=owned,
            pixi_dependencies=(("cargo-nextest", "*"),),
            gitignore_lines=("/target/",),
            artifacts=(ArtifactDecl(name=cli, toolchain="rust", package=cli),),
        )


#: The closed, shipit-owned registry, keyed by ``--stack`` value (ADR-0063).
_REGISTRY: dict[str, RustProfile] = {"rust": RustProfile()}


def resolve_profiles(stacks: tuple[str, ...]) -> tuple[RustProfile, ...]:
    """Resolve the repeated ``--stack`` values to their Creation profiles.

    At least one stack is mandatory; unknown values and duplicate selections
    are usage errors (``docs/spec/repo-new.md`` §Proposed Shape / User Stories
    8–9), raised as :class:`CreationError` so the command never silently omits
    or double-counts a requested capability.
    """
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
