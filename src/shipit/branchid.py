"""Branch-identity derivation — a pure, total parse from a branch name to the ``(epic, ws)`` it carries.

See docs/adr/0032-dev-cycle-events-tagged-records-witness-tiers.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_WORK_STREAM = re.compile(r"^(?P<epic>[A-Za-z0-9]+)/WS(?P<ws>\d{2,})$")

_UMBRELLA = re.compile(r"^(?P<epic>[A-Za-z0-9]+)/umbrella$")


@dataclass(frozen=True)
class BranchIdentity:
    """The dev-cycle identity a branch name carries; ``ws`` is an int, and either half may be ``None``."""

    epic: str | None = None
    ws: int | None = None


NOTHING = BranchIdentity()


def derive(branch: object) -> BranchIdentity:
    """The ``(epic, ws)`` identity of ``branch``, or :data:`NOTHING`; total on any input, including a non-string."""
    if not isinstance(branch, str):
        return NOTHING
    if match := _WORK_STREAM.fullmatch(branch):
        ws = int(match.group("ws"))
        if ws < 1:
            return NOTHING
        return BranchIdentity(epic=match.group("epic"), ws=ws)
    if match := _UMBRELLA.fullmatch(branch):
        return BranchIdentity(epic=match.group("epic"))
    return NOTHING
