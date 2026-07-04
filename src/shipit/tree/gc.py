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
  Trees and return a typed :class:`GcResult` of what actually happened. No
  printing anywhere in this module — the verb renders the result; the durable
  log twins (the per-failure WARNING, the sweep milestone, the incomplete-view
  warning; ADR-0029) live here with the effect they narrate.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .. import gh
from ..session import liveness
from . import layout, provision, registry
from .cleanup import DEFAULT_MAX_AGE_SECONDS, Cleanup, classify
from .readonly import remove_tree
from .registry import TreeRecord

if TYPE_CHECKING:
    from ..identity import Sha

logger = logging.getLogger("shipit.tree")


@dataclass(frozen=True)
class GcPlan:
    """The frozen gc decision: the fleet's partition plus the sweep's context.

    ``partition`` is :func:`~shipit.tree.cleanup.classify`'s three-bucket
    :class:`~shipit.tree.cleanup.Cleanup`; ``total`` is how many Trees were
    scanned and ``unknown`` how many had an unreadable PR state. The counts
    travel WITH the partition because both gc tails — the ``--dry-run`` render
    and the real sweep — need them to surface an INCOMPLETE view of the fleet.
    """

    partition: Cleanup
    total: int
    unknown: int


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
    ``failed`` the per-Tree delete failures the sweep continued past;
    ``stale``/``kept`` the untouched buckets; ``total``/``unknown`` the plan's
    fleet-view counts, carried through so the renderer can warn about an
    incomplete sweep off the result alone.
    """

    removed: tuple[str, ...]
    failed: tuple[GcFailure, ...]
    stale: tuple[str, ...]
    kept: int
    total: int
    unknown: int

    @property
    def swept(self) -> int:
        """How many Trees the sweep actually saw a readable PR state for."""
        return self.total - self.unknown


def plan(
    records: list[TreeRecord],
    *,
    now: float,
    pr_states: Mapping[str, str | None],
    live_sessions: Mapping[str, bool] | None = None,
    provision_shas: Mapping[str, frozenset[Sha]] | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> GcPlan:
    """Partition the fleet into the frozen :class:`GcPlan`. PURE.

    Every effectful input arrives as a value (the ``classify`` contract), so
    the whole gc decision — including the ``unknown`` incomplete-view count —
    is unit-testable without a fleet on disk. The one side effect is
    ``classify``'s per-Tree decision record at DEBUG (ADR-0029).
    """
    decision = classify(
        records,
        now=now,
        pr_states=pr_states,
        max_age_seconds=max_age_seconds,
        live_sessions=live_sessions,
        provision_shas=provision_shas,
    )
    unknown = sum(1 for state in pr_states.values() if state == "UNKNOWN")
    return GcPlan(partition=decision, total=len(records), unknown=unknown)


def plan_fleet(
    root: str, *, max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS
) -> GcPlan:
    """Scan the central root and build the :class:`GcPlan` — the effectful gather.

    The read-only half of gc: scan → per-Tree PR state / liveness /
    provisioning reads (each through its existing boundary) → the pure
    :func:`plan`. Nothing on disk is mutated; the returned plan is what a
    ``--dry-run`` renders and what :func:`sweep` applies, so the preview can
    NEVER drift from the action.
    """
    records = registry.scan(root)
    pr_states = {record.path: pr_state(record) for record in records}
    return plan(
        records,
        now=time.time(),
        pr_states=pr_states,
        live_sessions=live_sessions(records),
        provision_shas=provision_shas(records),
        max_age_seconds=max_age_seconds,
    )


def sweep(gc_plan: GcPlan, *, remove: Callable[[str], bool] = remove_tree) -> GcResult:
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

    The sweep's lifecycle milestone (the removed/stale/kept summary) and the
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
        removed.append(record.path)
    stale = tuple(record.path for record in gc_plan.partition.stale)
    kept = len(gc_plan.partition.keep)
    logger.info("tree gc removed %d, stale %d, kept %d", len(removed), len(stale), kept)
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
        stale=stale,
        kept=kept,
        total=gc_plan.total,
        unknown=gc_plan.unknown,
    )


def pr_state(record: TreeRecord) -> str | None:
    """The PR's remote state (``"MERGED"`` / ``"OPEN"`` / ``"CLOSED"`` /
    ``"UNKNOWN"`` …) for one Tree.

    Reads through the same ``gh`` boundary the registry uses, from inside the
    clone, so gc sees the authoritative merge state rather than re-parsing the
    rendered label. The vocabulary is the typed snapshot's own
    (:attr:`~shipit.gh.HeadPr.display_state`, which normalizes a draft open PR
    to ``"DRAFT"``), so the fleet has ONE state vocabulary and
    ``cleanup.classify``'s draft branch is reachable. An unreadable state
    (``gh.pr_for_head`` returns :data:`~shipit.gh.UNKNOWN` — a gh failure or a
    malformed payload the adapter's construction boundary rejected) maps to
    ``"UNKNOWN"`` — distinct from ``None`` (no branch / no PR) — so gc can both
    treat it conservatively and warn about it.
    """
    if not record.branch:
        return None
    pr = gh.pr_for_head(record.branch, cwd=record.path)
    if pr is gh.UNKNOWN:
        return "UNKNOWN"
    if pr is None:
        return None
    return pr.display_state


def live_sessions(records: list[TreeRecord]) -> dict[str, bool]:
    """Per-ephemeral-Tree session liveness — the ``live_sessions`` input
    :func:`plan` needs.

    For each *ephemeral* Tree (the only kind whose ladder consults liveness),
    read its pidfile and decide :func:`~shipit.session.liveness.is_live`
    against the real OS probe. No pidfile / an unreadable one reads as NOT
    live — the safe direction, because the pure ladder still protects such a
    Tree through its liveness-independent rungs (the dirty/unpushed floor, the
    grace window). Other kinds are simply absent from the map (``classify``
    defaults them to not-live, and their ladders never look).
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
    """Per-ephemeral-Tree provisioning-commit SHAs — the exclusion input.

    For each *ephemeral* Tree (the only ladder that consults the exclusion,
    #232), read the ``.git/shipit-provision.json`` record its birth
    provisioning wrote. A missing or unreadable record reads as the EMPTY set
    — nothing excluded, so the pure ladder's unpushed floor keeps the Tree:
    the safe direction. Other kinds are simply absent from the map
    (``classify`` defaults them to empty, and their ladders never exclude).
    """
    return {
        record.path: provision.read_provision_shas(record.path)
        for record in records
        if layout.tree_kind(record.path) == layout.EPHEMERAL_KIND
    }
