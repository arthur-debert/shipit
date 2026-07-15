"""Project-name value objects and validation for ``shipit repo new``.

The name is the load-bearing input to creation: it spells the destination
directory, the CLI package/executable, and — with a ``lib`` prefix — the
library package. Rust source refers to the library through Cargo's normal
hyphen-to-underscore crate-identifier conversion (``lib-my-tool`` → the crate
``lib_my_tool``). This module is the ONE place that derivation lives, so the
planner, the profiles, and the templates never re-derive a name and can never
disagree about how ``<name>`` becomes ``lib<name>`` becomes ``lib_<name>``.

Validation is the complete request grammar and destination-safety half of the
name contract (``docs/spec/repo-new.md`` §Proposed Shape; ADR-0059). Two layers
compose:

1. Canonical lowercase kebab-case — an ASCII lowercase letter, then lowercase
   alphanumeric segments joined by single hyphens. This alone refuses
   uppercase, underscores, whitespace, leading digits, and empty segments, and
   it is applied WITHOUT silent normalization: a name outside the grammar is
   rejected, never rewritten into shape.
2. The Cargo reservations. A name whose kebab spelling is fine can still be one
   the managed Cargo toolchain refuses — a Rust keyword, a name that collides
   with Cargo's build-directory artifact names, the built-in ``test`` crate, or
   a Windows reserved device name. Because the spec promises the derived crate
   names verbatim (``docs/spec/repo-new.md`` §Risks: silent normalization would
   violate the promised names), every DERIVED identifier is checked, not just
   the raw name: the CLI package/executable (``<name>``), the library package
   (``lib<name>``), and the two Rust import crate identifiers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import CreationError

#: Canonical lowercase kebab-case: an ASCII lowercase letter, then lowercase
#: alphanumeric segments joined by single hyphens (``docs/spec/repo-new.md``
#: §Proposed Shape). No leading digit, no leading/trailing/double hyphen, no
#: uppercase, no underscore — the destination, CLI package, and executable all
#: keep this exact spelling.
_KEBAB = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")

#: Rust keywords (strict, reserved, and weak), mirroring Cargo's own
#: ``restricted_names::is_keyword``. A package name that is a keyword is a hard
#: ``cargo new`` error because the crate identifier could not be a valid Rust
#: path segment. Only the lowercase, hyphen-free members can survive the kebab
#: grammar above, but the full list is kept so the check reads as Cargo's does.
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

#: Names Cargo refuses because they collide with subdirectories it creates under
#: the target directory (``restricted_names::is_conflicting_artifact_name``).
#: Under a binary package — which the ``rust`` profile always produces — these
#: are hard ``cargo new`` errors, not warnings.
_CARGO_ARTIFACT_DIRS = frozenset({"deps", "examples", "build", "incremental"})

#: Cargo's built-in test library. ``cargo new test`` is a hard error because the
#: name collides with Rust's ``test`` crate.
_CARGO_TEST_CRATE = frozenset({"test"})

#: Windows reserved device names (``restricted_names::is_windows_reserved``). A
#: directory with one of these names cannot exist on Windows, so a Repo named
#: for one could never be cloned there; creation refuses them for portability
#: even though ``cargo new`` only warns on a non-Windows host.
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{d}" for d in range(1, 10)}
    | {f"lpt{d}" for d in range(1, 10)}
)


@dataclass(frozen=True)
class ProjectName:
    """A validated project name and its deterministic derivations.

    Construct through :func:`validate_name`, never directly, so an unchecked
    string can never reach the planner. ``value`` is the canonical kebab-case
    spelling shared by the destination directory, the CLI package, and the
    executable; ``lib_pkg`` is the ``lib``-prefixed library package; and the
    ``*_crate`` fields apply Cargo's hyphen-to-underscore conversion for the
    identifiers Rust source imports.
    """

    value: str

    @property
    def cli_pkg(self) -> str:
        """The CLI package name and the installed executable — the raw name."""
        return self.value

    @property
    def lib_pkg(self) -> str:
        """The library-only package name: ``lib`` prefixed onto the raw name."""
        return f"lib{self.value}"

    @property
    def cli_crate(self) -> str:
        """The CLI crate identifier Rust imports (hyphens → underscores)."""
        return self.value.replace("-", "_")

    @property
    def lib_crate(self) -> str:
        """The library crate identifier Rust imports (hyphens → underscores)."""
        return self.lib_pkg.replace("-", "_")


def _reject_if_reserved(identifier: str, role: str) -> None:
    """Refuse ``identifier`` if the managed Cargo toolchain reserves it.

    ``role`` names which derived identifier is at fault (``the project name``,
    ``the derived library package``, …) so the error points the operator at the
    exact spelling that must change, not just "invalid". The four reservation
    classes each get a distinct message because each has a distinct fix: a Rust
    keyword and Cargo's ``test`` crate must simply be renamed; an artifact-dir
    collision and a Windows device name explain WHY the toolchain or a Windows
    checkout would reject the Repo.
    """
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
    """Parse ``raw`` into a :class:`ProjectName` or raise :class:`CreationError`.

    Enforces canonical lowercase kebab-case (:data:`_KEBAB`) so creation never
    silently rewrites a name into a shape the destination path or the Cargo
    workspace would reject, then refuses any name the managed Cargo toolchain
    reserves — checking every DERIVED identifier, not just the raw name: the CLI
    package/executable (``<name>``), the library package (``lib<name>``), and the
    two Rust import crate identifiers. Because the spec promises those spellings
    verbatim (``docs/spec/repo-new.md`` §Risks), a reserved DERIVED name is a
    rejection here, never a silent rewrite. The error names the exact rule and
    the offending identifier rather than a bare "invalid".
    """
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
    # Check every derived identifier the Repo will carry. The CLI package is the
    # raw name; the library package prefixes `lib`; the crate identifiers apply
    # Cargo's hyphen→underscore conversion. A kebab name and its derivations can
    # each independently land on a Cargo reservation (e.g. the name `test`), so
    # each is validated against the full reservation set.
    _reject_if_reserved(name.cli_pkg, "the project name")
    _reject_if_reserved(name.lib_pkg, "the derived library package")
    _reject_if_reserved(name.cli_crate, "the derived CLI crate identifier")
    _reject_if_reserved(name.lib_crate, "the derived library crate identifier")
    return name
