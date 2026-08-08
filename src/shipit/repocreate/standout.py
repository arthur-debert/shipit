"""``repocreate/standout`` — the Standout wizard as an alternate Rust scaffold producer.

See docs/adr/0086-alternate-scaffold-producers-inside-a-profile.md.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from pathlib import Path

from .. import execrun
from .errors import CreationError
from .names import ProjectName
from .profiles import ArtifactDecl, OwnedFile, RustScaffold

#: The wizard, as resolved from the caller's ``PATH``; never installed implicitly.
EXECUTABLE = "standout"

#: The wizard subcommand — it takes no arguments and asks everything on stdin.
SUBCOMMAND = "new-project"

_INSTALL_HINT = (
    "install it first, e.g. `cargo install standout --locked`, and make sure it "
    "is on PATH"
)


def produce(name: ProjectName, scratch: Path) -> RustScaffold:
    """Run the Standout wizard in ``scratch`` and import the project it generated."""
    _invoke(scratch)
    root = _generated_root(scratch, name)
    owned = _import(root)
    return RustScaffold(owned_files=owned, artifact=_derive_artifact(owned))


def _invoke(scratch: Path) -> None:
    binary = shutil.which(EXECUTABLE)
    if binary is None:
        raise CreationError(
            f"--standout-wizard needs the `{EXECUTABLE}` executable, which is not "
            f"on PATH; {_INSTALL_HINT}"
        )
    # Absolute: a relative PATH hit would otherwise re-resolve against `cwd=scratch`.
    rc = execrun.run_interactive([os.path.abspath(binary), SUBCOMMAND], cwd=scratch)
    if rc != 0:
        raise CreationError(
            f"`{EXECUTABLE} {SUBCOMMAND}` exited {rc}; no Repo was created"
        )


def _generated_root(scratch: Path, name: ProjectName) -> Path:
    """The wizard's single generated project directory, whose name must be ``name``.

    The wizard exits 0 when it is cancelled — and takes the default on EOF at every
    defaulted prompt — so producing nothing, not the rc, is what reads as cancelled.
    """
    entries = sorted(scratch.iterdir())
    if not entries:
        raise CreationError(
            f"`{EXECUTABLE} {SUBCOMMAND}` was cancelled and generated nothing; "
            "no Repo was created"
        )
    if len(entries) > 1:
        listed = ", ".join(entry.name for entry in entries)
        raise CreationError(
            f"`{EXECUTABLE} {SUBCOMMAND}` generated more than one top-level entry "
            f"({listed}); shipit imports exactly one generated project"
        )
    root = entries[0]
    if root.is_symlink() or not root.is_dir():
        raise CreationError(
            f"`{EXECUTABLE} {SUBCOMMAND}` generated {root.name!r}, which is not a "
            "directory; shipit imports exactly one generated project directory"
        )
    if root.name != name.value:
        raise CreationError(
            f"the wizard's project name {root.name!r} does not match the requested "
            f"Repo name {name.value!r}; the wizard has no name injection, so answer "
            f"its `Project name:` prompt with {name.value!r} and run again"
        )
    return root


def _import(root: Path) -> tuple[OwnedFile, ...]:
    """Read the generated tree into owned files; anything unsafe or undecodable refuses.

    The walk never follows a link and refuses every symlink it meets, which is what
    keeps each imported path inside ``root``.
    """
    owned: list[OwnedFile] = []
    for parent, dirs, files in os.walk(root, followlinks=False):
        for entry in dirs + files:
            path = Path(parent) / entry
            label = path.relative_to(root).as_posix()
            _check_importable(path, label)
            if path.is_dir():
                continue
            executable = bool(path.stat().st_mode & 0o111)
            owned.append(OwnedFile(label, _read_text(path, label), executable))
    if not owned:
        raise CreationError(
            f"`{EXECUTABLE} {SUBCOMMAND}` generated an empty project directory; "
            "no Repo was created"
        )
    return tuple(sorted(owned, key=lambda file: file.path))


def _check_importable(path: Path, label: str) -> None:
    if path.is_symlink():
        raise CreationError(
            f"the generated project entry {label!r} is a symlink; shipit imports "
            "only regular files and directories"
        )
    if not path.is_dir() and not path.is_file():
        raise CreationError(
            f"the generated project entry {label!r} is not a regular file; shipit "
            "imports only regular files and directories"
        )
    if ".git" in Path(label).parts:
        raise CreationError(
            f"the generated project carries the Git-internal path {label!r}; shipit "
            "owns the new Repo's Git state and refuses to import it"
        )


def _read_text(path: Path, label: str) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CreationError(
            f"the generated project file {label!r} is not valid UTF-8; shipit "
            "imports text sources only"
        ) from exc


def _derive_artifact(owned: tuple[OwnedFile, ...]) -> ArtifactDecl:
    """The Artifact of the one workspace member that builds a binary; zero or several refuse."""
    texts = {file.path: file.text for file in owned}
    members = _table(_parse(texts, "Cargo.toml"), "workspace").get("members")
    if not isinstance(members, list) or not members:
        raise CreationError(
            "the generated root Cargo.toml declares no `workspace.members`; shipit "
            "cannot tell which package is the executable"
        )
    binaries: list[str] = []
    for member in members:
        if not isinstance(member, str):
            raise CreationError(
                f"the generated root Cargo.toml lists a non-string workspace member "
                f"{member!r}"
            )
        manifest = _parse(texts, f"{member}/Cargo.toml")
        package = _table(manifest, "package").get("name")
        if not isinstance(package, str) or not package:
            raise CreationError(
                f"the generated member manifest {member}/Cargo.toml declares no "
                "`package.name`"
            )
        if manifest.get("bin") or f"{member}/src/main.rs" in texts:
            binaries.append(package)
    if len(binaries) != 1:
        found = ", ".join(sorted(binaries)) or "none"
        raise CreationError(
            f"the generated workspace must contain exactly one binary package for "
            f"shipit to declare as its Artifact; found {found}"
        )
    return ArtifactDecl(name=binaries[0], toolchain="rust", package=binaries[0])


def _parse(texts: dict[str, str], path: str) -> dict[str, object]:
    text = texts.get(path)
    if text is None:
        raise CreationError(f"the generated project has no {path}")
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise CreationError(f"the generated {path} is not valid TOML: {exc}") from exc


def _table(doc: dict[str, object], key: str) -> dict[str, object]:
    """``doc[key]`` as a table; a missing or non-table value reads as empty."""
    value = doc.get(key)
    return value if isinstance(value, dict) else {}
