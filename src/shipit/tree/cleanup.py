"""``tree/cleanup`` — the pure partition of the Tree fleet into removable/keep.

One rule for every Tree: ``KEEP if dirty || unpushed || idle < 48h``, where an
unreadable signal counts as keep.
See docs/adr/0072-tree-reclaim-is-activity-based.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .registry import TreeRecord

logger = logging.getLogger("shipit.tree")

#: The one reclaim threshold (seconds). Overridable per call (``gc --threshold``).
IDLE_THRESHOLD_SECONDS = 48 * 3_600

#: Duration suffixes ``parse_duration`` accepts → their length in seconds. Inverts the
#: units ``shipit.verbs.tree._format_age`` renders, so a printed age round-trips.
_DURATION_UNITS = {"d": 86_400, "h": 3_600, "m": 60, "s": 1}


def parse_duration(text: str) -> float:
    """Parse a duration like ``14d`` / ``36h`` / ``90m`` / ``45s`` into seconds.

    A positive whole number plus a single d/h/m/s unit; anything else raises
    :class:`ValueError` carrying the reason. Pure parser — it holds no exit code.
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

    ``gc`` deletes only :attr:`removable` and never touches :attr:`keep`.
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

    ``now`` is the current epoch time; every other input rides the record, so there
    is no clock and no I/O here. Applied identically to every Tree regardless of kind:

    1. dirty or unpushed → keep (the never-lose-work floor); an UNREADABLE unpushed
       list counts as local work.
    2. idle unreadable → keep (:func:`is_unexamined`).
    3. idle > threshold → removable, else keep.

    A signal that can flip a keep to a removable is a new decision input and belongs
    in an ADR, not in this function.
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
    """Whether ONE Tree is provably safe to reclaim — the whole rule. Pure."""
    if _has_local_only_work(record):
        return False
    idle = _idle_seconds(record, now=now)
    if idle is None:
        return False
    return idle > idle_threshold_seconds


def _idle_seconds(record: TreeRecord, *, now: float) -> float | None:
    """How long since anyone last worked in the Tree — ``None`` when unreadable. Pure.

    The newest of the activity walk and HEAD's committer stamp. ``None`` propagates
    when EITHER input is unknown — never "fall back to the other": each covers the
    other's blind spot, so a missing one is a hole, not a smaller answer. The stamp is
    only ever MAXED IN, so it can keep a Tree and never delete one; it catches the
    shape the walk cannot see, a commit that only DELETES files and so leaves no file
    whose mtime survives it.
    """
    if record.newest_mtime is None or record.last_commit is None:
        return None
    return now - max(record.newest_mtime, record.last_commit)


def _has_local_only_work(record: TreeRecord) -> bool:
    """Whether ``record`` holds work that exists ONLY in this clone. Pure.

    Uncommitted changes, or commits on no remote at all. An unreadable
    ``unpushed_shas`` reads as "has local work". ``ahead`` is deliberately not
    consulted: it is measured against the configured upstream, while the question
    here is upstream-independent.
    """
    if record.dirty:
        return True
    return record.unpushed_shas is None or bool(record.unpushed_shas)


def is_unexamined(record: TreeRecord) -> bool:
    """Whether ``record`` is kept because a signal was UNREADABLE, not because it was
    judged. Pure.

    Not a third bucket — every unexamined Tree is already in ``keep``; this only asks
    why, and ``gc`` exits non-zero over it so a blind sweep never looks like a clean
    fleet. The arms mirror the rule's short-circuit order.
    """
    if record.dirty:
        return False
    if record.unpushed_shas is None:
        return True
    if record.unpushed_shas:
        return False
    return record.newest_mtime is None or record.last_commit is None
