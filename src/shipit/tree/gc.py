"""``tree/gc`` — garbage collection as a plan + a sweep (ADR-0030).

The gc verb's promoted domain half, split on the boundary contract:

- :func:`plan` is PURE: given the scanned records and the one remaining effectful
  input as a value (``now``), it wraps :func:`shipit.tree.cleanup.classify`'s
  partition into a frozen :class:`GcPlan` — the exact decision BOTH gc modes act
  on. ``--dry-run`` renders this plan; the real sweep consumes it; parity is by
  construction.
- :func:`plan_fleet` is the effectful GATHER (mirroring ``prstate.fetch``'s
  snapshot idiom): scan the central root, then call the pure :func:`plan`. It
  reads; it never mutates. Under ADR-0072 the gather has almost nothing left to
  do — every signal the rule wants rides the scanned record, and the PR-state,
  liveness and provisioning reads it used to make are gone with the ladder that
  consulted them.
- :func:`sweep` is the effectful APPLY: delete exactly the plan's removable
  Trees and return a typed :class:`GcResult` of what actually happened. It
  announces each removal AS it happens through the caller's ``on_removed``
  sink, so a sweep that is interrupted mid-fleet still leaves a record of the
  Trees it destroyed (#1011). No printing anywhere in this module — the sink
  is the verb's renderer, called from here but written there; the durable log
  twins (the per-failure WARNING, the sweep milestone, the incomplete-view
  warning; ADR-0029) live here with the effect they narrate.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..session import liveness
from . import layout, provision, registry
from .cleanup import IDLE_THRESHOLD_SECONDS, Cleanup, classify, is_unexamined
from .readonly import remove_tree
from .registry import TreeRecord

if TYPE_CHECKING:
    from pathlib import Path

    from ..identity import Sha

logger = logging.getLogger("shipit.tree")


@dataclass(frozen=True)
class GcPlan:
    """The frozen gc decision: the fleet's partition plus the sweep's context.

    ``partition`` is :func:`~shipit.tree.cleanup.classify`'s two-bucket
    :class:`~shipit.tree.cleanup.Cleanup`; ``total`` is how many Trees were
    scanned and ``unexamined`` how many the rule could not JUDGE because a signal
    was unreadable (:func:`~shipit.tree.cleanup.is_unexamined`). The counts travel
    WITH the partition because both gc tails — the ``--dry-run`` render and the real
    sweep — need them to surface an INCOMPLETE view of the fleet.

    ``unexamined`` counts a SUBSET OF ``partition.keep``, never of ``removable``: an
    unreadable signal keeps its Tree (ADR-0072), so the count and the deletions can
    never describe the same Tree. That disjointness is the invariant the count's
    predecessor lacked — it was projected off PR state, which the rule had stopped
    reading, so a Tree could be reported "skipped" by the run that deleted it.
    """

    partition: Cleanup
    total: int
    unexamined: int

    @property
    def judged(self) -> int:
        """How many Trees the rule actually reached a verdict on (``total`` less the
        unexamined)."""
        return self.total - self.unexamined

    @property
    def incomplete(self) -> bool:
        """Whether the fleet was only PARTIALLY judged.

        True when any Tree was kept on an unreadable signal rather than a verdict —
        a failed ``git rev-list`` or a failed activity walk. gc says so loudly and
        exits non-zero from both tails (#1011/#1012) rather than reporting a clean
        bill of health for a root it could not fully read.
        """
        return self.unexamined > 0


@dataclass(frozen=True)
class GcFailure:
    """One Tree the sweep could not delete: its path and the failure text."""

    path: str
    error: str


@dataclass(frozen=True)
class GcResult:
    """What the sweep actually did — the typed result the verb renders.

    ``removed`` holds only the paths that CAME OFF DISK (a Tree already gone —
    a concurrent sweep, a manual ``rm`` — is neither counted nor reported);
    ``failed`` the per-Tree delete failures the sweep continued past; ``kept``
    the untouched bucket; ``total``/``unexamined`` the plan's fleet-view counts,
    carried through so the renderer can warn about an incomplete sweep off the
    result alone.

    There is no ``stale`` list: the partition it mirrored is gone (ADR-0072 —
    :class:`~shipit.tree.cleanup.Cleanup`), so a sweep now reports what it
    removed, what it could not, and what it kept.
    """

    removed: tuple[str, ...]
    failed: tuple[GcFailure, ...]
    kept: int
    total: int
    unexamined: int

    @property
    def judged(self) -> int:
        """How many Trees the rule reached a verdict on (:attr:`GcPlan.judged`)."""
        return self.total - self.unexamined

    @property
    def incomplete(self) -> bool:
        """Whether the fleet was only PARTIALLY judged (:attr:`GcPlan.incomplete`)."""
        return self.unexamined > 0


def plan(
    records: list[TreeRecord],
    *,
    now: float,
    idle_threshold_seconds: float = IDLE_THRESHOLD_SECONDS,
) -> GcPlan:
    """Partition the fleet into the frozen :class:`GcPlan`. PURE.

    Every effectful input arrives as a value (the ``classify`` contract), so the whole
    gc decision — including the ``unexamined`` incomplete-view count — is unit-testable
    without a fleet on disk. The one side effect is ``classify``'s per-Tree decision
    record at DEBUG (ADR-0029).

    PR state is gone from this signature, not merely from the rule. It counted the
    fleet's unreadable signals for #1012's partly-seen report, and that projection was
    load-bearing only while the PR read could SUPPRESS a removal — UNKNOWN kept a Tree,
    so UNKNOWN explained a wrongly-empty sweep. Under ADR-0072 nothing consults it, so
    an UNKNOWN Tree is now removed like any other, and counting it as "skipped" would
    describe a Tree the very same run deleted.

    #1012's PROPERTY is kept, repointed at the signals that inherited the suppressing
    role: :func:`~shipit.tree.cleanup.is_unexamined` — an unreadable ``unpushed`` read
    or a failed activity walk. Those keep a Tree silently today, so they are exactly
    what can now make a blind sweep look like a clean fleet.
    """
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

    The read-only half of gc: scan → the pure :func:`plan`. Nothing on disk is
    mutated; the returned plan is what a ``--dry-run`` renders and what
    :func:`sweep` applies, so the preview can NEVER drift from the action.

    Everything the rule needs now rides the scanned record — the activity walk
    included (:attr:`~shipit.tree.registry.TreeRecord.newest_mtime`) — so this gather
    does no per-Tree work of its own, and projects no PR state: nothing downstream
    reads one any more, neither the rule nor the incomplete-view count (ADR-0072,
    :func:`plan`).
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

    The effectful apply. Deletion is best-effort per Tree: a failed delete (a
    read-only file, a lock, a vanished dir) lands in ``failed`` — WARNING with
    the exception attached, the degraded-but-continuing convention (ADR-0029)
    — and the sweep CONTINUES to the next Tree rather than aborting mid-fleet.
    A path already gone (``remove`` returns ``False``) is skipped silently:
    ``removed`` reflects what actually came off disk, never what was merely
    planned. ``remove`` is injectable so the sweep is unit-testable without a
    real fleet; it defaults to the one reclaim funnel
    (:func:`~shipit.tree.readonly.remove_tree`, which narrates each removal).

    ``on_removed`` is called with each path the instant it comes off disk, and
    is how the destroyed set reaches the operator IN TIME. Deleting a fleet
    takes minutes, and a `GcResult` that only arrives at the end is a record
    the process must survive to hand back: a sweep killed at minute 14 (a
    timeout, or the Ctrl-C a silent multi-minute delete invites) had destroyed
    175 Trees and named none of them (#1011). So the sink is the audit trail,
    the returned :class:`GcResult` merely the summary, and the ORDER matters —
    a path is announced before it is accumulated, never after. A sink that
    raises is not caught here: only the per-Tree ``remove`` is best-effort.

    The sweep's lifecycle milestone (the removed/kept summary) and the
    incomplete-view warning are recorded here — the durable twins of the lines
    the verb renders off the result.
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


def pr_state(record: TreeRecord) -> str | None:
    """The PR's remote state for one Tree. **DEAD — no caller** (ADR-0072).

    It lost the rule under ADR-0072 and its last consumer with it: the
    incomplete-view count it used to feed now reads the signals that actually
    suppress a removal (:func:`plan`), because "UNKNOWN" stopped meaning "kept" the
    moment nothing consulted it. Deleted in WS03 along with the whole ``gh``
    dependency and the ``PrIndex`` batching behind it — kept here only so this WS's
    behaviour change and that WS's pure deletion stay separately reviewable.

    The state the scan ALREADY read (:attr:`~shipit.tree.registry.TreeRecord.pr_state`),
    not a fresh lookup — and not a re-parse of the rendered ``pr`` label either: the
    registry mints both views from ONE PR snapshot. The vocabulary is the typed
    snapshot's own (:attr:`~shipit.gh.HeadPr.display_state`, which normalizes a draft
    open PR to ``"DRAFT"``), so the fleet had ONE state vocabulary. An unreadable state
    maps to ``"UNKNOWN"`` — distinct from ``None`` (no branch / no PR).

    This used to make its OWN ``gh.pr_for_head`` call per Tree, so a sweep paid the
    fleet's PR cost twice — once inside ``scan``, once here, sequentially. That second
    fan-out is what tipped a large sweep past GitHub's hourly GraphQL budget; since
    exhaustion reads as ``UNKNOWN`` and ``UNKNOWN`` then meant keep, ``gc`` exited 0
    having removed nothing while reporting success (issue #1011). The state now rides
    the record, and this is a pure projection.
    """
    return record.pr_state


def live_sessions(records: list[TreeRecord]) -> dict[str, bool]:
    """Per-ephemeral-Tree session liveness. **DEAD — no caller** (ADR-0072).

    Reclaim no longer consults liveness at all: it was a proxy for "is anyone
    working here", and the activity walk measures that directly and more
    truthfully (:func:`shipit.tree.cleanup.classify`). ``plan_fleet`` stopped
    calling this, and it is deleted along with :mod:`shipit.session.liveness`
    in WS03 — kept here only so this WS's behaviour change and that WS's pure
    deletion stay separately reviewable. Do not wire it back in: the probe's
    documented false-negatives are exactly what deleted a live session's Tree
    (#1018).

    For each *ephemeral* Tree, reads its pidfile and decides
    :func:`~shipit.session.liveness.is_live` against the real OS probe; other
    kinds are absent from the map.
    """
    live: dict[str, bool] = {}
    for record in records:
        if layout.tree_kind(record.path) != layout.EPHEMERAL_KIND:
            continue
        session = liveness.read_pidfile(record.path)
        live[record.path] = session is not None and liveness.is_live(
            session, liveness.os_probe
        )
    return live


def provision_shas(records: list[TreeRecord]) -> dict[str, frozenset[Sha]]:
    """Per-ephemeral-Tree provisioning-commit SHAs. **DEAD — no caller** (ADR-0072).

    The unpushed floor no longer carves anything out (#232's exclusion is gone
    with the ephemeral ladder), so nothing reads this. It was already inert in
    production: the ``.git/shipit-provision.json`` it reads has had no writer
    since ADR-0033 retired it. Deleted with :mod:`shipit.tree.provision` in
    WS03; kept here so that deletion stays a pure-deletion PR.

    For each *ephemeral* Tree, reads that record; a missing or unreadable one
    reads as the EMPTY set.
    """
    return {
        record.path: provision.read_provision_shas(record.path)
        for record in records
        if layout.tree_kind(record.path) == layout.EPHEMERAL_KIND
    }
