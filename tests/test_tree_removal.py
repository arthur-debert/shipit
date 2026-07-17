"""Removal gating as a typed domain outcome (CLI02-WS03, ADR-0030).

Typed tests for :mod:`shipit.tree.removal` — the promoted domain half of
``shipit tree remove``: target resolution, the pure risk detection, and the
gate's typed :class:`~shipit.tree.removal.Gate` outcome. No fleet on disk, no
terminal, no callables: the whole truth table is values in, values out
(replacing the old verb tests' injected ``confirm``/``is_tty`` spies).
"""

from __future__ import annotations

import pytest

from shipit.tree import removal
from shipit.tree.registry import TreeRecord
from shipit.tree.removal import Gate, GateAction, RemovalError


def _record(**over) -> TreeRecord:
    base = dict(
        path="/trees/acme/widget/issues/7/work-aaaa",
        branch="issues/7/work",
        base="origin/main",
        dirty=False,
        ahead=0,
        behind=0,
        pr="#7 DRAFT",
        pr_state="DRAFT",
        mtime=1000.0,
        unpushed_shas=(),
    )
    base.update(over)
    return TreeRecord(**base)


# --- resolve_target --------------------------------------------------------------


def test_resolve_target_matches_by_full_path():
    target = _record()
    other = _record(path="/trees/acme/widget/issues/9/work-bbbb")

    assert removal.resolve_target([target, other], target.path) is target


def test_resolve_target_matches_by_dir_name():
    target = _record()
    assert removal.resolve_target([target], "work-aaaa") is target


def test_resolve_target_path_match_takes_precedence():
    # A record whose FULL PATH equals the target wins over a basename collision.
    exact = _record(path="/trees/work-aaaa")
    by_name = _record(path="/trees/acme/widget/issues/7/work-aaaa")

    assert removal.resolve_target([by_name, exact], "/trees/work-aaaa") is exact


def test_resolve_target_no_match_refuses():
    with pytest.raises(RemovalError, match="no Tree matching"):
        removal.resolve_target([], "does-not-exist")


def test_resolve_target_ambiguous_refuses_and_names_both():
    a = _record(path="/trees/acme/widget/issues/7/work-aaaa")
    b = _record(path="/trees/acme/gadget/issues/7/work-aaaa")  # same leaf, two repos

    with pytest.raises(RemovalError, match="ambiguous") as excinfo:
        removal.resolve_target([a, b], "work-aaaa")
    assert a.path in str(excinfo.value) and b.path in str(excinfo.value)


# --- removal_risk ----------------------------------------------------------------


def test_removal_risk_clean_pushed_tree_is_safe():
    # A clean, fully-pushed Tree holds no work that the delete would lose -> no gate.
    assert removal.removal_risk(_record(dirty=False, ahead=0)) is None


def test_removal_risk_flags_dirty():
    risk = removal.removal_risk(_record(dirty=True, ahead=0))
    assert risk is not None and "uncommitted" in risk


def test_removal_risk_flags_unpushed_commits():
    risk = removal.removal_risk(_record(dirty=False, ahead=3))
    assert risk is not None and "3 unpushed commit" in risk


def test_removal_risk_combines_dirty_and_unpushed():
    risk = removal.removal_risk(_record(dirty=True, ahead=1))
    assert risk is not None
    assert "uncommitted" in risk and "1 unpushed commit" in risk


# --- gate: the typed outcome -------------------------------------------------------


def test_gate_clean_proceeds_without_prompting():
    gate = removal.gate(
        _record(dirty=False, ahead=0), assume_yes=False, interactive=True
    )
    assert gate == Gate(action=GateAction.PROCEED)


def test_gate_assume_yes_proceeds_even_when_risky():
    gate = removal.gate(_record(dirty=True, ahead=2), assume_yes=True, interactive=True)
    assert gate == Gate(action=GateAction.PROCEED)


def test_gate_risky_interactive_asks_first():
    record = _record(dirty=True, ahead=0)
    gate = removal.gate(record, assume_yes=False, interactive=True)
    assert gate.action is GateAction.CONFIRM
    assert gate.prompt is not None
    assert record.path in gate.prompt and "uncommitted changes" in gate.prompt
    assert gate.reason is None


def test_gate_risky_non_interactive_refuses_without_yes():
    record = _record(dirty=False, ahead=1)
    gate = removal.gate(record, assume_yes=False, interactive=False)
    assert gate.action is GateAction.REFUSE
    assert gate.reason is not None
    assert "non-interactively" in gate.reason and "--yes" in gate.reason
    assert gate.prompt is None


def test_gate_clean_non_interactive_proceeds():
    # The safe non-interactive default cuts both ways: a clean+pushed Tree needs
    # no terminal and no --yes.
    gate = removal.gate(_record(), assume_yes=False, interactive=False)
    assert gate.action is GateAction.PROCEED


# --- remove: the effectful apply ---------------------------------------------------


def test_remove_deletes_the_clone_dir(tmp_path):
    leaf = tmp_path / "work-aaaa"
    (leaf / ".git").mkdir(parents=True)

    removal.remove(_record(path=str(leaf)))

    assert not leaf.exists()


def test_remove_maps_a_failed_delete_to_the_typed_refusal(monkeypatch):
    def boom(_path):
        raise OSError("permission denied")

    monkeypatch.setattr(removal, "remove_tree", boom)

    with pytest.raises(RemovalError, match="could not remove"):
        removal.remove(_record())
