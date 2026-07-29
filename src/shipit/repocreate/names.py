"""``repocreate/names`` — the project-name value object, its grammar, and its derivations.

Destination safety lives in ``create.py``'s preflight, not here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import CreationError

#: Canonical lowercase kebab-case, applied WITHOUT normalization: a name outside it
#: is rejected, never rewritten into shape.
_KEBAB = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")

#: Rust keywords, mirroring Cargo's own ``restricted_names::is_keyword``.
_RUST_KEYWORDS = frozenset(
    {
        "abstract", "as", "async", "await", "become", "box", "break", "const",
        "continue", "crate", "do", "dyn", "else", "enum", "extern", "false",
        "final", "fn", "for", "if", "impl", "in", "let", "loop", "macro",
        "match", "mod", "move", "mut", "override", "priv", "pub", "ref",
        "return", "self", "static", "struct", "super", "trait", "true", "try",
        "type", "typeof", "unsafe", "unsized", "use", "virtual", "where",
        "while", "yield",
    }
)  # fmt: skip

#: Names colliding with subdirectories Cargo creates under the target directory.
_CARGO_ARTIFACT_DIRS = frozenset({"deps", "examples", "build", "incremental"})

_CARGO_TEST_CRATE = frozenset({"test"})

#: Windows reserved device names; refused for portability even off Windows.
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{d}" for d in range(1, 10)}
    | {f"lpt{d}" for d in range(1, 10)}
)


@dataclass(frozen=True)
class ProjectName:
    """A validated project name; construct through :func:`validate_name`, never directly."""

    value: str

    @property
    def cli_pkg(self) -> str:
        return self.value

    @property
    def lib_pkg(self) -> str:
        return f"lib{self.value}"

    @property
    def cli_crate(self) -> str:
        return self.value.replace("-", "_")

    @property
    def lib_crate(self) -> str:
        return self.lib_pkg.replace("-", "_")


def _reject_if_reserved(identifier: str, role: str) -> None:
    """Refuse ``identifier`` if Cargo reserves it; ``role`` names it in the message."""
    if identifier in _RUST_KEYWORDS:
        raise CreationError(
            f"invalid project name: {role} {identifier!r} is a Rust keyword and "
            "cannot be a Cargo package name; choose another name"
        )
    if identifier in _CARGO_TEST_CRATE:
        raise CreationError(
            f"invalid project name: {role} {identifier!r} collides with Rust's "
            "built-in `test` crate and is rejected by Cargo; choose another name"
        )
    if identifier in _CARGO_ARTIFACT_DIRS:
        raise CreationError(
            f"invalid project name: {role} {identifier!r} collides with a Cargo "
            "build-directory name and is rejected by `cargo new`; choose "
            "another name"
        )
    if identifier in _WINDOWS_RESERVED:
        raise CreationError(
            f"invalid project name: {role} {identifier!r} is a Windows reserved "
            "device name; a Repo directory with that name cannot exist on "
            "Windows, so creation refuses it; choose another name"
        )


def validate_name(raw: str) -> ProjectName:
    """Parse ``raw`` into a :class:`ProjectName`, checking every DERIVED identifier too."""
    if not raw:
        raise CreationError("project name is empty; expected lowercase kebab-case")
    if not _KEBAB.match(raw):
        raise CreationError(
            f"invalid project name {raw!r}: expected canonical lowercase "
            "kebab-case — an ASCII lowercase letter followed by lowercase "
            "alphanumeric segments joined by single hyphens (e.g. `hello`, "
            "`my-tool`), with no leading digit, uppercase, underscore, or "
            "leading/trailing/double hyphen"
        )
    name = ProjectName(value=raw)
    _reject_if_reserved(name.cli_pkg, "the project name")
    _reject_if_reserved(name.lib_pkg, "the derived library package")
    _reject_if_reserved(name.cli_crate, "the derived CLI crate identifier")
    _reject_if_reserved(name.lib_crate, "the derived library crate identifier")
    return name
