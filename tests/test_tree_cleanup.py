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


def _record(path: str = "/trees/t", **over) -> TreeRecord:
    base = dict(
        path=path,
        branch="fix/7-thing",
        base="origin/main",
        dirty=False,
        ahead=0,
        behind=0,
        pr=None,
        mtime=AGED_MTIME,
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
    ("open/unmerged PR is in flight", {}, "OPEN", "keep"),
    ("draft PR is in flight", {}, "DRAFT", "keep"),
    ("recent (merged but young) is kept", {"mtime": RECENT_MTIME}, "MERGED", "keep"),
    ("aged + clean + no PR is stale", {}, None, "stale"),
    ("aged + clean + closed-unmerged PR is stale", {}, "CLOSED", "stale"),
]


@pytest.mark.parametrize(
    "desc, over, state, expected", TABLE, ids=[row[0] for row in TABLE]
)
def test_classify_truth_table(desc, over, state, expected):
    assert _classify_one(_record(**over), state) == expected


def test_stale_is_never_removable():
    # The ambiguous-but-abandoned cases (no PR / closed) must never be auto-removed.
    for state in (None, "CLOSED"):
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
