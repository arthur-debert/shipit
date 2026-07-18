"""``tree/fleet`` — the fleet listing as typed records (ADR-0030).

The ``tree list`` verb's promoted domain half: :func:`build` derives, PURELY,
one frozen :class:`FleetTree` row per scanned
:class:`~shipit.tree.registry.TreeRecord` — the raw snapshot plus the two
facts the listing adds on top: the Tree's **created** timestamp — recovered from
the flat dir leaf's ``<timestamp>`` slot (:func:`~shipit.tree.layout.created_from_leaf`),
the first real creation column ``tree list`` has ever had (ADR-0074; ``None`` for an
old nested Tree that predates the flat grammar) — and its **age** against an injected
``now`` (no clock in here). ``created`` is a display fact only: ``gc`` never reads it
(creation-age is not activity-age, ADR-0072). The :class:`Fleet` wrapper is the ``--json``
surface: ``to_dict()`` declares the field set the render seam serializes
(ADR-0030), while the text table stays a pure verb-layer renderer over the
same rows — one derivation, two views that cannot disagree.
"""

from __future__ import annotations

from dataclasses import dataclass

from .layout import created_from_leaf
from .registry import TreeRecord


@dataclass(frozen=True)
class FleetTree:
    """One Tree's at-a-glance listing row, derived purely from its scan record.

    ``branch`` stays ``None`` for a detached/unborn HEAD and ``base`` stays
    ``None`` when absent — the renderer owns the placeholder spellings
    (``(detached)``, ``-``), so the JSON surface carries honest nulls.
    ``age_seconds`` is the Tree's age at the listing's ``now`` (clamped at
    zero: a just-touched Tree never reads negative). ``created`` is the flat
    leaf's ``%Y%m%d-%H%M%S`` stamp, or ``None`` for a pre-flat nested Tree.
    """

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
    """One record's listing row: the snapshot plus created stamp and age."""
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
