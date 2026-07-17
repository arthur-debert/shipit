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
- **``idle``** — how long since anyone touched the Tree: ``now`` less the NEWEST of the
  pruned activity walk (:func:`shipit.tree.activity.newest_mtime`, carried on the
  record as :attr:`~shipit.tree.registry.TreeRecord.newest_mtime`) and HEAD's commit
  stamp. The walk decides; the commit stamp is maxed in and can only ever KEEP, and it
  earns that for one shape the walk structurally cannot see — a commit that only
  DELETES files writes no file whose mtime survives it (:func:`_idle_seconds`).

Three signals. **No PR state, no pidfile, no ``ps`` probe, no kind dispatch** — review,
ephemeral and write Trees reclaim identically, and the ``stale`` bucket is gone with the
ambiguity it managed. The line any new signal has to clear is the one ``idle`` shows: a
stamp may be maxed in where it can only keep a Tree. A signal that can turn a keep into
a delete is a new decision input and belongs in an ADR.

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

The ADR asks for that keep **and** its report, in the same breath — every such signal
"keeps the Tree *and is reported, not swallowed*". :func:`is_unexamined` is the second
half: it names the Trees kept because a signal could not be read rather than because
they were judged, which is what ``gc`` exits non-zero over. A silent conservative keep
is indistinguishable from a clean fleet, and that is the failure that let 526 Trees
accumulate behind a cheerful ``removed 0`` (#1011/#1012).
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
    non-integer magnitude, or empty input raises :class:`ValueError` carrying the
    reason, so a malformed duration is rejected here rather than silently defaulting.

    Raising is the whole contract: this is a pure parser and holds no exit code.
    What a caller does with the ``ValueError`` is the caller's to state — the CLI
    reaches this through :class:`~shipit.verbs._params.DurationParam`, which turns it
    into a click usage error at argv parse (ADR-0030's exit contract).
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
       is not on a remote and nobody has touched it for two days. Idle is the newest of
       the activity walk and HEAD's commit stamp (:func:`_idle_seconds`).

    PR state, session liveness, provisioning SHAs and the Tree's kind are all absent on
    purpose: they were proxies for step 3's question, and the walk answers it directly
    (see the module docstring and ADR-0072). Do NOT reintroduce one as a "backstop" —
    a liveness probe would fire only for a session that writes no file for two days,
    which is precisely the regime its false-negatives inhabit (#1018).

    The bar any future signal must clear is the one :func:`_idle_seconds` states: a
    stamp may only be MAXED INTO step 3, where it can keep a Tree and never delete one.
    A signal that can flip a keep to a removable is a new decision input and belongs in
    an ADR, not in this function.
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
    """How long since anyone last worked in the Tree — ``None`` when unreadable. Pure.

    The newest of the activity walk (:attr:`~shipit.tree.registry.TreeRecord.newest_mtime`,
    ADR-0072's measured signal) and HEAD's committer stamp
    (:attr:`~shipit.tree.registry.TreeRecord.last_commit`). Both ride the record the scan
    already paid for, so this stays pure — no clock, no I/O (ADR-0030).

    ``None`` propagates when the WALK failed: that is the signal that could not be
    established, and an unreadable signal is not evidence of idleness, so the caller
    keeps the Tree (ADR-0072's unknown-is-not-false rule, and ``unpushed_shas``'
    unreadable-reads-conservative discipline). A missing ``last_commit`` is not the same
    thing and does not blank the answer — the walk still measured it.

    **Why the commit stamp is maxed in, when ADR-0072 calls it a proxy.** It is one, and
    it would be wrong ALONE — that is the ADR's point, and why the walk decides. But
    ``max`` cannot make a weak signal dangerous: every extra stamp can only push idle
    DOWN, i.e. only ever KEEP. What it buys is a hole the walk cannot see. A file's mtime
    records writes to that file, and a commit that DELETES a file writes nothing that
    survives it: the entry leaves its parent directory (dirs are not eligible) and the
    commit lands in ``.git`` (pruned). So an agent in an old Tree that deletes a file,
    commits and pushes leaves the Tree clean, fully pushed, and reading its PRE-deletion
    mtime — removable, seconds after real work. That is #1018's own shape (a live Run's
    Tree deleted under it) reappearing through a gap in the measurement, and the ADR's
    asymmetry — a wrongly-kept Tree costs disk, a wrongly-deleted one costs a running
    session — settles which way to resolve it. This restores to the one rule the
    ``max(..., last_commit)`` the WRITE ladder already had and the ephemeral one never
    got (ADR-0072's Context names that missing patch as the root cause).

    The root mtime (:attr:`~shipit.tree.registry.TreeRecord.mtime`) stays out: it is
    strictly weaker than the walk, which already observes everything it does and more
    (ADR-0072 measured it lagging by up to 10h), so it would add cost and no keeps.
    """
    if record.newest_mtime is None:
        return None
    newest = record.newest_mtime
    if record.last_commit is not None:
        newest = max(newest, record.last_commit)
    return now - newest


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


def is_unexamined(record: TreeRecord) -> bool:
    """Whether ``record`` is kept because a signal was UNREADABLE, not because it was
    judged. Pure.

    Not a third bucket (:class:`Cleanup` has two, and ADR-0072 deleted ``stale`` on
    purpose): every unexamined Tree is already in ``keep``. This only asks WHY it is
    there — "the rule ran and said keep" or "the rule could not run". Both are keeps;
    only the second means gc could not see the fleet, and that is a reporting fact, not
    a decision one, so nothing here reaches :func:`_is_removable`.

    The arms mirror the rule's short-circuit exactly, because "the signal that decided
    it" is only meaningful in the order the rule actually consults them:

    - ``dirty``, or a readable non-empty ``unpushed_shas`` → **examined**. The Tree is
      kept on POSITIVE evidence; an unreadable walk underneath changes nothing, because
      the floor already decided and the walk was never reached.
    - ``unpushed_shas is None`` → **unexamined**. The floor fired on a read that failed
      (:func:`_has_local_only_work`), so the keep rests on a git hiccup, not a fact.
    - ``newest_mtime is None`` → **unexamined**. The activity walk failed or found no
      eligible file, so the idle question was never answered
      (:func:`shipit.tree.activity.newest_mtime`).
    - otherwise → **examined**: idle was measured and compared.

    This is the load-bearing half of ADR-0072's unknown-is-not-false rule. The keep is
    the safe half and :func:`_is_removable` already does it; the ADR asks for the other
    half in the same breath — every such signal "keeps the Tree **and is reported, not
    swallowed**". A silent conservative keep is indistinguishable from a clean fleet,
    which is the whole of #1011/#1012: a sweep that reads nothing reports ``removed 0``
    and exits 0, and looks exactly like a sweep with nothing to do.
    """
    if record.dirty:
        return False
    if record.unpushed_shas is None:
        return True
    if record.unpushed_shas:
        return False
    return record.newest_mtime is None
