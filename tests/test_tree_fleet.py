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

#: The exact JSON field set every `tree list --json` row must emit.
EXPECTED_ROW_FIELDS = {
    "path",
    "kind",
    "branch",
    "base",
    "ahead",
    "behind",
    "dirty",
    "pr",
    "age_seconds",
}


def _record(**over) -> TreeRecord:
    base = dict(
        path="/trees/acme/widget/issues/7/work-aaaa",
        branch="issues/7/work",
        base="origin/main",
        dirty=False,
        ahead=0,
        behind=0,
        pr="#7 DRAFT",
        mtime=1000.0,
        unpushed_shas=(),
    )
    base.update(over)
    return TreeRecord(**base)


# --- build: the pure record -> row derivation -----------------------------------


def test_build_derives_kind_from_the_path():
    records = [
        _record(),  # issues/<id>/... -> write
        _record(path="/trees/acme/widget/review/tre03-ws03", branch="TRE03/WS03"),
        _record(path="/trees/acme/widget/ephemeral/sess-1", branch="ephemeral/sess-1"),
    ]

    result = fleet.build(records, now=1000.0)

    assert [row.kind for row in result.trees] == ["write", "review", "ephemeral"]


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
    result = fleet.build([_record(branch=None, base=None, pr=None)], now=1000.0)
    row = result.trees[0]
    assert row.branch is None and row.base is None and row.pr is None


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
            path="/trees/acme/widget/epics/HAR02/WS02-bbbb",
            branch="HAR02/WS02",
            base="origin/HAR02/umbrella",
            dirty=True,
            ahead=2,
            behind=1,
            pr="#9 OPEN",
            mtime=500.0,
        ),
    ]

    out = format_fleet(fleet.build(records, now=1000.0))

    # Header + both Trees render, with branch, base, dirty state, and PR label.
    assert "BRANCH" in out and "BASE" in out and "PR" in out and "KIND" in out
    assert "issues/7/work" in out
    assert "HAR02/WS02" in out
    assert "clean" in out and "dirty" in out
    assert "#7 DRAFT" in out and "#9 OPEN" in out
    # Divergence is annotated on the BASE cell.
    assert "origin/HAR02/umbrella (+2/-1)" in out


def test_format_fleet_renders_placeholders():
    out = format_fleet(
        fleet.build([_record(branch=None, base=None, pr=None)], now=1000.0)
    )
    assert "(detached)" in out
    lines = out.splitlines()
    assert len(lines) == 2  # header + one row
    assert lines[1].split()[-1] == "-"  # the PR placeholder


def test_format_fleet_renders_the_kind_column():
    records = [
        _record(),
        _record(path="/trees/acme/widget/review/tre03-ws03", branch="b", pr=None),
        _record(path="/trees/acme/widget/ephemeral/sess-1", branch="b", pr=None),
    ]

    out = format_fleet(fleet.build(records, now=1000.0))

    rows = {line.split()[0]: line.split()[1] for line in out.splitlines()[1:]}
    assert rows["/trees/acme/widget/issues/7/work-aaaa"] == "write"
    assert rows["/trees/acme/widget/review/tre03-ws03"] == "review"
    assert rows["/trees/acme/widget/ephemeral/sess-1"] == "ephemeral"


def test_format_fleet_has_no_trailing_whitespace_or_newline():
    out = format_fleet(fleet.build([_record()], now=1000.0))
    assert not out.endswith("\n")
    assert all(line == line.rstrip() for line in out.splitlines())
