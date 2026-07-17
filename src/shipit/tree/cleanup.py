"""``tree/cleanup`` — the pure partition of the Tree fleet into removable/stale/keep.

``classify(records, now, pr_states) -> Cleanup`` is the deep, pure heart of garbage
collection: given the snapshot the registry already scanned (:class:`TreeRecord`s),
the current time, and a per-Tree PR-merge snapshot, it partitions the fleet into the
three buckets the ``gc`` verb acts on. It mirrors ``prstate``'s "snapshot → decision"
idiom (cf. :func:`shipit.prstate.state.evaluate`): everything it needs is an INPUT —
``now`` and ``pr_states`` are passed in, so there is NO clock and NO I/O inside, and
the whole truth table is unit-tested directly. The effectful removal (the verb layer)
consumes this decision; the decision itself never deletes anything. The one side
effect is the per-Tree decision record at DEBUG (LOG02, mirroring
:func:`shipit.prstate.state.evaluate`'s precedent) — the returned partition is
untouched by it.

The partition is **conservative by default** (PRD user story 16/17): a Tree is
deleted ONLY when its loss is provably safe, anything that merely looks abandoned is
surfaced as *stale* (listed, never auto-removed), and everything carrying live or
local work is *kept*.

The three buckets below describe the **write** ladder — the ordinary work Tree. The two
other kinds (review, ephemeral) are decided FIRST by their own ladders, documented after
them; neither ever lands in *stale*, and neither reads PR state the way these do.

- **removable** — every safe-to-delete condition holds: the PR is **merged** on the
  remote ∧ the working tree is **clean** ∧ there are **no unpushed commits**
  (neither ahead of an upstream nor on no remote at all — ``_has_local_only_work``)
  ∧ the Tree has been **idle** — no root-level write and no commit
  (:func:`_idle_seconds`) — longer than the short merged-idle grace window
  (:data:`MERGED_IDLE_GRACE_SECONDS`). The work is on the remote; there is nothing
  left to lose, so ``gc`` reclaims it. The abandonment age threshold does NOT gate
  this case (#1009): a merged Tree's safety is already provable, and the two-week
  threshold vetoing it parked a fortnight of finished work.
- **stale** — the Tree looks abandoned (aged, clean, nothing unpushed) but its PR did
  NOT merge and is no longer in flight (no PR, a PR closed without merging, or a state
  that could not be read — UNKNOWN). That is ambiguous — maybe finished elsewhere, maybe dropped — so it is **listed, never
  auto-removed**; a human decides. Age is the ONLY abandonment signal for these
  unmerged shapes, so ``max_age_seconds`` still governs them.
- **keep** — everything else: a dirty tree, unpushed commits, an in-flight (open/draft)
  PR, a merged Tree still inside its idle grace window, or an UNMERGED Tree too recent
  to be aged. Live or local work is always protected.

A **shared read-only (reviewer) Tree** (ADR-0018; ``…/review/<branch>``) is a
distinct reclaim case the precedence ladder handles FIRST. It carries no local work
(read-only, ``chmod``'d) and is shared across reviewers, so age / dirty / unpushed do
not apply; instead it is **removable when its PR is merged or closed AND no reviewer
is still live against it** (the ``live_reviews`` input), and **kept** otherwise (an
in-flight PR, an unreadable state, or a live reviewer). It never lands in *stale*:
a cheap shared clone is either provably reclaimable or kept.

An **ephemeral session Tree** (ADR-0027; ``…/ephemeral/<id>``) is the third distinct
case: the coordinator's own per-launch workspace. It usually has NO PR (the standard
ladder would strand it in *stale* forever) and is often CLEAN (a planning session
that never committed — "clean + aged" alone would delete a Tree out from under a
live idle session), so its reclaim turns on a **liveness signal** (the
``live_sessions`` input, fed from the pidfile ``session/liveness`` reads) plus
liveness-INDEPENDENT backstops: a hard time cap so a stale pidfile can never strand
a Tree forever, and a grace window so a just-launched session is not raced before
its pidfile lands. Reclaim here is liveness-based, **not PR-based** (ADR-0027): apart
from the merged fast path, PR state is never consulted — an UNKNOWN (unreadable) state
does NOT hold a session Tree, by design. Like a review Tree it is binary — removable or
kept, never *stale*: a disposable per-launch clone is either provably safe to reclaim
or kept.
Its never-lose-work floor additionally excludes **exactly the provisioning
commit(s)** recorded at the Tree's birth (:mod:`shipit.tree.provision`, the
``provision_shas`` input): a managed-set drift window makes provisioning commit the
reconcile on every fresh Tree, and without the exclusion that shipit-made,
no-remote commit would hold an abandoned session Tree forever (#232).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .layout import EPHEMERAL_KIND, REVIEW_KIND, tree_kind
from .registry import TreeRecord

if TYPE_CHECKING:
    from ..identity import Sha

logger = logging.getLogger("shipit.tree")

#: Default age threshold (seconds): an UNMERGED Tree must be IDLE (:func:`_idle_seconds`
#: — no root-level write, no commit) for longer than this before it is even a candidate
#: for reclaim. Two weeks is deliberately generous — ``gc`` is conservative, and an
#: actively used Tree keeps refreshing that signal, so it never ages.
#: Overridable per call so the boundary is exhaustively table-tested.
#: It governs only the shapes where age is the SOLE abandonment signal (no PR / closed /
#: UNKNOWN); a merged Tree is decided before it, on the IDLE window
#: :data:`MERGED_IDLE_GRACE_SECONDS` (#1009).
DEFAULT_MAX_AGE_SECONDS = 14 * 86_400

#: The write ladder's **idle** window (seconds) for a MERGED Tree: clean, fully pushed
#: and merged, it is removable once it has been IDLE for longer than this. The clock is
#: time since the Tree's last ACTIVITY — the newest of its root mtime and ``HEAD``'s
#: committer timestamp (:func:`_idle_seconds`) — NOT time since the PR merged: the
#: question this window exists to answer is "is an agent still working in this Tree?",
#: and time-since-merge does not answer it. A write Tree has NO liveness signal (unlike
#: the ephemeral kind, which has its pidfile), so observed activity stands in for one.
#: Hours, not weeks: once the merge is on the remote there is nothing left to lose, so
#: the two-week age gate added no safety, only 421 parked Trees (#1009).
#:
#: The commit timestamp is what makes this window real rather than nominal (codex
#: review): root mtime alone bumps only on root-level churn, so it does not observe an
#: agent editing and committing under ``src/`` at all. Together the two cover the gap
#: this window exists for — the interval between a push and the next edit, the one
#: moment a live agent's Tree reads clean and fully pushed and so reaches this rung at
#: all. Overridable per call so the boundary is exhaustively table-tested.
MERGED_IDLE_GRACE_SECONDS = 12 * 3_600

#: The ephemeral ladder's HARD time cap (seconds): past this age a clean, fully-
#: pushed session Tree is removable EVEN IF its pidfile claims live (ADR-0027 rung
#: 4). ~4 days is "abandoned in practice" for an idle session, and the override is
#: the escape hatch that keeps a wrong/forgotten/stale pidfile from stranding a
#: Tree forever — liveness delays reclaim, it never vetoes it indefinitely.
EPHEMERAL_HARD_CAP_SECONDS = 4 * 86_400

#: The ephemeral ladder's grace window (seconds): a NOT-live, clean, pushed session
#: Tree younger than this is still kept (rung 5), so a just-launched session is not
#: raced by a gc sweep in the moments before its ``SessionStart`` pidfile lands.
EPHEMERAL_GRACE_SECONDS = 3_600

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


def _is_closed(state: str | None) -> bool:
    """``True`` when the PR was **closed without merging** (a terminal, non-merge state).

    Together with :func:`_is_merged` this is the ``merged/closed`` terminal condition
    a shared review Tree's reclaim turns on (ADR-0018): once the PR is done — merged or
    abandoned — the read-only clone has nothing left to serve.
    """
    return (state or "").upper() == "CLOSED"


def _is_unknown(state: str | None) -> bool:
    """``True`` when the PR state could not be read (``"UNKNOWN"`` in the vocabulary).

    The verb layer maps an unreadable :data:`~shipit.gh.UNKNOWN` snapshot to the state
    string ``"UNKNOWN"``.

    This predicate has exactly ONE caller — the **write** ladder (:func:`_bucket_for`),
    where UNKNOWN is neither merged nor in flight and so falls to **stale** alongside
    the other ambiguous shapes, never ``removable``. Recognising it there explicitly
    keeps that safety legible rather than leaving it to the catch-all. The claim is
    scoped to that ladder and does NOT generalise to the fleet — the other two kinds
    reach their verdicts without ever calling this:

    - **review** (:func:`_review_bucket`) — binary, never *stale*: it reclaims only on a
      terminal (merged/closed) PR, so an UNKNOWN state simply fails that test → **keep**.
    - **ephemeral** (:func:`_ephemeral_bucket`) — decides on **liveness and age, not PR
      state** (ADR-0027), so a not-live, clean, fully pushed session Tree past its grace
      window IS ``removable`` while its PR state is unreadable. That is by design; do NOT
      "fix" it by adding an UNKNOWN rung there. PR state is only ever that ladder's fast
      path to reclaim (rung 2, ``_is_merged``); its rung 1 — dirty or unpushed → keep —
      is an absolute floor, so anything reaching the liveness/age rungs is already clean
      AND fully pushed, its work safe on a remote, with nothing left for PR state to
      protect. An UNKNOWN rung would instead strand every session Tree whenever the
      ``gh`` budget is drained.

    ``gc`` separately *warns* whenever any UNKNOWN was seen, on every kind.
    """
    return (state or "").upper() == "UNKNOWN"


def classify(
    records: list[TreeRecord],
    now: float,
    pr_states: Mapping[str, str | None],
    *,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
    merged_idle_grace_seconds: float = MERGED_IDLE_GRACE_SECONDS,
    live_reviews: Mapping[str, bool] | None = None,
    live_sessions: Mapping[str, bool] | None = None,
    provision_shas: Mapping[str, frozenset[Sha]] | None = None,
    hard_cap_seconds: float = EPHEMERAL_HARD_CAP_SECONDS,
    grace_seconds: float = EPHEMERAL_GRACE_SECONDS,
) -> Cleanup:
    """Partition ``records`` into removable / stale / keep — a pure, total decision.

    ``now`` is the current epoch time and ``pr_states`` maps a Tree's ``path`` to its
    PR state on the remote (``"MERGED"`` / ``"OPEN"`` / ``"CLOSED"`` / ``"DRAFT"``,
    ``"UNKNOWN"`` when the state could not be read, or ``None`` for no PR). Keying by
    ``path`` — not branch — is deliberate: two Trees can
    share one branch (PRD), so the path is the only unique handle. ``live_reviews``
    maps a *review* Tree's ``path`` to whether a reviewer Run is still live against it
    (default: none live); ``live_sessions`` maps an *ephemeral* Tree's ``path`` to
    whether its recorded Claude session is still live (the ``session/liveness``
    pidfile decision; default: none live); ``provision_shas`` maps an *ephemeral*
    Tree's ``path`` to the commit SHAs its provisioning recorded at birth
    (:mod:`shipit.tree.provision` — default: none, so nothing is excluded from the
    unpushed floor and every local-only commit protects). All are INPUTS, so this
    function holds no clock and does no I/O. ``merged_idle_grace_seconds`` (the write
    ladder's idle window for a merged Tree) and ``hard_cap_seconds`` / ``grace_seconds``
    (the ephemeral ladder's time backstops) override those boundaries so each is
    exhaustively table-tested.

    The rules, in precedence order (the first that matches wins):

    0. **shared read-only (reviewer) Tree** (``…/review/<branch>``) — a DISTINCT
       reclaim case decided before the write-Tree ladder (ADR-0018). It holds no local
       work, so age / dirty / unpushed do not apply: it is **removable** when its PR is
       **merged or closed** ∧ **no reviewer is live** against it, and **keep**
       otherwise (in-flight PR, unreadable state, or a live reviewer). It is never
       *stale*.
    0b. **ephemeral session Tree** (``…/ephemeral/<id>``) — the coordinator's own
       per-launch workspace, decided by its own five-rung ladder
       (:func:`_ephemeral_bucket`, ADR-0027): liveness-gated, with liveness-
       independent backstops. Never *stale*.
    1. **dirty or unpushed** → **keep** — local work is never at risk from ``gc``,
       regardless of age or PR state. "Unpushed" is :func:`_has_local_only_work`'s
       upstream-INDEPENDENT definition — ``ahead > 0`` *or* commits on no remote at
       all (``unpushed``) — because a branch with no tracking upstream reads
       ``ahead == 0`` while still holding local-only commits (e.g. extra commits
       after the remote branch was deleted on merge); ``ahead`` alone would age
       such a Tree into ``removable`` and lose them (codex review).
    2. **merged PR** (clean, nothing unpushed) → **removable** once the Tree has been
       **idle** longer than the merged-idle grace window, else **keep**. Idle is
       :func:`_idle_seconds` — time since the NEWEST of the root mtime and ``HEAD``'s
       committer stamp, the pair that actually observes an agent at work. Decided
       BEFORE the abandonment age gate (#1009): the work is on the remote, so the merge
       already proves the loss is safe and age adds nothing — gating this on
       ``max_age_seconds`` parked a fortnight of finished work (421 of a 503-Tree
       fleet). The window's clock is activity, not time since the merge: it asks "is an
       agent still working here?", and a write Tree has no liveness signal to ask
       directly. It covers the one gap the floor above leaves — an agent between a push
       and its next edit, whose Tree momentarily reads clean and fully pushed. This
       mirrors the ephemeral ladder's rung 2 (ADR-0027), which already decides
       ``_is_merged`` ahead of its liveness/age rungs.
    3. **not aged** (``idle <= max_age_seconds``, the same :func:`_idle_seconds`
       clock) → **keep** — too recent to call abandoned. Reaching here the PR is
       UNMERGED, which is the only shape age governs.
    4. aged, clean, nothing unpushed, unmerged — decide on the PR:
       - **in flight** (open/draft) → **keep** (protect active review);
       - otherwise (no PR, closed-without-merge, or **UNKNOWN**) → **stale**
         (abandoned-but-ambiguous, listed for a human, NEVER auto-removed). An UNKNOWN
         state is unreadable, not provably abandoned, so it is conservatively stale and
         ``gc`` raises an incomplete-sweep warning for it.
    """
    reviews = live_reviews or {}
    sessions = live_sessions or {}
    provisioned = provision_shas or {}
    buckets: dict[str, list[TreeRecord]] = {"removable": [], "stale": [], "keep": []}
    for record in records:
        label = _bucket_for(
            record,
            now=now,
            state=pr_states.get(record.path),
            max_age_seconds=max_age_seconds,
            merged_idle_grace_seconds=merged_idle_grace_seconds,
            reviewer_live=reviews.get(record.path, False),
            session_live=sessions.get(record.path, False),
            provision=provisioned.get(record.path, frozenset()),
            hard_cap_seconds=hard_cap_seconds,
            grace_seconds=grace_seconds,
        )
        # The ladder's per-Tree decision record (spray convention, mirroring
        # `prstate.state.evaluate`): DEBUG, with the inputs that drove the rung —
        # so a surprising delete/keep is reconstructable from the durable log.
        # The log is the only side effect; the returned partition is unchanged.
        logger.debug(
            "gc ladder: %s -> %s (kind=%s, pr=%s, dirty=%s, unpushed=%s)",
            record.path,
            label,
            tree_kind(record.path),
            pr_states.get(record.path),
            record.dirty,
            record.unpushed,
            extra={"tree": record.path, "bucket": label},
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
    merged_idle_grace_seconds: float,
    reviewer_live: bool,
    session_live: bool,
    provision: frozenset[Sha],
    hard_cap_seconds: float,
    grace_seconds: float,
) -> str:
    """The bucket name (``"removable"`` / ``"stale"`` / ``"keep"``) for one Tree.

    Encodes the precedence ladder documented on :func:`classify`, dispatching on the
    Tree's kind (:func:`~shipit.tree.layout.tree_kind` — the leaf's parent segment,
    the naming source of truth; there is no manifest, so the path IS the signal).
    Pure: it reads only its arguments.
    """
    kind = tree_kind(record.path)
    if kind == REVIEW_KIND:
        return _review_bucket(state, reviewer_live=reviewer_live)
    if kind == EPHEMERAL_KIND:
        return _ephemeral_bucket(
            record,
            now=now,
            state=state,
            live=session_live,
            provision=provision,
            hard_cap_seconds=hard_cap_seconds,
            grace_seconds=grace_seconds,
        )
    if _has_local_only_work(record):
        return "keep"
    idle = _idle_seconds(record, now=now)
    if idle is None:
        # The activity signal is unreadable — conservatively ACTIVE. A git hiccup
        # must never license a delete (`unpushed_shas`' precedent, codex review).
        return "keep"
    if _is_merged(state):
        # Decided BEFORE the abandonment age gate (#1009): the merge already proves
        # the loss is safe, so the only thing holding the Tree is the short idle
        # window standing in for the liveness signal a write Tree does not have.
        return "removable" if idle > merged_idle_grace_seconds else "keep"
    # Unmerged from here down — the shapes where idleness IS the abandonment signal.
    if idle <= max_age_seconds:
        return "keep"
    if _is_in_flight(state):
        return "keep"
    # UNKNOWN (state unreadable) lands here alongside no-PR / closed-without-merge:
    # all ambiguous, all conservatively STALE (listed, never auto-removed). UNKNOWN is
    # called out explicitly so "on THIS ladder, never removable when we couldn't even
    # read the PR" is a documented invariant, not an accident of the catch-all. The
    # invariant is the write ladder's alone: review and ephemeral Trees never consult
    # `_is_unknown` and reach their own verdicts (see its docstring).
    if _is_unknown(state):
        return "stale"
    return "stale"


def _idle_seconds(record: TreeRecord, *, now: float) -> float | None:
    """How long the write Tree has been IDLE — ``None`` when that is unreadable. Pure.

    Idle is measured from the NEWEST of the Tree's two activity signals, because
    neither alone observes an agent at work:

    - ``mtime`` — the clone ROOT's mtime. A directory's mtime bumps only when an entry
      is added or removed in THAT directory, so it sees root-level churn and checkout
      activity but NOT the ordinary shape of agent work: editing a file under ``src/``,
      staging it, and committing it all leave it untouched.
    - ``last_commit`` — ``HEAD``'s committer timestamp, which moves exactly when the
      agent commits (and on amend/rebase). This is what closes the gap the window
      exists for: an agent editing without committing is ``dirty`` and never reaches
      this rung; an agent that HAS committed has a fresh stamp here. Pushing does not
      change it, so the post-push/pre-next-edit interval — the one moment a live
      agent's Tree reads clean and fully pushed — is genuinely covered.

    ``last_commit is None`` means the stamp could not be read, which is NOT evidence of
    idleness: it returns ``None`` so the caller keeps the Tree (``unpushed_shas``'
    unreadable-reads-conservative discipline). Both values arrive ON the record, so
    this stays pure — no clock, no I/O (ADR-0030).
    """
    if record.last_commit is None:
        return None
    return now - max(record.mtime, record.last_commit)


def _review_bucket(state: str | None, *, reviewer_live: bool) -> str:
    """The bucket for a shared read-only (reviewer) Tree (ADR-0018). Pure.

    Reclaim is binary — ``removable`` or ``keep``, never ``stale``: a review Tree is a
    cheap shared clone carrying no local work, so it is either provably done with or
    kept. It is **removable** only when its PR has reached a terminal state (merged or
    closed) AND no reviewer Run is still live against it; a live reviewer, an in-flight
    PR (open/draft), an unreadable (UNKNOWN) state, or no PR at all all → **keep**
    (never reclaim a clone a reviewer might still be reading, and never guess from an
    unreadable PR).
    """
    if reviewer_live:
        return "keep"
    if _is_merged(state) or _is_closed(state):
        return "removable"
    return "keep"


def _ephemeral_bucket(
    record: TreeRecord,
    *,
    now: float,
    state: str | None,
    live: bool,
    provision: frozenset[Sha],
    hard_cap_seconds: float,
    grace_seconds: float,
) -> str:
    """The bucket for an ephemeral session Tree (ADR-0027). Pure.

    Binary — ``removable`` or ``keep``, never ``stale``: a session Tree is a
    disposable per-launch clone, so it is either provably safe to reclaim or kept.
    Reclaim is **liveness-based, not PR-based** (ADR-0027): ``state`` is read ONCE, as
    the merged fast path at rung 2, and never again — rungs 3-5 are pure liveness and
    age. So an UNKNOWN (unreadable) PR state does not appear on this ladder and cannot
    block reclaim, deliberately: rung 1 is an absolute floor, so a Tree reaching rungs
    3-5 is already clean AND fully pushed and PR state has nothing left to protect. An
    UNKNOWN rung here would strand every session Tree whenever the ``gh`` budget is
    drained (cf. :func:`_is_unknown`, whose "never removable" holds on the write ladder
    only).
    The five rungs, first match wins:

    1. **dirty or unpushed → keep** — the absolute floor; local work is never at
       risk. "Unpushed" here is :func:`_has_local_only_work`'s upstream-INDEPENDENT
       definition (commits on no remote at all), because a fresh ``ephemeral/<id>``
       branch has no upstream and ``ahead`` would read it as level — a missing
       upstream must never by itself block reclaim, and never by itself permit it.
       One carve-out (#232): the commit SHAs *provisioning itself* recorded at the
       Tree's birth (``provision``) are excluded from the count — a managed-set
       drift window commits the reconcile on every fresh Tree, and that shipit-made
       commit is not the session's work, so it must not strand the Tree past every
       liveness-independent backstop. The exclusion is exact-SHA-only: any OTHER
       local-only commit, an unreadable list, or a rebased/amended (SHA-changed)
       provisioning commit still keeps — the floor stays absolute for real work.
    2. **merged PR → removable** — the session's branch moved to real work and it
       merged; "done" is provable, nothing is lost (reuses the standard vocabulary).
    3. **live ∧ younger than the hard cap → keep** — the pidfile says the session
       process is still running; an idle-but-live session keeps its workspace.
    4. **past the hard cap (clean, pushed) → removable EVEN IF live** — the escape
       hatch: a clean session idle for days is abandoned in practice, so a wrong or
       stale pidfile can never strand a Tree forever.
    5. **else (not live, clean, pushed) → removable past the grace window** — the
       session is provably gone and nothing local remains; the grace window keeps a
       just-launched Tree from being raced before its pidfile lands.
    """
    if _has_local_only_work(record, exclude=provision):
        return "keep"
    if _is_merged(state):
        return "removable"
    age = now - record.mtime
    if live and age <= hard_cap_seconds:
        return "keep"
    if age > hard_cap_seconds:
        return "removable"
    return "removable" if age > grace_seconds else "keep"


def _has_local_only_work(
    record: TreeRecord, *, exclude: frozenset[Sha] = frozenset()
) -> bool:
    """Whether ``record`` holds work that exists ONLY in this clone. Pure.

    The never-lose-work floor shared by the write AND ephemeral ladders (the
    review ladder needs none: a read-only shared clone holds no local work by
    construction): uncommitted changes (``dirty``), commits on NO remote at all
    (``unpushed_shas`` — the upstream-independent list, which alone covers the
    fresh no-upstream ``ephemeral/<id>`` branch that ``ahead`` reads as level), or
    commits ahead of a configured upstream (``ahead``). An UNREADABLE list
    (``unpushed_shas is None``) reads as "has local work": the safe direction —
    collapsing unknown to "pushed" would point a git hiccup at data loss.

    ``exclude`` is the ephemeral ladder's provisioning-commit carve-out (#232):
    SHAs known to be shipit's own managed-set reconcile, not the session's work.
    They are subtracted from the local-only list, and — because an excluded commit
    also sits ahead of the upstream it was cut from — from the ``ahead`` reading:
    ``ahead`` is local work only when it exceeds what the exclusion accounts for
    (an ahead count it does NOT explain may be commits pushed to some other
    branch, which conservatively still keeps, exactly as before). With the default
    empty ``exclude`` (every non-ephemeral caller), the semantics are unchanged.
    """
    if record.dirty:
        return True
    if record.unpushed_shas is None:
        return True
    if any(sha not in exclude for sha in record.unpushed_shas):
        return True
    # Every local-only commit is an excluded provisioning commit; `ahead` beyond
    # those is either work pushed elsewhere (keep, conservatively) or a miscount.
    return record.ahead > len(record.unpushed_shas)
