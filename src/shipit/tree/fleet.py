"""``tree/fleet`` — the ``tree list`` fleet listing as typed, purely derived records."""

from __future__ import annotations

from dataclasses import dataclass

from .layout import created_from_leaf
from .registry import TreeRecord


@dataclass(frozen=True)
class FleetTree:
    """One Tree's listing row. ``age_seconds`` is clamped at zero; ``created`` is
    ``None`` for a dir leaf that carries no timestamp."""

    path: str
    created: str | None
    branch: str | None
    base: str | None
    ahead: int
    behind: int
    dirty: bool
    age_seconds: int

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "created": self.created,
            "branch": self.branch,
            "base": self.base,
            "ahead": self.ahead,
            "behind": self.behind,
            "dirty": self.dirty,
            "age_seconds": self.age_seconds,
        }


@dataclass(frozen=True)
class Fleet:
    """The whole fleet as typed rows — the result ``tree list`` renders."""

    trees: tuple[FleetTree, ...]

    def to_dict(self) -> dict:
        return {"trees": [tree.to_dict() for tree in self.trees]}


def build(records: list[TreeRecord], *, now: float) -> Fleet:
    """Derive the :class:`Fleet` from the scanned records at time ``now``. Pure."""
    return Fleet(trees=tuple(_row(record, now=now) for record in records))


def _row(record: TreeRecord, *, now: float) -> FleetTree:
    return FleetTree(
        path=record.path,
        created=created_from_leaf(record.path),
        branch=record.branch,
        base=record.base,
        ahead=record.ahead,
        behind=record.behind,
        dirty=record.dirty,
        age_seconds=int(max(now - record.mtime, 0)),
    )
