"""``tree/gc`` — Tree garbage collection as a pure plan plus an effectful sweep.

:func:`plan` decides, :func:`plan_fleet` gathers, :func:`sweep` applies; ``--dry-run``
renders the same plan the real sweep consumes, so preview and action cannot drift.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import registry
from .cleanup import IDLE_THRESHOLD_SECONDS, Cleanup, classify, is_unexamined
from .readonly import remove_tree
from .registry import TreeRecord

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger("shipit.tree")


@dataclass(frozen=True)
class GcPlan:
    """The frozen gc decision: the fleet's partition plus the sweep's context.

    ``unexamined`` counts Trees the rule could not JUDGE because a signal was
    unreadable. It is always a SUBSET of ``partition.keep``, never of ``removable``.
    """

    partition: Cleanup
    total: int
    unexamined: int

    @property
    def judged(self) -> int:
        """How many Trees the rule reached a verdict on."""
        return self.total - self.unexamined

    @property
    def incomplete(self) -> bool:
        """Whether the fleet was only PARTIALLY judged — gc says so and exits
        non-zero rather than reporting a clean bill of health for a root it could
        not fully read."""
        return self.unexamined > 0


@dataclass(frozen=True)
class GcFailure:
    """One Tree the sweep could not delete: its path and the failure text."""

    path: str
    error: str


@dataclass(frozen=True)
class GcResult:
    """What the sweep actually did.

    ``removed`` holds only the paths that CAME OFF DISK; ``failed`` the per-Tree
    delete failures the sweep continued past; ``kept`` the untouched bucket;
    ``total``/``unexamined`` the plan's fleet-view counts.
    """

    removed: tuple[str, ...]
    failed: tuple[GcFailure, ...]
    kept: int
    total: int
    unexamined: int

    @property
    def judged(self) -> int:
        """How many Trees the rule reached a verdict on."""
        return self.total - self.unexamined

    @property
    def incomplete(self) -> bool:
        """Whether the fleet was only PARTIALLY judged."""
        return self.unexamined > 0


def plan(
    records: list[TreeRecord],
    *,
    now: float,
    idle_threshold_seconds: float = IDLE_THRESHOLD_SECONDS,
) -> GcPlan:
    """Partition the fleet into the frozen :class:`GcPlan`. Pure — every effectful
    input arrives as a value."""
    decision = classify(
        records,
        now=now,
        idle_threshold_seconds=idle_threshold_seconds,
    )
    unexamined = sum(1 for record in records if is_unexamined(record))
    return GcPlan(partition=decision, total=len(records), unexamined=unexamined)


def plan_fleet(
    root: str | Path, *, idle_threshold_seconds: float = IDLE_THRESHOLD_SECONDS
) -> GcPlan:
    """Scan the central root and build the :class:`GcPlan` — the effectful gather.

    Read-only: nothing on disk is mutated.
    """
    return plan(
        registry.scan(root),
        now=time.time(),
        idle_threshold_seconds=idle_threshold_seconds,
    )


def sweep(
    gc_plan: GcPlan,
    *,
    remove: Callable[[str], bool] = remove_tree,
    on_removed: Callable[[str], None] | None = None,
) -> GcResult:
    """Delete the plan's removable Trees; return the typed :class:`GcResult`.

    Deletion is best-effort per Tree: a failed delete lands in ``failed`` and the
    sweep continues. A path already gone (``remove`` returns ``False``) is skipped
    silently, so ``removed`` reflects what actually came off disk. ``on_removed`` is
    called with each path the instant it comes off disk — the audit trail that
    survives a sweep killed mid-fleet — so a path is announced BEFORE it is
    accumulated; a sink that raises is not caught here.
    """
    removed: list[str] = []
    failed: list[GcFailure] = []
    for record in gc_plan.partition.removable:
        try:
            deleted = remove(record.path)
        except OSError as exc:
            logger.warning(
                "tree gc could not remove %s",
                record.path,
                exc_info=True,
                extra={"tree": record.path},
            )
            failed.append(GcFailure(path=record.path, error=str(exc)))
            continue
        if not deleted:
            continue
        if on_removed is not None:
            on_removed(record.path)
        removed.append(record.path)
    kept = len(gc_plan.partition.keep)
    logger.info("tree gc removed %d, kept %d", len(removed), kept)
    if gc_plan.unexamined:
        logger.warning(
            "tree gc judged %d of %d; %d kept unexamined (a signal could not be read "
            "— incomplete view of the fleet)",
            gc_plan.judged,
            gc_plan.total,
            gc_plan.unexamined,
        )
    return GcResult(
        removed=tuple(removed),
        failed=tuple(failed),
        kept=kept,
        total=gc_plan.total,
        unexamined=gc_plan.unexamined,
    )
