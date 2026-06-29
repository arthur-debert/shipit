"""``tree/cleanup`` — the pure partition of the Tree fleet into removable/stale/keep.

``classify(records, now, pr_states) -> Cleanup`` is the deep, pure heart of garbage
collection: given the snapshot the registry already scanned (:class:`TreeRecord`s),
the current time, and a per-Tree PR-merge snapshot, it partitions the fleet into the
three buckets the ``gc`` verb acts on. It mirrors ``prstate``'s "snapshot → decision"
idiom (cf. :func:`shipit.prstate.state.evaluate`): everything it needs is an INPUT —
``now`` and ``pr_states`` are passed in, so there is NO clock and NO I/O inside, and
the whole truth table is unit-tested directly. The effectful removal (the verb layer)
consumes this decision; the decision itself never deletes anything.

The partition is **conservative by default** (PRD user story 16/17): a Tree is
deleted ONLY when its loss is provably safe, anything that merely looks abandoned is
surfaced as *stale* (listed, never auto-removed), and everything carrying live or
local work is *kept*.

- **removable** — every safe-to-delete condition holds: the PR is **merged** on the
  remote ∧ the working tree is **clean** ∧ there are **no unpushed commits**
  (``ahead == 0``) ∧ the Tree is **aged** past the threshold. There is nothing left
  to lose, so ``gc`` reclaims it.
- **stale** — the Tree looks abandoned (aged, clean, nothing unpushed) but its PR did
  NOT merge and is no longer in flight (no PR, or a PR closed without merging). That
  is ambiguous — maybe finished elsewhere, maybe dropped — so it is **listed, never
  auto-removed**; a human decides.
- **keep** — everything else: a dirty tree, unpushed commits, an in-flight (open/draft)
  PR, or a Tree too recent to be aged. Live or local work is always protected.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .registry import TreeRecord

#: Default age threshold (seconds): a Tree must be untouched for longer than this
#: before it is even a candidate for removal. Two weeks is deliberately generous —
#: ``gc`` is conservative, and a Tree's mtime bumps on any write, so an actively used
#: Tree never ages. Overridable per call so the boundary is exhaustively table-tested.
DEFAULT_MAX_AGE_SECONDS = 14 * 86_400

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
    ``max_age_seconds`` expects). A missing/unknown unit, a non-positive or
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
    """The fleet partitioned by :func:`classify` — three disjoint, exhaustive buckets.

    Every input record lands in exactly one list. ``gc`` deletes only
    :attr:`removable`, prints :attr:`stale` as a "needs-a-human" list, and never
    touches :attr:`keep`.
    """

    removable: list[TreeRecord]
    stale: list[TreeRecord]
    keep: list[TreeRecord]


def _is_merged(state: str | None) -> bool:
    """``True`` when the PR snapshot says the PR is **merged** on the remote."""
    return (state or "").upper() == "MERGED"


def _is_in_flight(state: str | None) -> bool:
    """``True`` when the PR is still live — open (incl. draft) and thus active work."""
    return (state or "").upper() in {"OPEN", "DRAFT"}


def classify(
    records: list[TreeRecord],
    now: float,
    pr_states: Mapping[str, str | None],
    *,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> Cleanup:
    """Partition ``records`` into removable / stale / keep — a pure, total decision.

    ``now`` is the current epoch time and ``pr_states`` maps a Tree's ``path`` to its
    PR state on the remote (``"MERGED"`` / ``"OPEN"`` / ``"CLOSED"`` / ``"DRAFT"``, or
    ``None`` for no PR). Keying by ``path`` — not branch — is deliberate: two Trees can
    share one branch (PRD), so the path is the only unique handle. Both are INPUTS, so
    this function holds no clock and does no I/O.

    The rules, in precedence order (the first that matches wins):

    1. **dirty or unpushed** (``ahead > 0``) → **keep** — local work is never at risk
       from ``gc``, regardless of age or PR state.
    2. **not aged** (``now - mtime <= max_age_seconds``) → **keep** — too recent to
       reclaim; a Tree's mtime bumps on every write, so a live Tree never ages.
    3. aged, clean, nothing unpushed — decide on the PR:
       - **merged** → **removable** (the one safe-to-delete case);
       - **in flight** (open/draft) → **keep** (protect active review);
       - otherwise (no PR, or closed-without-merge) → **stale** (abandoned-but-ambiguous,
         listed for a human, NEVER auto-removed).
    """
    buckets: dict[str, list[TreeRecord]] = {"removable": [], "stale": [], "keep": []}
    for record in records:
        label = _bucket_for(
            record,
            now=now,
            state=pr_states.get(record.path),
            max_age_seconds=max_age_seconds,
        )
        buckets[label].append(record)
    return Cleanup(
        removable=buckets["removable"],
        stale=buckets["stale"],
        keep=buckets["keep"],
    )


def _bucket_for(
    record: TreeRecord,
    *,
    now: float,
    state: str | None,
    max_age_seconds: float,
) -> str:
    """The bucket name (``"removable"`` / ``"stale"`` / ``"keep"``) for one Tree.

    Encodes the precedence ladder documented on :func:`classify`. Pure: it reads only
    its arguments.
    """
    if record.dirty or record.ahead > 0:
        return "keep"
    aged = (now - record.mtime) > max_age_seconds
    if not aged:
        return "keep"
    if _is_merged(state):
        return "removable"
    if _is_in_flight(state):
        return "keep"
    return "stale"
