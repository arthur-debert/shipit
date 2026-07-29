"""Load the review instructions text — an explicit path, else the bundled default."""

from __future__ import annotations

import importlib.resources


def default_instructions() -> str:
    """Return the bundled default review instructions text."""
    return (
        importlib.resources.files("shipit.review")
        .joinpath("instructions.txt")
        .read_text(encoding="utf-8")
    )


def load_instructions(path: str | None) -> str:
    """Return review instructions: from ``path`` if given, else the bundled default."""
    if path is not None:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    return default_instructions()
