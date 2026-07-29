from __future__ import annotations

import pytest

from shipit.identity import Sha
from shipit.tree.cleanup import IDLE_THRESHOLD_SECONDS, classify, parse_duration
from shipit.tree.registry import TreeRecord

NOW = 1_000_000.0
THRESHOLD = 100.0
IDLE_MTIME = NOW - (THRESHOLD + 50)
ACTIVE_MTIME = NOW - (THRESHOLD - 50)

SHA_WORK = Sha("b" * 40)
SHA_OTHER = Sha("c" * 40)


def _record(path: str = "/trees/t", **over) -> TreeRecord:
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
    if "last_commit" not in over:
        base["last_commit"] = base["newest_mtime"]
    return TreeRecord(**base)


def _classify_one(record: TreeRecord) -> str:
    decision = classify([record], NOW, idle_threshold_seconds=THRESHOLD)
    for name in ("removable", "keep"):
        if getattr(decision, name):
            return name
    raise AssertionError("record landed in no bucket")


TABLE = [
    ("clean + fully pushed + idle past the threshold", {}, "removable"),
    (
        "a file was written inside the threshold -> keep",
        {"newest_mtime": ACTIVE_MTIME},
        "keep",
    ),
    # reads as ACTIVE. A filesystem hiccup must never license a delete.
    (
        "UNREADABLE activity signal -> keep (unknown is not idle)",
        {"newest_mtime": None, "last_commit": IDLE_MTIME},
        "keep",
    ),
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
    ("ahead of upstream but on a remote -> removable", {"ahead": 2}, "removable"),
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
    # deletion-only commit the stamp was added for, so deferring to it here would license
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


def test_a_tree_with_a_live_session_is_never_removable():
    live_session = _record(
        path="/trees/acme/widget/ephemeral/abc123",
        branch="ephemeral/abc123",
        dirty=False,
        unpushed_shas=(),
        mtime=NOW - (THRESHOLD * 100),
        newest_mtime=NOW - 1,
    )
    decision = classify([live_session], NOW, idle_threshold_seconds=THRESHOLD)
    assert decision.removable == []
    assert [r.path for r in decision.keep] == [live_session.path]


def test_activity_under_a_subdirectory_keeps_the_tree():
    record = _record(mtime=NOW - (THRESHOLD * 1_000), newest_mtime=NOW - 5)
    assert _classify_one(record) == "keep"


def test_a_clean_tree_with_unpushed_commits_is_kept():
    record = _record(dirty=False, unpushed_shas=(SHA_WORK,), newest_mtime=IDLE_MTIME)
    assert _classify_one(record) == "keep"


def test_idle_threshold_boundary_is_exclusive():
    assert _classify_one(_record(newest_mtime=NOW - THRESHOLD)) == "keep"
    assert _classify_one(_record(newest_mtime=NOW - THRESHOLD - 1)) == "removable"


def test_default_threshold_is_48_hours():
    assert IDLE_THRESHOLD_SECONDS == 48 * 3_600
    young = _record(newest_mtime=NOW - (IDLE_THRESHOLD_SECONDS - 1))
    old = _record(newest_mtime=NOW - (IDLE_THRESHOLD_SECONDS + 1))
    assert classify([young], NOW).removable == []
    assert len(classify([old], NOW).removable) == 1


def test_the_rule_does_not_dispatch_on_kind():
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
    assert len(decision.removable) + len(decision.keep) == len(records)


def test_empty_fleet_is_two_empty_buckets():
    decision = classify([], NOW)
    assert decision.removable == []
    assert decision.keep == []


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
    assert parse_duration("48h") == IDLE_THRESHOLD_SECONDS
