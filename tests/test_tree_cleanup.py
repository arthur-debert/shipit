"""Truth-table tests for ``tree.cleanup.classify`` — the pure removable/keep partition.

These assert EXTERNAL behavior (PRD Testing Decisions): given a snapshot of records and
the current time, ``classify`` drops each Tree in the right bucket. ``classify`` is pure
— ``now`` is an input, there is no clock or I/O — so the whole table is driven directly,
including the threshold boundary.

ONE rule, every kind (ADR-0072)::

    KEEP  if  dirty  ||  unpushed  ||  idle < 48h

so the table is small on purpose: the three ladders these tests used to cover (write /
review / ephemeral, 15 decision inputs between them) are gone, and with them the PR
state, the liveness probe and the kind dispatch. What remains is three signals, their
two unreadable arms, and one boundary.
"""

from __future__ import annotations

import pytest

from shipit.identity import Sha
from shipit.tree.cleanup import IDLE_THRESHOLD_SECONDS, classify, parse_duration
from shipit.tree.registry import TreeRecord

NOW = 1_000_000.0
#: The idle threshold, overridden small so the boundary is table-testable.
THRESHOLD = 100.0
#: A newest-file mtime comfortably PAST the threshold: nobody has written here.
IDLE_MTIME = NOW - (THRESHOLD + 50)
#: A newest-file mtime inside the threshold: someone wrote a file recently.
ACTIVE_MTIME = NOW - (THRESHOLD - 50)

#: Stand-in full SHAs for truth-table rows (git SHAs are 40 hex chars).
SHA_WORK = Sha("b" * 40)
SHA_OTHER = Sha("c" * 40)


def _record(path: str = "/trees/t", **over) -> TreeRecord:
    # The baseline is the ONLY removable shape: clean, every commit on some remote
    # (`unpushed_shas=()`), and idle past the threshold — with every signal READABLE,
    # which is part of the shape and not a detail. All three unreadable defaults on
    # TreeRecord (`unpushed_shas=None`, `newest_mtime=None`, `last_commit=None`) read
    # as KEEP, so a row must pin them to mean anything else.
    base = dict(
        path=path,
        branch="issues/7/work",
        base="origin/main",
        dirty=False,
        ahead=0,
        behind=0,
        mtime=IDLE_MTIME,
        unpushed_shas=(),
        newest_mtime=IDLE_MTIME,
    )
    base.update(over)
    # Idle is the newest of the walk and the commit stamp, and BOTH must be readable for
    # it to be answered at all. So a row that moves the activity signal means to move the
    # whole of it: last_commit tracks the walk unless the row names it, which keeps `max`
    # a no-op and lets each row below say exactly one thing. Rows probing the stamp — or
    # either unreadable arm — pass both halves explicitly.
    if "last_commit" not in over:
        base["last_commit"] = base["newest_mtime"]
    return TreeRecord(**base)


def _classify_one(record: TreeRecord) -> str:
    """Run ``classify`` on a single record and return the bucket name it landed in."""
    decision = classify([record], NOW, idle_threshold_seconds=THRESHOLD)
    for name in ("removable", "keep"):
        if getattr(decision, name):
            return name
    raise AssertionError("record landed in no bucket")


# (description, record-overrides) -> expected bucket. Each row is one cell of the rule.
TABLE = [
    ("clean + fully pushed + idle past the threshold", {}, "removable"),
    # --- the activity signal ---
    (
        "a file was written inside the threshold -> keep",
        {"newest_mtime": ACTIVE_MTIME},
        "keep",
    ),
    # Unknown is NOT false (ADR-0072): a walk that failed, or found no eligible file,
    # reads as ACTIVE. A filesystem hiccup must never license a delete.
    (
        "UNREADABLE activity signal -> keep (unknown is not idle)",
        {"newest_mtime": None, "last_commit": IDLE_MTIME},
        "keep",
    ),
    # --- the never-lose-work floor, on a Tree that is otherwise removable ---
    ("dirty (else removable) is protected", {"dirty": True}, "keep"),
    (
        "unpushed (commits on no remote) is protected",
        {"unpushed_shas": (SHA_WORK, SHA_OTHER)},
        "keep",
    ),
    (
        "UNREADABLE unpushed list is protected",
        {"unpushed_shas": None},
        "keep",
    ),
    # `ahead` is NOT the floor's signal: a commit ahead of its upstream but present on
    # some remote is recoverable. `unpushed_shas` asks the question that matters —
    # "does this exist anywhere but here?" — and it says no local-only work here.
    ("ahead of upstream but on a remote -> removable", {"ahead": 2}, "removable"),
    # --- what the rule deliberately does NOT read (ADR-0072) ---
    # PR state has no vote at all: the ``gh`` read and the ``pr``/``pr_state`` fields it
    # fed are gone from the scan (WS03), so the rule structurally cannot consult one.
    # Root mtime was the old clock and lags real activity by up to 10h; it is a display
    # signal now, and must not sway the rule in EITHER direction. Both rows pin that.
    (
        "stale root mtime does not remove an ACTIVE Tree",
        {"mtime": IDLE_MTIME, "newest_mtime": ACTIVE_MTIME},
        "keep",
    ),
    (
        "fresh root mtime does not keep an IDLE Tree",
        {"mtime": ACTIVE_MTIME, "newest_mtime": IDLE_MTIME},
        "removable",
    ),
    # The commit stamp is the one signal maxed IN, and only ever to keep. It cannot
    # decide alone (it is blind to a session that never commits) but it sees the one
    # thing the walk structurally cannot: a commit that only DELETES files.
    (
        "fresh last_commit keeps an idle-LOOKING Tree",
        {"last_commit": ACTIVE_MTIME},
        "keep",
    ),
    (
        "stale last_commit does not remove an ACTIVE Tree",
        {"last_commit": IDLE_MTIME, "newest_mtime": ACTIVE_MTIME},
        "keep",
    ),
    # ...and it is only ever maxed in when it can be READ. Idle is the newest of the two,
    # so an unknown half is a hole and not a lesser answer: the walk cannot see the
    # deletion-only commit the stamp was added for, so deferring to it here would license
    # exactly the delete the row above exists to prevent (codex, #1029 review round 2).
    (
        "UNREADABLE last_commit -> keep (it blanks idle, it does not defer to the walk)",
        {"last_commit": None, "newest_mtime": IDLE_MTIME},
        "keep",
    ),
    (
        "UNREADABLE last_commit -> keep, even with an active walk",
        {"last_commit": None, "newest_mtime": ACTIVE_MTIME},
        "keep",
    ),
    (
        "BOTH activity halves unreadable -> keep",
        {"last_commit": None, "newest_mtime": None},
        "keep",
    ),
]


@pytest.mark.parametrize("desc, over, expected", TABLE, ids=[row[0] for row in TABLE])
def test_classify_truth_table(desc, over, expected):
    assert _classify_one(_record(**over)) == expected


# --- the #1018 gate ----------------------------------------------------------------
#
# These are the regression tests the whole work stream exists for. Each one FAILS
# against the old ladder.


def test_a_tree_with_a_live_session_is_never_removable():
    """#1018, reproduced exactly: gc deleted a LIVE Claude session's worktree.

    The session had been running ~9 hours doing external `gcloud`/`gsutil` work, so:
    its git tree was CLEAN (nothing to commit), it had NO PR, its root mtime was hours
    stale (a directory's mtime does not move when an agent works under `src/`), and its
    pidfile probe read a false negative. Under the old ephemeral ladder rungs 1, 2 and
    4 could not fire, rung 3 needed `live == True` and did not get it, and rung 5 read
    age ONLY — from root mtime, against a 1h grace window — so a live session's cwd was
    deleted out from under it.

    The one thing that WAS true of that Tree: a file in it had just been written. That
    is the signal the rule now reads, and it is why this Tree is kept.
    """
    live_session = _record(
        path="/trees/acme/widget/ephemeral/abc123",
        branch="ephemeral/abc123",
        dirty=False,  # external gcloud work: nothing to commit
        unpushed_shas=(),  # nothing local-only either
        mtime=NOW - (THRESHOLD * 100),  # root mtime hours stale — the 10h lag
        newest_mtime=NOW - 1,  # ...but a file was written one second ago
    )
    decision = classify([live_session], NOW, idle_threshold_seconds=THRESHOLD)
    assert decision.removable == []
    assert [r.path for r in decision.keep] == [live_session.path]


def test_activity_under_a_subdirectory_keeps_the_tree():
    """The 10h-lag bug, pinned at the rule: root mtime stale, a subdir file fresh.

    An agent editing under `src/` leaves the clone ROOT's mtime untouched — that is
    what fed the old ladder and what made it delete a live Tree. `newest_mtime` sees
    the write wherever it lands, so the Tree is kept despite an ancient root mtime.
    (That the walk actually FINDS a subdir file is pinned in test_tree_activity.py;
    here the record states the two signals disagree, and the rule follows the one that
    measures.)
    """
    record = _record(mtime=NOW - (THRESHOLD * 1_000), newest_mtime=NOW - 5)
    assert _classify_one(record) == "keep"


def test_a_clean_tree_with_unpushed_commits_is_kept():
    """The retained floor, and the one non-obvious keep (ADR-0072).

    A clean Tree whose commits were never pushed looks exactly as idle as an abandoned
    one — it IS idle. Without this floor it is deleted at 48h and those commits die
    with `.git`: unrecoverable, and invisible until someone goes looking for them.
    """
    record = _record(dirty=False, unpushed_shas=(SHA_WORK,), newest_mtime=IDLE_MTIME)
    assert _classify_one(record) == "keep"


# --- the boundary and the defaults --------------------------------------------------


def test_idle_threshold_boundary_is_exclusive():
    # Idle exactly AT the threshold the Tree is still kept; one second past it is
    # removable. "Past", not "at" — the keep direction owns the boundary.
    assert _classify_one(_record(newest_mtime=NOW - THRESHOLD)) == "keep"
    assert _classify_one(_record(newest_mtime=NOW - THRESHOLD - 1)) == "removable"


def test_default_threshold_is_48_hours():
    # The default, with no override: a Tree idle just under 48h is kept, one idle just
    # over is removable. This is the ONE constant — it replaced the 14d/12h/4d/1h set.
    assert IDLE_THRESHOLD_SECONDS == 48 * 3_600
    young = _record(newest_mtime=NOW - (IDLE_THRESHOLD_SECONDS - 1))
    old = _record(newest_mtime=NOW - (IDLE_THRESHOLD_SECONDS + 1))
    assert classify([young], NOW).removable == []
    assert len(classify([old], NOW).removable) == 1


def test_the_rule_does_not_dispatch_on_kind():
    # Review, ephemeral and write Trees reclaim IDENTICALLY (ADR-0072): the same
    # signals, the same verdict. Kind is a name, not a decision input.
    paths = [
        "/trees/acme/widget/review/tre03-ws03",
        "/trees/acme/widget/ephemeral/abc123",
        "/trees/acme/widget/branches/feat-x-deadbeef",
    ]
    idle = [_record(path=p) for p in paths]
    active = [_record(path=p, newest_mtime=ACTIVE_MTIME) for p in paths]
    assert {
        r.path for r in classify(idle, NOW, idle_threshold_seconds=THRESHOLD).removable
    } == set(paths)
    assert {
        r.path for r in classify(active, NOW, idle_threshold_seconds=THRESHOLD).keep
    } == set(paths)


def test_partition_is_disjoint_and_exhaustive():
    records = [
        _record(path="/trees/removable"),
        _record(path="/trees/keep-dirty", dirty=True),
        _record(path="/trees/keep-unpushed", unpushed_shas=(SHA_WORK,)),
        _record(path="/trees/keep-active", newest_mtime=ACTIVE_MTIME),
        _record(path="/trees/keep-unreadable", newest_mtime=None),
    ]
    decision = classify(records, NOW, idle_threshold_seconds=THRESHOLD)

    assert [r.path for r in decision.removable] == ["/trees/removable"]
    assert {r.path for r in decision.keep} == {
        "/trees/keep-dirty",
        "/trees/keep-unpushed",
        "/trees/keep-active",
        "/trees/keep-unreadable",
    }
    # Every input lands in exactly one bucket (disjoint + exhaustive).
    assert len(decision.removable) + len(decision.keep) == len(records)


def test_empty_fleet_is_two_empty_buckets():
    decision = classify([], NOW)
    assert decision.removable == []
    assert decision.keep == []


# --- parse_duration (backs `gc --threshold`) ----------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("48h", 48 * 3_600),
        ("14d", 14 * 86_400),
        ("90m", 5_400),
        ("45s", 45),
        ("  36H  ", 36 * 3_600),
    ],
)
def test_parse_duration_accepts_each_unit(text, expected):
    assert parse_duration(text) == float(expected)


@pytest.mark.parametrize("text", ["", "   ", "14", "14w", "d", "-1d", "1.5d", "0h"])
def test_parse_duration_rejects_malformed_input(text):
    with pytest.raises(ValueError):
        parse_duration(text)


def test_parse_duration_round_trips_the_default_threshold():
    # The printed age (`48h`) round-trips back through `--threshold` to the same
    # boundary the constant sets.
    assert parse_duration("48h") == IDLE_THRESHOLD_SECONDS
