"""``tree/fleet`` — the fleet listing as typed records (ADR-0030).

The ``tree list`` verb's promoted domain half: :func:`build` derives, PURELY,
one frozen :class:`FleetTree` row per scanned
:class:`~shipit.tree.registry.TreeRecord` — the raw snapshot plus the two
facts the listing adds on top: the Tree's **kind**
(:func:`~shipit.tree.layout.tree_kind` — write / review / ephemeral, surfaced
as an at-a-glance fact rather than left implied by the path; reclaim itself is
one uniform activity-based rule, ADR-0072) and its **age** against an injected
``now`` (no clock in here). The :class:`Fleet` wrapper is the ``--json``
surface: ``to_dict()`` declares the field set the render seam serializes
(ADR-0030), while the text table stays a pure verb-layer renderer over the
same rows — one derivation, two views that cannot disagree.
"""

from __future__ import annotations

from dataclasses import dataclass

from .layout import tree_kind
from .registry import TreeRecord


@dataclass(frozen=True)
class FleetTree:
    """One Tree's at-a-glance listing row, derived purely from its scan record.

    ``branch`` stays ``None`` for a detached/unborn HEAD and ``base`` stays
    ``None`` when absent — the renderer owns the placeholder spellings
    (``(detached)``, ``-``), so the JSON surface carries honest nulls.
    ``age_seconds`` is the Tree's age at the listing's ``now`` (clamped at
    zero: a just-touched Tree never reads negative).
    """

    path: str
    kind: str
    branch: str | None
    base: str | None
    ahead: int
    behind: int
    dirty: bool
    age_seconds: int

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "kind": self.kind,
            "branch": self.branch,
            "base": self.base,
            "ahead": self.ahead,
            "behind": self.behind,
            "dirty": self.dirty,
            "age_seconds": self.age_seconds,
        }


@dataclass(frozen=True)
class Fleet:
    """The whole fleet as typed rows — the result ``tree list`` renders.

    An empty fleet is a valid value (no Trees yet), not an error; ``to_dict``
    still declares the ``trees`` field so a ``--json`` consumer always gets
    the same shape.
    """

    trees: tuple[FleetTree, ...]

    def to_dict(self) -> dict:
        return {"trees": [tree.to_dict() for tree in self.trees]}


def build(records: list[TreeRecord], *, now: float) -> Fleet:
    """Derive the :class:`Fleet` from the scanned records at time ``now``. Pure.

    ``now`` is an input (mirroring ``cleanup.classify``), so the age every row
    carries is deterministic under test. Row order is the scan's own (sorted
    by path — the registry's stable-listing contract).
    """
    return Fleet(trees=tuple(_row(record, now=now) for record in records))


def _row(record: TreeRecord, *, now: float) -> FleetTree:
    """One record's listing row: the snapshot plus kind and age."""
    return FleetTree(
        path=record.path,
        kind=tree_kind(record.path),
        branch=record.branch,
        base=record.base,
        ahead=record.ahead,
        behind=record.behind,
        dirty=record.dirty,
        age_seconds=int(max(now - record.mtime, 0)),
    )
