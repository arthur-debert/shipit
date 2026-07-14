"""Project-name value objects and validation for ``shipit repo new``.

The name is the load-bearing input to creation: it spells the destination
directory, the CLI package/executable, and — with a ``lib`` prefix — the
library package. Rust source refers to the library through Cargo's normal
hyphen-to-underscore crate-identifier conversion (``lib-my-tool`` → the crate
``lib_my_tool``). This module is the ONE place that derivation lives, so the
planner, the profiles, and the templates never re-derive a name and can never
disagree about how ``<name>`` becomes ``lib<name>`` becomes ``lib_<name>``.

Validation here is the NARROW happy-path rule (the WS01 tracer): canonical
lowercase kebab-case. The exhaustive request-validation matrix — reserved Cargo
names, length bounds, Unicode confusables — is WS02's slice
(``docs/spec/repo-new.md`` §Non-Goals / the epic decomposition); this module
refuses the obviously-invalid and derives cleanly for the valid remainder.
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


def validate_name(raw: str) -> ProjectName:
    """Parse ``raw`` into a :class:`ProjectName` or raise :class:`CreationError`.

    Enforces canonical lowercase kebab-case (:data:`_KEBAB`) so creation never
    silently rewrites a name into a shape the destination path or the Cargo
    workspace would reject. The error names the exact rule rather than a bare
    "invalid", so an operator can fix the request without reading the spec.
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
    return ProjectName(value=raw)
