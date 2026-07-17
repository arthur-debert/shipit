"""``tree/gc`` — garbage collection as a plan + a sweep (ADR-0030).

The gc verb's promoted domain half, split on the boundary contract:

- :func:`plan` is PURE: given the scanned records and every effectful input as
  a value (``now``, PR states, session liveness, provisioning SHAs), it wraps
  :func:`shipit.tree.cleanup.classify`'s partition into a frozen
  :class:`GcPlan` — the exact decision BOTH gc modes act on. ``--dry-run``
  renders this plan; the real sweep consumes it; parity is by construction.
- :func:`plan_fleet` is the effectful GATHER (mirroring ``prstate.fetch``'s
  snapshot idiom): scan the central root, read each Tree's PR state / liveness
  / provisioning record through the existing boundaries, then call the pure
  :func:`plan`. It reads; it never mutates.
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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..session import liveness
from . import layout, provision, registry
from .cleanup import IDLE_THRESHOLD_SECONDS, Cleanup, classify
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
    scanned and ``unknown`` how many had an unreadable PR state. The counts
    travel WITH the partition because both gc tails — the ``--dry-run`` render
    and the real sweep — need them to surface an INCOMPLETE view of the fleet.
    """

    partition: Cleanup
    total: int
    unknown: int

    @property
    def swept(self) -> int:
        """How many Trees the plan saw a readable PR state for."""
        return self.total - self.unknown

    @property
    def incomplete(self) -> bool:
        """Whether the fleet was only PARTIALLY seen.

        True when any Tree's PR state was unreadable — the sign that a repo's
        ``gh`` read failed and so the fleet was only partly seen. gc says so
        loudly and exits non-zero from both tails (#1011/#1012) rather than
        reporting a clean bill of health for a root it could not fully read.
        """
        return self.unknown > 0


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
    the untouched bucket; ``total``/``unknown`` the plan's fleet-view counts,
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
    unknown: int

    @property
    def swept(self) -> int:
        """How many Trees the sweep actually saw a readable PR state for."""
        return self.total - self.unknown

    @property
    def incomplete(self) -> bool:
        """Whether the sweep only PARTIALLY saw the fleet (:attr:`GcPlan.incomplete`)."""
        return self.unknown > 0


def plan(
    records: list[TreeRecord],
    *,
    now: float,
    pr_states: Mapping[str, str | None],
    idle_threshold_seconds: float = IDLE_THRESHOLD_SECONDS,
) -> GcPlan:
    """Partition the fleet into the frozen :class:`GcPlan`. PURE.

    Every effectful input arrives as a value (the ``classify`` contract), so
    the whole gc decision — including the ``unknown`` incomplete-view count —
    is unit-testable without a fleet on disk. The one side effect is
    ``classify``'s per-Tree decision record at DEBUG (ADR-0029).

    ``pr_states`` no longer reaches the RULE — reclaim is activity-based and reads
    no PR state at all (ADR-0072, :func:`~shipit.tree.cleanup.classify`). It is still
    an input here because the ``unknown`` count is projected off it, and that count is
    the whole basis of the partly-seen-fleet report (#1012), which is orthogonal to
    the ladder that went away and still correct: an unreadable repo is a repo whose
    Trees the sweep should say it could not fully vouch for.
    """
    decision = classify(
        records,
        now=now,
        idle_threshold_seconds=idle_threshold_seconds,
    )
    unknown = sum(1 for state in pr_states.values() if state == "UNKNOWN")
    return GcPlan(partition=decision, total=len(records), unknown=unknown)


def plan_fleet(
    root: str | Path, *, idle_threshold_seconds: float = IDLE_THRESHOLD_SECONDS
) -> GcPlan:
    """Scan the central root and build the :class:`GcPlan` — the effectful gather.

    The read-only half of gc: scan → the pure :func:`plan`. Nothing on disk is
    mutated; the returned plan is what a ``--dry-run`` renders and what
    :func:`sweep` applies, so the preview can NEVER drift from the action.

    Everything the rule needs now rides the scanned record — the activity walk
    included (:attr:`~shipit.tree.registry.TreeRecord.newest_mtime`) — so this gather
    does no per-Tree work of its own. The PR state it still projects
    (:func:`pr_state`, free off the record) feeds only the incomplete-view count, not
    the decision (ADR-0072).
    """
    records = registry.scan(root)
    pr_states = {record.path: pr_state(record) for record in records}
    return plan(
        records,
        now=time.time(),
        pr_states=pr_states,
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
    if gc_plan.unknown:
        logger.warning(
            "tree gc swept %d of %d; %d skipped (PR state unknown — incomplete view "
            "of the fleet)",
            gc_plan.total - gc_plan.unknown,
            gc_plan.total,
            gc_plan.unknown,
        )
    return GcResult(
        removed=tuple(removed),
        failed=tuple(failed),
        kept=kept,
        total=gc_plan.total,
        unknown=gc_plan.unknown,
    )


def pr_state(record: TreeRecord) -> str | None:
    """The PR's remote state (``"MERGED"`` / ``"OPEN"`` / ``"CLOSED"`` /
    ``"UNKNOWN"`` …) for one Tree — read straight off the scanned ``record``.

    The state the scan ALREADY read (:attr:`~shipit.tree.registry.TreeRecord.pr_state`),
    not a fresh lookup — and not a re-parse of the rendered ``pr`` label either: the
    registry mints both views from ONE PR snapshot. The vocabulary is the typed
    snapshot's own (:attr:`~shipit.gh.HeadPr.display_state`, which normalizes a draft
    open PR to ``"DRAFT"``), so the fleet has ONE state vocabulary and
    ``cleanup.classify``'s draft branch is reachable. An unreadable state maps to
    ``"UNKNOWN"`` — distinct from ``None`` (no branch / no PR) — so gc can both treat
    it conservatively and warn about it.

    This used to make its OWN ``gh.pr_for_head`` call per Tree, so a sweep paid the
    fleet's PR cost twice — once inside ``scan``, once here, sequentially. That second
    fan-out is what tipped a large sweep past GitHub's hourly GraphQL budget; since
    exhaustion reads as ``UNKNOWN`` and ``UNKNOWN`` means keep, ``gc`` then exited 0
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
