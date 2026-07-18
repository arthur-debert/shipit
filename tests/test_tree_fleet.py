"""The fleet listing as typed records (CLI02-WS03, ADR-0030).

Typed-in/typed-out tests for :mod:`shipit.tree.fleet` (the pure derivation of
listing rows from scan records) plus the pure ``format_fleet`` table renderer
and the ``--json`` field set — the promoted halves of ``shipit tree list``.
No monkeypatching, no fleet on disk: values in, values out.
"""

from __future__ import annotations

from shipit.tree import fleet
from shipit.tree.registry import TreeRecord
from shipit.verbs.tree import format_fleet

#: The exact JSON field set every `tree list --json` row must emit. Since ADR-0074
#: the `kind` field is gone (no kind segment to parse) and a real `created` column —
#: the flat leaf's `<timestamp>` — takes its place.
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

#: A flat Tree leaf (ADR-0074): `<repo>-<agent>-<timestamp>-<id>`, so its `<timestamp>`
#: slot is a real created column and `created_from_leaf` recovers it.
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


# --- build: the pure record -> row derivation -----------------------------------


def test_build_derives_created_from_the_flat_leaf():
    # The created column is sourced from the flat leaf's `<timestamp>` slot (ADR-0074);
    # an OLD nested Tree (no flat leaf, reclaimed by attrition) reads None, never a
    # fabricated date.
    records = [
        _record(),  # flat leaf A -> its timestamp
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
    # A just-touched Tree (clock skew, an mtime bumped mid-scan) never reads a
    # negative age.
    result = fleet.build([_record(mtime=2000.0)], now=1000.0)
    assert result.trees[0].age_seconds == 0


def test_build_keeps_absent_facts_as_none():
    # The typed row carries honest nulls; placeholder spellings ("(detached)",
    # "-") are the text renderer's job, so the JSON surface stays raw.
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


# --- format_fleet: the pure table renderer ---------------------------------------


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

    # Header + both Trees render, with branch, base, and dirty state. The KIND column
    # is gone (no kind segment since ADR-0074); a real CREATED column takes its place.
    # No PR column: the `gh` read is gone with the reclaim signal it fed (ADR-0072).
    assert "BRANCH" in out and "BASE" in out and "CREATED" in out
    assert "KIND" not in out
    assert "PR" not in out
    assert "issues/7/work" in out
    assert "HAR02/WS02" in out
    assert "clean" in out and "dirty" in out
    # The created stamps render from the flat leaves.
    assert "20260717-081333" in out and "20260102-030405" in out
    # Divergence is annotated on the BASE cell.
    assert "origin/HAR02/umbrella (+2/-1)" in out


def test_format_fleet_renders_placeholders():
    out = format_fleet(fleet.build([_record(branch=None, base=None)], now=1000.0))
    assert "(detached)" in out  # the branch placeholder
    lines = out.splitlines()
    assert len(lines) == 2  # header + one row
    assert lines[1].split()[3] == "-"  # the BASE placeholder (column index 3)


def test_format_fleet_renders_the_created_column():
    # The CREATED cell (column index 1, after PATH) is the flat leaf's `<timestamp>`;
    # a pre-flat nested Tree reads `-`.
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
