"""Truth-table tests for ``tree.cleanup.classify`` — the pure removable/stale/keep partition.

These assert EXTERNAL behavior (PRD Testing Decisions): given a snapshot of records,
the current time, and a per-Tree PR-state map, ``classify`` drops each Tree in the right
bucket. ``classify`` is pure — ``now`` and ``pr_states`` are inputs, there is no clock or
I/O — so the whole table is driven directly, including the age-threshold boundary.
"""

from __future__ import annotations

import pytest

from shipit.tree.cleanup import (
    DEFAULT_MAX_AGE_SECONDS,
    classify,
    parse_duration,
)
from shipit.tree.registry import TreeRecord

NOW = 1_000_000.0
THRESHOLD = 100.0
#: An mtime comfortably older than ``THRESHOLD`` relative to ``NOW`` (Tree is aged).
AGED_MTIME = NOW - (THRESHOLD + 50)
#: An mtime within ``THRESHOLD`` of ``NOW`` (Tree is recent).
RECENT_MTIME = NOW - (THRESHOLD - 50)


#: Stand-in full SHAs for truth-table rows (git SHAs are 40 hex chars).
SHA_PROVISION = "a" * 40
SHA_WORK = "b" * 40
SHA_OTHER = "c" * 40


def _record(path: str = "/trees/t", **over) -> TreeRecord:
    # `unpushed_shas=()` (every commit reachable from SOME remote) is the baseline
    # the non-floor rows need: the write ladder's floor is `_has_local_only_work`,
    # which reads the TreeRecord default (`unpushed_shas=None`, list unreadable)
    # conservatively as has-work.
    base = dict(
        path=path,
        branch="issues/7/work",
        base="origin/main",
        dirty=False,
        ahead=0,
        behind=0,
        pr=None,
        mtime=AGED_MTIME,
        unpushed_shas=(),
    )
    base.update(over)
    return TreeRecord(**base)


def _classify_one(record: TreeRecord, state: str | None) -> str:
    """Run ``classify`` on a single record and return the bucket name it landed in."""
    decision = classify(
        [record], now=NOW, pr_states={record.path: state}, max_age_seconds=THRESHOLD
    )
    for name in ("removable", "stale", "keep"):
        if getattr(decision, name):
            return name
    raise AssertionError("record landed in no bucket")


# (description, record-overrides, pr-state) -> expected bucket. Each row is one cell of
# the PRD truth table.
TABLE = [
    ("merged + clean + no-unpushed + aged", {}, "MERGED", "removable"),
    ("dirty (else removable) is protected", {"dirty": True}, "MERGED", "keep"),
    ("unpushed (ahead>0) is protected", {"ahead": 2}, "MERGED", "keep"),
    # The upstream-INDEPENDENT floor (codex review): a branch with no tracking
    # upstream reads ahead==0 while still holding commits on no remote at all —
    # e.g. extra commits made after the remote branch was deleted on merge. The
    # `unpushed` list alone must protect it, and an UNREADABLE list must read
    # as has-work (a git hiccup must never point at data loss).
    (
        "unpushed (on no remote, ahead==0) is protected",
        {"unpushed_shas": (SHA_WORK, SHA_OTHER, SHA_PROVISION)},
        "MERGED",
        "keep",
    ),
    (
        "UNREADABLE unpushed list is protected",
        {"unpushed_shas": None},
        "MERGED",
        "keep",
    ),
    ("open/unmerged PR is in flight", {}, "OPEN", "keep"),
    ("draft PR is in flight", {}, "DRAFT", "keep"),
    ("recent (merged but young) is kept", {"mtime": RECENT_MTIME}, "MERGED", "keep"),
    ("aged + clean + no PR is stale", {}, None, "stale"),
    ("aged + clean + closed-unmerged PR is stale", {}, "CLOSED", "stale"),
    # UNKNOWN (unreadable PR state) is conservatively stale — NEVER removable — even
    # when the Tree is otherwise a removable shape (aged + clean + no unpushed).
    ("aged + clean + UNKNOWN PR is stale (never removable)", {}, "UNKNOWN", "stale"),
]


@pytest.mark.parametrize(
    "desc, over, state, expected", TABLE, ids=[row[0] for row in TABLE]
)
def test_classify_truth_table(desc, over, state, expected):
    assert _classify_one(_record(**over), state) == expected


def test_stale_is_never_removable():
    # The ambiguous-but-abandoned cases (no PR / closed) and the unreadable case
    # (UNKNOWN) must never be auto-removed.
    for state in (None, "CLOSED", "UNKNOWN"):
        decision = classify(
            [_record()],
            now=NOW,
            pr_states={"/trees/t": state},
            max_age_seconds=THRESHOLD,
        )
        assert decision.removable == []
        assert len(decision.stale) == 1


def test_age_threshold_boundary_is_exclusive():
    # Exactly at the threshold the Tree is NOT yet aged -> not removable (kept).
    at = _record(mtime=NOW - THRESHOLD)
    assert _classify_one(at, "MERGED") == "keep"
    # One second older than the threshold -> aged -> removable.
    past = _record(mtime=NOW - THRESHOLD - 1)
    assert _classify_one(past, "MERGED") == "removable"


def test_partition_is_disjoint_and_exhaustive():
    records = [
        _record(path="/trees/removable"),  # merged + aged -> removable
        _record(path="/trees/keep-dirty", dirty=True),
        _record(path="/trees/keep-unpushed", ahead=1),
        _record(path="/trees/keep-recent", mtime=RECENT_MTIME),
        _record(path="/trees/keep-open"),
        _record(path="/trees/stale"),  # no PR, aged -> stale
    ]
    pr_states = {
        "/trees/removable": "MERGED",
        "/trees/keep-dirty": "MERGED",
        "/trees/keep-unpushed": "MERGED",
        "/trees/keep-recent": "MERGED",
        "/trees/keep-open": "OPEN",
        "/trees/stale": None,
    }
    decision = classify(
        records, now=NOW, pr_states=pr_states, max_age_seconds=THRESHOLD
    )

    assert [r.path for r in decision.removable] == ["/trees/removable"]
    assert [r.path for r in decision.stale] == ["/trees/stale"]
    assert {r.path for r in decision.keep} == {
        "/trees/keep-dirty",
        "/trees/keep-unpushed",
        "/trees/keep-recent",
        "/trees/keep-open",
    }
    # Every input lands in exactly one bucket (disjoint + exhaustive).
    total = len(decision.removable) + len(decision.stale) + len(decision.keep)
    assert total == len(records)


def test_missing_pr_state_is_treated_as_no_pr():
    # A record absent from pr_states (e.g. no branch) defaults to None -> stale when aged.
    decision = classify([_record()], now=NOW, pr_states={}, max_age_seconds=THRESHOLD)
    assert len(decision.stale) == 1


def test_default_threshold_is_two_weeks():
    # A merged+clean Tree younger than the default threshold is kept; older is removable.
    young = _record(mtime=NOW - (DEFAULT_MAX_AGE_SECONDS - 1))
    old = _record(mtime=NOW - (DEFAULT_MAX_AGE_SECONDS + 1))
    assert classify([young], now=NOW, pr_states={"/trees/t": "MERGED"}).removable == []
    assert (
        len(classify([old], now=NOW, pr_states={"/trees/t": "MERGED"}).removable) == 1
    )


# --- shared read-only (reviewer) Tree reclaim (ADR-0018) -----------------------

#: A review Tree's path carries the `review` kind segment (the classify marker).
REVIEW_PATH = "/trees/acme/widget/review/tre03-ws03"


def _review_record(**over) -> TreeRecord:
    """A review-Tree record: read-only (clean, level) and aged by default."""
    return _record(path=REVIEW_PATH, branch="TRE03/WS03", **over)


def _classify_review(state, *, live: bool) -> str:
    decision = classify(
        [_review_record()],
        now=NOW,
        pr_states={REVIEW_PATH: state},
        max_age_seconds=THRESHOLD,
        live_reviews={REVIEW_PATH: live},
    )
    for name in ("removable", "stale", "keep"):
        if getattr(decision, name):
            return name
    raise AssertionError("record landed in no bucket")


# (description, pr-state, reviewer-live) -> expected bucket. The review reclaim rule:
# removable iff (merged OR closed) AND no reviewer is live; else keep; never stale.
REVIEW_TABLE = [
    ("merged + no live reviewer -> removable", "MERGED", False, "removable"),
    ("closed + no live reviewer -> removable", "CLOSED", False, "removable"),
    ("merged but a reviewer is live -> keep", "MERGED", True, "keep"),
    ("closed but a reviewer is live -> keep", "CLOSED", True, "keep"),
    ("open PR (in flight) -> keep", "OPEN", False, "keep"),
    ("draft PR (in flight) -> keep", "DRAFT", False, "keep"),
    ("UNKNOWN state -> keep (never guess)", "UNKNOWN", False, "keep"),
    ("no PR -> keep", None, False, "keep"),
]


@pytest.mark.parametrize(
    "desc, state, live, expected", REVIEW_TABLE, ids=[r[0] for r in REVIEW_TABLE]
)
def test_review_tree_reclaim_truth_table(desc, state, live, expected):
    assert _classify_review(state, live=live) == expected


def test_review_tree_is_never_stale():
    # A review Tree is a cheap shared clone: it is either removable or kept, never the
    # "needs-a-human" stale bucket (unlike a write Tree with no/closed PR).
    for state in (None, "CLOSED", "UNKNOWN"):
        decision = classify(
            [_review_record()],
            now=NOW,
            pr_states={REVIEW_PATH: state},
            max_age_seconds=THRESHOLD,
        )
        assert decision.stale == []


def test_review_tree_ignores_age_for_reclaim():
    # The review rule is checked BEFORE the age ladder, so a merged review Tree is
    # removable even when freshly touched — age does not protect a shared clone whose
    # PR is done (and no reviewer is live).
    recent = _review_record(mtime=RECENT_MTIME)
    decision = classify(
        [recent], now=NOW, pr_states={REVIEW_PATH: "MERGED"}, max_age_seconds=THRESHOLD
    )
    assert [r.path for r in decision.removable] == [REVIEW_PATH]


def test_review_tree_defaults_to_no_live_reviewer():
    # Omitting live_reviews entirely (the gc default) treats every review Tree as
    # having no live reviewer, so a merged one is reclaimable.
    decision = classify(
        [_review_record()],
        now=NOW,
        pr_states={REVIEW_PATH: "MERGED"},
        max_age_seconds=THRESHOLD,
    )
    assert len(decision.removable) == 1


def test_write_tree_under_a_review_named_org_is_not_a_review_tree():
    # `_is_review_tree` keys off the leaf's PARENT segment, not "review anywhere in the
    # path": a write Tree whose org/repo happens to be named "review" must still take the
    # write ladder. Here a dirty + merged write Tree must be KEPT (dirty protects it); if
    # it were misclassified as a review Tree the merge would make it removable.
    path = "/trees/review/widget/branches/feat-x-deadbeef"
    record = _record(path=path, dirty=True)
    decision = classify(
        [record], now=NOW, pr_states={path: "MERGED"}, max_age_seconds=THRESHOLD
    )
    assert [r.path for r in decision.keep] == [path]
    assert not decision.removable


# --- ephemeral session Tree reclaim: the five-rung ladder (ADR-0027) -----------

#: An ephemeral Tree's path carries the `ephemeral` kind segment (the classify marker).
EPHEMERAL_PATH = "/trees/acme/widget/ephemeral/sess-20260702-1234"

#: Ladder time backstops, overridden small so every boundary is table-testable.
HARD_CAP = 1_000.0
GRACE = 100.0

#: Ages relative to NOW for each band of the ladder.
WITHIN_GRACE = NOW - (GRACE - 10)
PAST_GRACE = NOW - (GRACE + 10)  # past grace, still under the hard cap
PAST_HARD_CAP = NOW - (HARD_CAP + 10)


def _ephemeral_record(**over) -> TreeRecord:
    """An ephemeral-Tree record: clean, fully pushed, NO upstream (the birth shape).

    ``base=None`` + ``ahead=0`` is exactly the fresh ``ephemeral/<id>`` branch the
    upstream-independent ``unpushed`` list exists for; ``unpushed_shas=()`` means
    every commit is on some remote.
    """
    base = dict(
        path=EPHEMERAL_PATH,
        branch="ephemeral/sess-20260702-1234",
        base=None,
        ahead=0,
        unpushed_shas=(),
        mtime=PAST_GRACE,
    )
    base.update(over)
    return _record(**base)


def _classify_ephemeral(
    record: TreeRecord,
    state: str | None,
    *,
    live: bool,
    provision: frozenset[str] | None = None,
) -> str:
    decision = classify(
        [record],
        now=NOW,
        pr_states={record.path: state},
        max_age_seconds=THRESHOLD,
        live_sessions={record.path: live},
        provision_shas=None if provision is None else {record.path: provision},
        hard_cap_seconds=HARD_CAP,
        grace_seconds=GRACE,
    )
    for name in ("removable", "stale", "keep"):
        if getattr(decision, name):
            return name
    raise AssertionError("record landed in no bucket")


# (description, record-overrides, pr-state, live) -> expected bucket. One row per
# rung of the ADR-0027 ladder plus the boundary cases each rung's wording pins.
EPHEMERAL_TABLE = [
    # Rung 1 — dirty or unpushed -> KEEP, the absolute floor: beats merged, live,
    # and even the hard cap.
    ("dirty beats everything -> keep", {"dirty": True}, "MERGED", True, "keep"),
    (
        "dirty past the hard cap is still kept",
        {"dirty": True, "mtime": PAST_HARD_CAP},
        None,
        False,
        "keep",
    ),
    (
        "unpushed commits (no upstream at all) -> keep",
        {"unpushed_shas": (SHA_WORK, SHA_OTHER)},
        None,
        False,
        "keep",
    ),
    (
        "unpushed past the hard cap is still kept",
        {"unpushed_shas": (SHA_WORK,), "mtime": PAST_HARD_CAP},
        None,
        False,
        "keep",
    ),
    (
        "ahead of an upstream -> keep",
        {"base": "origin/main", "ahead": 1},
        None,
        False,
        "keep",
    ),
    (
        "UNREADABLE unpushed list is conservatively kept",
        {"unpushed_shas": None},
        None,
        False,
        "keep",
    ),
    # Rung 2 — merged PR -> REMOVABLE (the branch moved to real work and merged);
    # provable-done beats liveness and the grace window.
    ("merged -> removable", {}, "MERGED", False, "removable"),
    (
        "merged wins over a live session",
        {"mtime": WITHIN_GRACE},
        "MERGED",
        True,
        "removable",
    ),
    # Rung 3 — live and younger than the hard cap -> KEEP (an idle live session
    # keeps its workspace, however far past the grace window).
    ("live under the hard cap -> keep", {}, None, True, "keep"),
    ("live keeps an open-PR Tree too", {}, "OPEN", True, "keep"),
    # Rung 4 — past the hard cap, clean + pushed -> REMOVABLE even if the pidfile
    # claims live: the stale-pidfile escape hatch.
    (
        "hard cap overrides liveness",
        {"mtime": PAST_HARD_CAP},
        None,
        True,
        "removable",
    ),
    # Rung 5 — not live, clean, pushed -> REMOVABLE past the grace window...
    ("dead past grace -> removable", {}, None, False, "removable"),
    (
        "dead + UNKNOWN PR state past grace -> removable",
        {},
        "UNKNOWN",
        False,
        "removable",
    ),
    # ...and KEPT within it (a just-launched session is not raced before its
    # pidfile lands).
    ("dead within grace -> keep", {"mtime": WITHIN_GRACE}, None, False, "keep"),
]


@pytest.mark.parametrize(
    "desc, over, state, live, expected",
    EPHEMERAL_TABLE,
    ids=[row[0] for row in EPHEMERAL_TABLE],
)
def test_ephemeral_ladder_truth_table(desc, over, state, live, expected):
    assert _classify_ephemeral(_ephemeral_record(**over), state, live=live) == expected


# --- the rung-1 provisioning-commit carve-out (#232) ----------------------------
#
# A managed-set drift window makes provisioning commit the reconcile at Tree birth:
# one local-only commit on every fresh ephemeral Tree, which the absolute floor
# would otherwise KEEP forever (even the hard cap requires "pushed"). The recorded
# SHA — and exactly it — is excluded; every mismatch stays conservative.
#
# The validation-observed drift shape: upstream `origin/main`, the provisioning
# commit both ahead-of-upstream (ahead=1) and on no remote.
_DRIFT_SHAPE = {
    "base": "origin/main",
    "ahead": 1,
    "unpushed_shas": (SHA_PROVISION,),
}

# (description, record-overrides, live, provision-exclusion-set) -> expected bucket.
PROVISION_TABLE = [
    # The recorded provisioning commit is NOT work: the Tree falls THROUGH rung 1
    # to the liveness rungs — kept while live, reclaimed once the session is gone.
    (
        "provisioning-commit-only, dead past grace -> removable",
        _DRIFT_SHAPE,
        False,
        frozenset({SHA_PROVISION}),
        "removable",
    ),
    (
        "provisioning-commit-only, live -> keep via rung 3 (not the floor)",
        _DRIFT_SHAPE,
        True,
        frozenset({SHA_PROVISION}),
        "keep",
    ),
    (
        "provisioning-commit-only, past the hard cap -> removable even if live",
        {**_DRIFT_SHAPE, "mtime": PAST_HARD_CAP},
        True,
        frozenset({SHA_PROVISION}),
        "removable",
    ),
    # The floor stays ABSOLUTE for real work: any other local-only commit keeps,
    # exclusion or not.
    (
        "provisioning + one real commit -> keep",
        {
            "base": "origin/main",
            "ahead": 2,
            "unpushed_shas": (SHA_PROVISION, SHA_WORK),
        },
        False,
        frozenset({SHA_PROVISION}),
        "keep",
    ),
    # Missing metadata (no record was written / unreadable -> empty exclusion set):
    # the provisioning commit reads as work -> KEEP, the pre-#232 behavior.
    (
        "metadata missing -> keep",
        _DRIFT_SHAPE,
        False,
        frozenset(),
        "keep",
    ),
    # SHA mismatch (a rebase/amend changed the commit id): identity is the SHA,
    # never the message, so the mismatch falls back to KEEP — the safe direction.
    (
        "recorded SHA does not match the local commit (rebase) -> keep",
        {**_DRIFT_SHAPE, "unpushed_shas": (SHA_OTHER,)},
        False,
        frozenset({SHA_PROVISION}),
        "keep",
    ),
    # An UNREADABLE local-only list keeps even with a recorded exclusion: unknown
    # must never read as "nothing to lose".
    (
        "unreadable unpushed list -> keep despite a recorded exclusion",
        {"base": "origin/main", "ahead": 1, "unpushed_shas": None},
        False,
        frozenset({SHA_PROVISION}),
        "keep",
    ),
    # Dirty still beats everything — the exclusion narrows only the unpushed read.
    (
        "dirty -> keep despite a recorded exclusion",
        {**_DRIFT_SHAPE, "dirty": True},
        False,
        frozenset({SHA_PROVISION}),
        "keep",
    ),
    # `ahead` beyond what the exclusion explains is conservatively still work
    # (commits pushed to some other branch, or a miscount): keep.
    (
        "ahead beyond the excluded provisioning commit -> keep",
        {**_DRIFT_SHAPE, "ahead": 2},
        False,
        frozenset({SHA_PROVISION}),
        "keep",
    ),
]


@pytest.mark.parametrize(
    "desc, over, live, provision, expected",
    PROVISION_TABLE,
    ids=[row[0] for row in PROVISION_TABLE],
)
def test_ephemeral_provision_commit_carveout(desc, over, live, provision, expected):
    record = _ephemeral_record(**over)
    assert _classify_ephemeral(record, None, live=live, provision=provision) == expected


def test_provision_exclusion_never_reaches_the_write_ladder():
    # The carve-out is EPHEMERAL-ONLY: a write Tree whose path appears in
    # provision_shas still keeps on its local-only commit (the write floor takes
    # no exclusion), aged and merged or not.
    record = _record(unpushed_shas=(SHA_PROVISION,))
    decision = classify(
        [record],
        now=NOW,
        pr_states={record.path: "MERGED"},
        max_age_seconds=THRESHOLD,
        provision_shas={record.path: frozenset({SHA_PROVISION})},
    )
    assert [r.path for r in decision.keep] == [record.path]


def test_ephemeral_tree_is_never_stale():
    # No PR is the ephemeral NORM (the standard ladder would strand it in stale
    # forever) — the ladder resolves every shape to removable or keep.
    for state in (None, "CLOSED", "UNKNOWN", "OPEN"):
        for live in (False, True):
            decision = classify(
                [_ephemeral_record()],
                now=NOW,
                pr_states={EPHEMERAL_PATH: state},
                max_age_seconds=THRESHOLD,
                live_sessions={EPHEMERAL_PATH: live},
                hard_cap_seconds=HARD_CAP,
                grace_seconds=GRACE,
            )
            assert decision.stale == []


def test_ephemeral_defaults_to_not_live():
    # Omitting live_sessions entirely treats every session as gone: a clean, pushed
    # Tree past the grace window is reclaimable (the pidfile-less default).
    decision = classify(
        [_ephemeral_record()],
        now=NOW,
        pr_states={EPHEMERAL_PATH: None},
        max_age_seconds=THRESHOLD,
        hard_cap_seconds=HARD_CAP,
        grace_seconds=GRACE,
    )
    assert [r.path for r in decision.removable] == [EPHEMERAL_PATH]


def test_ephemeral_grace_and_hard_cap_boundaries_are_exclusive():
    # Exactly AT the grace window a dead Tree is still kept; exactly AT the hard
    # cap a live Tree is still kept — both boundaries are "past", not "at".
    at_grace = _ephemeral_record(mtime=NOW - GRACE)
    assert _classify_ephemeral(at_grace, None, live=False) == "keep"
    at_cap = _ephemeral_record(mtime=NOW - HARD_CAP)
    assert _classify_ephemeral(at_cap, None, live=True) == "keep"
    past_cap = _ephemeral_record(mtime=NOW - HARD_CAP - 1)
    assert _classify_ephemeral(past_cap, None, live=True) == "removable"


def test_write_tree_under_an_ephemeral_named_org_takes_the_write_ladder():
    # Kind is the LEAF'S PARENT segment, never "ephemeral anywhere in the path": a
    # write Tree under an org named `ephemeral` must take the write ladder — here
    # aged + clean + no PR lands in STALE (a bucket the ephemeral ladder never uses).
    path = "/trees/ephemeral/widget/branches/feat-x-deadbeef"
    record = _record(path=path)
    decision = classify(
        [record], now=NOW, pr_states={path: None}, max_age_seconds=THRESHOLD
    )
    assert [r.path for r in decision.stale] == [path]


# --- parse_duration: the pure --threshold helper -------------------------------

# (input, expected seconds) — one row per accepted shape. Each unit and a couple of
# magnitudes, plus surrounding whitespace and a mixed-case suffix, must all parse.
_DURATION_OK = [
    ("14d", 14 * 86_400),
    ("36h", 36 * 3_600),
    ("90m", 90 * 60),
    ("45s", 45),
    ("1d", 86_400),
    ("  7d  ", 7 * 86_400),  # surrounding whitespace is stripped
    ("12H", 12 * 3_600),  # the unit is case-insensitive
]


@pytest.mark.parametrize(
    "text, seconds", _DURATION_OK, ids=[row[0].strip() for row in _DURATION_OK]
)
def test_parse_duration_accepts_human_durations(text, seconds):
    result = parse_duration(text)
    assert result == float(seconds)
    assert isinstance(result, float)  # the type classify's max_age_seconds expects


# Each rejected shape is a clean ValueError, never a silent default.
_DURATION_BAD = [
    "",  # empty
    "   ",  # blank
    "14",  # no unit
    "14w",  # unknown unit
    "d",  # no magnitude
    "1.5d",  # non-integer magnitude
    "-5d",  # negative
    "0d",  # non-positive
    "abc",  # not a duration at all
]


@pytest.mark.parametrize("text", _DURATION_BAD)
def test_parse_duration_rejects_bad_input(text):
    with pytest.raises(ValueError):
        parse_duration(text)


def test_parse_duration_round_trips_with_default_threshold():
    # The CLI default (14d) must parse back to exactly DEFAULT_MAX_AGE_SECONDS, so the
    # documented default and the keyword default can never silently diverge.
    assert parse_duration("14d") == float(DEFAULT_MAX_AGE_SECONDS)
