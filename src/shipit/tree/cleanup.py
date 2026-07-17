"""``tree/cleanup`` — the pure partition of the Tree fleet into removable/keep.

``classify(records, now) -> Cleanup`` is the deep, pure heart of garbage collection:
given the snapshot the registry already scanned (:class:`TreeRecord`s) and the current
time, it splits the fleet into the two buckets the ``gc`` verb acts on. It mirrors
``prstate``'s "snapshot → decision" idiom (cf. :func:`shipit.prstate.state.evaluate`):
everything it needs is an INPUT — ``now`` is passed in, so there is NO clock and NO I/O
inside, and the whole truth table is unit-tested directly. The effectful removal (the
verb layer) consumes this decision; the decision itself never deletes anything. The one
side effect is the per-Tree decision record at DEBUG (LOG02, mirroring
:func:`shipit.prstate.state.evaluate`'s precedent) — the returned partition is
untouched by it.

**One rule, every kind** (ADR-0072)::

    KEEP  if  dirty  ||  unpushed  ||  idle < 48h

- **``dirty``** — the working tree has uncommitted changes. The never-lose-work floor.
- **``unpushed``** — commits that exist on NO remote. Retained deliberately: a clean
  Tree whose commits were never pushed reads as idle, and without this floor it is
  deleted at 48h and those commits die with ``.git``, unrecoverable.
- **``idle``** — ``now - newest file mtime``, over a pruned walk
  (:func:`shipit.tree.activity.newest_mtime`, carried on the record as
  :attr:`~shipit.tree.registry.TreeRecord.newest_mtime`).

Three signals. **No PR state, no pidfile, no ``ps`` probe, no kind dispatch** — review,
ephemeral and write Trees reclaim identically, and the ``stale`` bucket is gone with the
ambiguity it managed.

Why this replaced three ladders (ADR-0072): they measured PROXIES for "is anyone working
here", and every proxy was wrong. ``gc`` deleted the worktree of a LIVE Claude session
(#1018): the ephemeral ladder's last rung read ``now - root_mtime`` — a clock that lags
real activity by up to **10 hours**, because a directory's mtime does not move when an
agent edits under ``src/`` — and read age *only*, never liveness, so the pidfile probe's
false-negative fell straight through to a delete. Measured across the live fleet, the
newest-file-mtime signal separates with **no overlap**: every live Tree < 1h idle, every
dead one > 41h. 48h sits above that band, so the safety property — never delete a live
Tree — holds with a 48× margin over the busiest observed live Tree.

**Unknown is not false — an unreadable signal KEEPS** (ADR-0072). The rule is written
over three booleans and a boolean has nowhere to put "I could not tell", so that gap is
closed explicitly: an unreadable unpushed list (``unpushed_shas is None``) reads as
has-local-work, and an unreadable activity signal (``newest_mtime is None``) reads as
recently active. The asymmetry is the whole point and must be re-derived from the
consequence, never inherited by accident: a wrongly-KEPT Tree costs disk until the next
sweep; a wrongly-DELETED one costs work that no longer exists.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .registry import TreeRecord

logger = logging.getLogger("shipit.tree")

#: The ONE reclaim threshold (seconds): a Tree with no local work that has been IDLE
#: — no file written anywhere in it, build/env dirs pruned
#: (:func:`shipit.tree.activity.newest_mtime`) — for longer than this is removable.
#:
#: 48h replaces the four tunables the three ladders needed (``DEFAULT_MAX_AGE_SECONDS``
#: 14d, ``MERGED_IDLE_GRACE_SECONDS`` 12h, ``EPHEMERAL_HARD_CAP_SECONDS`` 4d,
#: ``EPHEMERAL_GRACE_SECONDS`` 1h). It is not a tuning compromise but a measurement:
#: across the live fleet every live Tree reads < 1h idle and every dead one > 41h, so
#: ANY threshold in that open band separates the fleet perfectly (ADR-0072). 48h sits
#: deliberately ABOVE the band rather than inside it — being above costs only that a
#: Tree idle 41-48h is reclaimed on the next sweep instead of this one, and buys a 48×
#: margin on the only error that is unrecoverable: deleting a Tree someone is using.
#: No grace window is needed: a just-launched Tree is minutes idle, not two days.
#: Overridable per call (``gc --threshold``) so the boundary is exhaustively tested.
IDLE_THRESHOLD_SECONDS = 48 * 3_600

#: The duration suffixes ``parse_duration`` accepts → their length in seconds. Mirrors
#: (and inverts) the units ``shipit.verbs.tree._format_age`` renders, so a Tree's printed
#: age (``3d``) round-trips back through ``--threshold 3d`` to the same boundary.
_DURATION_UNITS = {"d": 86_400, "h": 3_600, "m": 60, "s": 1}


def parse_duration(text: str) -> float:
    """Parse a human duration like ``14d`` / ``36h`` / ``90m`` / ``45s`` into seconds.

    A small pure helper backing ``tree gc --threshold``: the inverse of
    :func:`shipit.verbs.tree._format_age`. Accepts a positive whole number suffixed
    with a single unit — ``d`` days, ``h`` hours, ``m`` minutes, ``s`` seconds — and
    returns the equivalent seconds as a float (the type ``classify``'s
    ``idle_threshold_seconds`` expects). A missing/unknown unit, a non-positive or
    non-integer magnitude, or empty input raises :class:`ValueError`, so a malformed
    ``--threshold`` becomes a clean exit-1 message rather than a silent default.
    """
    raw = text.strip().lower()
    if not raw:
        raise ValueError("duration must not be empty (e.g. 14d, 36h, 90m)")
    unit = raw[-1]
    if unit not in _DURATION_UNITS:
        raise ValueError(
            f"duration {text!r} must end in one of d/h/m/s (e.g. 14d, 36h, 90m)"
        )
    magnitude = raw[:-1]
    if not magnitude.isdigit():
        raise ValueError(
            f"duration {text!r} needs a positive whole number before its "
            "d/h/m/s suffix (e.g. 14d, 36h, 90m)"
        )
    value = int(magnitude)
    if value <= 0:
        raise ValueError(f"duration {text!r} must be positive")
    return float(value * _DURATION_UNITS[unit])


@dataclass(frozen=True)
class Cleanup:
    """The fleet partitioned by :func:`classify` — two disjoint, exhaustive buckets.

    Every input record lands in exactly one list. ``gc`` deletes only
    :attr:`removable` and never touches :attr:`keep`.

    There is no third bucket. The old ``stale`` list — "looks abandoned but we cannot
    prove it, so print it and let a human decide" — existed to manage an ambiguous
    middle, and the measurement that produced ADR-0072 found no such middle: live Trees
    read < 1h idle, dead ones > 41h, with nothing in between. A bucket for the
    undecidable is dead weight once the signal decides.
    """

    removable: list[TreeRecord]
    keep: list[TreeRecord]


def classify(
    records: list[TreeRecord],
    now: float,
    *,
    idle_threshold_seconds: float = IDLE_THRESHOLD_SECONDS,
) -> Cleanup:
    """Partition ``records`` into removable / keep — a pure, total decision.

    ``now`` is the current epoch time; every other input rides the record, so this
    function holds no clock and does no I/O (ADR-0030). ``idle_threshold_seconds``
    overrides the one boundary (:data:`IDLE_THRESHOLD_SECONDS`) so it is exhaustively
    table-tested.

    The rule, applied identically to every Tree regardless of kind (ADR-0072):

    1. **dirty or unpushed → keep** — the never-lose-work floor
       (:func:`_has_local_only_work`). Local work is never at risk from ``gc``, and an
       UNREADABLE unpushed list counts as local work.
    2. **idle unreadable → keep** — :attr:`~shipit.tree.registry.TreeRecord.newest_mtime`
       is ``None`` (the walk failed, or found no eligible file). Unknown is not false;
       a filesystem hiccup must never license a delete.
    3. **idle > the threshold → removable**, else **keep** — the Tree holds nothing that
       is not on a remote and nobody has written a file in it for two days.

    PR state, session liveness, provisioning SHAs and the Tree's kind are all absent on
    purpose: they were proxies for step 3's question, and the walk answers it directly
    (see the module docstring and ADR-0072). Do NOT reintroduce one as a "backstop" —
    a liveness probe would fire only for a session that writes no file for two days,
    which is precisely the regime its false-negatives inhabit (#1018).
    """
    buckets: dict[str, list[TreeRecord]] = {"removable": [], "keep": []}
    for record in records:
        label = (
            "removable"
            if _is_removable(
                record, now=now, idle_threshold_seconds=idle_threshold_seconds
            )
            else "keep"
        )
        # The per-Tree decision record (spray convention, mirroring
        # `prstate.state.evaluate`): DEBUG, with the three signals that drove it — so a
        # surprising delete/keep is reconstructable from the durable log. The log is the
        # only side effect; the returned partition is unchanged.
        logger.debug(
            "gc rule: %s -> %s (dirty=%s, unpushed=%s, idle=%s)",
            record.path,
            label,
            record.dirty,
            record.unpushed,
            _idle_seconds(record, now=now),
            extra={"tree": record.path, "bucket": label},
        )
        buckets[label].append(record)
    return Cleanup(removable=buckets["removable"], keep=buckets["keep"])


def _is_removable(
    record: TreeRecord, *, now: float, idle_threshold_seconds: float
) -> bool:
    """Whether ONE Tree is provably safe to reclaim — the whole rule. Pure.

    ``KEEP if dirty || unpushed || idle < threshold`` (ADR-0072), read the other way
    round. Both unreadable-signal arms answer ``False`` (keep): see :func:`classify`.
    """
    if _has_local_only_work(record):
        return False
    idle = _idle_seconds(record, now=now)
    if idle is None:
        return False
    return idle > idle_threshold_seconds


def _idle_seconds(record: TreeRecord, *, now: float) -> float | None:
    """How long since ANY file in the Tree was written — ``None`` when unreadable. Pure.

    Reads :attr:`~shipit.tree.registry.TreeRecord.newest_mtime`, the pruned walk the
    scan already paid for (:func:`shipit.tree.activity.newest_mtime`, ~1.9ms), so this
    stays pure — no clock, no I/O (ADR-0030).

    ``None`` propagates: it means the signal could not be established, which is NOT
    evidence of idleness, so the caller keeps the Tree (``unpushed_shas``'
    unreadable-reads-conservative discipline). It is deliberately NOT combined with the
    root mtime or the commit stamp: both are strictly weaker (the root's lags by up to
    10h and the commit stamp is blind to an uncommitted session), and a Tree whose walk
    failed is kept anyway, so neither adds safety here.
    """
    if record.newest_mtime is None:
        return None
    return now - record.newest_mtime


def _has_local_only_work(record: TreeRecord) -> bool:
    """Whether ``record`` holds work that exists ONLY in this clone. Pure.

    The never-lose-work floor: uncommitted changes (``dirty``), or commits that exist on
    NO remote at all (``unpushed_shas`` — ``git rev-list HEAD --not --remotes``). An
    UNREADABLE list (``unpushed_shas is None``) reads as "has local work": the safe
    direction, since collapsing unknown to "pushed" would point a git hiccup at data
    loss.

    ``ahead`` is deliberately NOT consulted: it counts commits ahead of the configured
    upstream, and a commit ahead of its upstream but present on some other remote branch
    is on a remote — recoverable, so not this floor's business. ``unpushed_shas`` is the
    upstream-INDEPENDENT question ("does this exist anywhere but here?"), which is the
    one the floor is actually asking, and it alone covers the fresh no-upstream branch
    that ``ahead`` reads as level.

    This is the floor ADR-0072 retains, and it is the one non-obvious keep: a clean Tree
    whose commits were never pushed looks idle, and without it that Tree is deleted at
    48h and its commits die with ``.git`` — unrecoverable, for the price of one
    ``git rev-list``.
    """
    if record.dirty:
        return True
    return record.unpushed_shas is None or bool(record.unpushed_shas)
