from __future__ import annotations

from shipit.tree import fleet
from shipit.tree.registry import TreeRecord
from shipit.verbs.tree import format_fleet

EXPECTED_ROW_FIELDS = {
    "path",
    "created",
    "branch",
    "base",
    "ahead",
    "behind",
    "dirty",
    "age_seconds",
}

_LEAF_A = "widget-claude-20260717-081333-619cf51a-f501-44dc-992f-74df773204aa"
_LEAF_B = "widget-codex-20260102-030405-7c9e6679-7425-40de-944b-e07fc1f90ae7"


def _record(**over) -> TreeRecord:
    base = dict(
        path=f"/trees/{_LEAF_A}",
        branch="issues/7/work",
        base="origin/main",
        dirty=False,
        ahead=0,
        behind=0,
        mtime=1000.0,
        unpushed_shas=(),
    )
    base.update(over)
    return TreeRecord(**base)


def test_build_derives_created_from_the_flat_leaf():
    records = [
        _record(),
        _record(path=f"/trees/{_LEAF_B}", branch="HAR02/WS02"),
        _record(path="/trees/acme/widget/issues/7/work-aaaa", branch="issues/7/work"),
    ]

    result = fleet.build(records, now=1000.0)

    assert [row.created for row in result.trees] == [
        "20260717-081333",
        "20260102-030405",
        None,
    ]


def test_build_ages_each_row_against_the_injected_now():
    result = fleet.build([_record(mtime=1000.0)], now=1000.0 + 3600)
    assert result.trees[0].age_seconds == 3600


def test_build_clamps_a_future_mtime_to_zero_age():
    result = fleet.build([_record(mtime=2000.0)], now=1000.0)
    assert result.trees[0].age_seconds == 0


def test_build_keeps_absent_facts_as_none():
    result = fleet.build([_record(branch=None, base=None)], now=1000.0)
    row = result.trees[0]
    assert row.branch is None and row.base is None


def test_build_preserves_scan_order():
    records = [_record(path="/trees/a"), _record(path="/trees/b")]
    result = fleet.build(records, now=1000.0)
    assert [row.path for row in result.trees] == ["/trees/a", "/trees/b"]


def test_fleet_to_dict_declares_the_row_field_set():
    result = fleet.build([_record()], now=1000.0)
    payload = result.to_dict()
    assert set(payload) == {"trees"}
    assert set(payload["trees"][0]) == EXPECTED_ROW_FIELDS


def test_empty_fleet_to_dict_keeps_the_shape():
    assert fleet.build([], now=1000.0).to_dict() == {"trees": []}


def test_format_fleet_empty_is_the_no_trees_hint():
    assert format_fleet(fleet.Fleet(trees=())) == "No Trees under the central root."


def test_format_fleet_renders_the_table():
    records = [
        _record(),
        _record(
            path=f"/trees/{_LEAF_B}",
            branch="HAR02/WS02",
            base="origin/HAR02/umbrella",
            dirty=True,
            ahead=2,
            behind=1,
            mtime=500.0,
        ),
    ]

    out = format_fleet(fleet.build(records, now=1000.0))

    assert "BRANCH" in out and "BASE" in out and "CREATED" in out
    assert "KIND" not in out
    assert "PR" not in out
    assert "issues/7/work" in out
    assert "HAR02/WS02" in out
    assert "clean" in out and "dirty" in out
    assert "20260717-081333" in out and "20260102-030405" in out
    assert "origin/HAR02/umbrella (+2/-1)" in out


def test_format_fleet_renders_placeholders():
    out = format_fleet(fleet.build([_record(branch=None, base=None)], now=1000.0))
    assert "(detached)" in out
    lines = out.splitlines()
    assert len(lines) == 2
    assert lines[1].split()[3] == "-"


def test_format_fleet_renders_the_created_column():
    records = [
        _record(),
        _record(path=f"/trees/{_LEAF_B}", branch="b"),
        _record(path="/trees/acme/widget/review/tre03-ws03", branch="b"),
    ]

    out = format_fleet(fleet.build(records, now=1000.0))

    rows = {line.split()[0]: line.split()[1] for line in out.splitlines()[1:]}
    assert rows[f"/trees/{_LEAF_A}"] == "20260717-081333"
    assert rows[f"/trees/{_LEAF_B}"] == "20260102-030405"
    assert rows["/trees/acme/widget/review/tre03-ws03"] == "-"


def test_format_fleet_has_no_trailing_whitespace_or_newline():
    out = format_fleet(fleet.build([_record()], now=1000.0))
    assert not out.endswith("\n")
    assert all(line == line.rstrip() for line in out.splitlines())
