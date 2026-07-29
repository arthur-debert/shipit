"""The filesystem containment primitives: the shared REFUSE-LINKS predicates.

No link is ever followed, so a path built from real components physically cannot
leave its base. These helpers REPORT the offending component and never raise.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


def is_link(path: Path) -> bool:
    """True if ``path``'s final component is a REDIRECT — a POSIX symlink or a Windows junction / mount-point reparse point. A non-existent path is not a link."""
    return path.is_symlink() or path.is_junction()


def first_link_component(base: Path, parts: Sequence[str]) -> Path | None:
    """The first path under ``base``, walking ``parts`` ONE COMPONENT AT A TIME, that :func:`is_link` — or ``None`` when the whole chain is real. ``base`` itself is not inspected."""
    current = base
    for part in parts:
        current = current / part
        if is_link(current):
            return current
    return None
