"""``repocreate/errors`` — the one error type the repository-creation domain DEFINES."""

from __future__ import annotations


class CreationError(RuntimeError):
    """A handled repository-creation failure — never a partial published Repo."""
