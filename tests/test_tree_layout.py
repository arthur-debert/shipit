"""Unit tests for ``tree.layout.plan`` — the pure Tree-planning truth table.

Asserts external behavior (the resolved branch/dir/base for a spec), never "it
called git": the planner is pure, so the plan IS the contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shipit.tree import layout
from shipit.tree.layout import TreeSpec, plan, sanitize_slug

ROOT = Path("/trees")


def _issue_spec(**over) -> TreeSpec:
    base = dict(org="acme", repo="widget", agent_hash="deadbeef", issue=123, root=ROOT)
    base.update(over)
    return TreeSpec(**base)


# --------------------------------------------------------------------------
# branch
# --------------------------------------------------------------------------


def test_branch_is_fix_issue_with_slug():
    p = plan(_issue_spec(slug="header-align"))
    assert p.branch == "fix/123-header-align"


def test_branch_is_bare_fix_issue_when_slug_empty():
    p = plan(_issue_spec(slug=""))
    assert p.branch == "fix/123"


def test_branch_never_carries_the_agent_hash():
    p = plan(_issue_spec(agent_hash="cafe1234", slug="x"))
    assert "cafe1234" not in p.branch


# --------------------------------------------------------------------------
# dir — hash lands HERE, keyed by issue, under the central root
# --------------------------------------------------------------------------


def test_dir_is_central_root_org_repo_issues_n_hash():
    p = plan(_issue_spec())
    assert p.dir == ROOT / "acme" / "widget" / "issues" / "123-deadbeef"


def test_dir_carries_the_agent_hash():
    p = plan(_issue_spec(agent_hash="abc99999"))
    assert p.dir.name == "123-abc99999"


def test_dir_leaf_does_not_use_the_slug():
    # The slug shapes the branch, not the dir leaf (which is issue + hash).
    p = plan(_issue_spec(slug="some-words"))
    assert p.dir.name == "123-deadbeef"


# --------------------------------------------------------------------------
# base
# --------------------------------------------------------------------------


def test_base_is_origin_main_for_an_issue():
    assert plan(_issue_spec()).base == "origin/main"


# --------------------------------------------------------------------------
# slug sanitization (lives in layout)
# --------------------------------------------------------------------------


def test_sanitize_lowercases_and_dashes_separators():
    assert sanitize_slug("Header/Align: Foo.Bar") == "header-align-foo-bar"


def test_sanitize_collapses_runs_and_trims():
    assert sanitize_slug("  Lots   of   Space  ") == "lots-of-space"


def test_sanitize_all_separators_is_empty():
    assert sanitize_slug("  ///  ") == ""


def test_plan_applies_slug_sanitization_to_the_branch():
    p = plan(_issue_spec(slug="Fix The Thing"))
    assert p.branch == "fix/123-fix-the-thing"


# --------------------------------------------------------------------------
# epic / work-stream shape — branch E/WSnn, base origin/E/umbrella, hash on dir
# --------------------------------------------------------------------------


def _epic_spec(**over) -> TreeSpec:
    base = dict(
        org="acme",
        repo="widget",
        agent_hash="deadbeef",
        epic="HAR02",
        ws=2,
        root=ROOT,
    )
    base.update(over)
    return TreeSpec(**base)


@pytest.mark.parametrize(
    "ws, expected_branch",
    [
        (1, "HAR02/WS01"),
        (2, "HAR02/WS02"),
        (12, "HAR02/WS12"),
        (100, "HAR02/WS100"),
    ],
)
def test_epic_branch_is_slash_namespaced_zero_padded(ws, expected_branch):
    assert plan(_epic_spec(ws=ws)).branch == expected_branch


def test_epic_branch_keeps_epic_code_verbatim():
    # The epic code is human-assigned (uppercase THEME+NN) and is NOT sanitized.
    assert plan(_epic_spec(epic="GPU02")).branch == "GPU02/WS02"


def test_epic_base_is_origin_epic_umbrella():
    assert plan(_epic_spec()).base == "origin/HAR02/umbrella"


def test_epic_dir_is_epics_kind_with_hash_on_leaf():
    p = plan(_epic_spec())
    assert p.dir == ROOT / "acme" / "widget" / "epics" / "HAR02" / "WS02-deadbeef"


def test_epic_dir_carries_slug_when_given_branch_does_not():
    p = plan(_epic_spec(slug="Tiling Pass"))
    assert (
        p.dir
        == ROOT / "acme" / "widget" / "epics" / "HAR02" / "WS02-tiling-pass-deadbeef"
    )
    # The slug rides the dir only; the canonical branch stays E/WSnn.
    assert p.branch == "HAR02/WS02"


def test_epic_branch_never_carries_the_agent_hash():
    p = plan(_epic_spec(agent_hash="cafe1234", slug="anything"))
    assert "cafe1234" not in p.branch


def test_epic_dir_leaf_carries_the_agent_hash():
    assert plan(_epic_spec(agent_hash="abc99999")).dir.name == "WS02-abc99999"


def test_epic_requires_both_epic_and_ws():
    with pytest.raises(ValueError, match="both --epic and --ws"):
        plan(_epic_spec(ws=None))
    with pytest.raises(ValueError, match="both --epic and --ws"):
        plan(TreeSpec(org="o", repo="r", agent_hash="h", ws=3, root=ROOT))


# --------------------------------------------------------------------------
# freeform shape — branch verbatim, base origin/main, sanitized dir leaf
# --------------------------------------------------------------------------


def _freeform_spec(**over) -> TreeSpec:
    base = dict(
        org="acme", repo="widget", agent_hash="deadbeef", branch="spike/foo", root=ROOT
    )
    base.update(over)
    return TreeSpec(**base)


def test_freeform_branch_is_verbatim():
    # The caller owns the freeform name; the planner reflects it unchanged.
    assert plan(_freeform_spec(branch="my/wild-Branch")).branch == "my/wild-Branch"


def test_freeform_base_is_origin_main():
    assert plan(_freeform_spec()).base == "origin/main"


def test_freeform_dir_is_branches_kind_with_sanitized_leaf():
    p = plan(_freeform_spec(branch="spike/foo"))
    assert p.dir == ROOT / "acme" / "widget" / "branches" / "spike-foo-deadbeef"


def test_freeform_dir_sanitizes_separators_and_casing():
    p = plan(_freeform_spec(branch="Spike/Foo.Bar Baz"))
    assert p.dir.name == "spike-foo-bar-baz-deadbeef"


def test_freeform_branch_never_carries_the_agent_hash():
    p = plan(_freeform_spec(agent_hash="cafe1234", branch="wip"))
    assert "cafe1234" not in p.branch


# --------------------------------------------------------------------------
# the hash NEVER lands on the branch, for ANY shape
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec",
    [
        _issue_spec(agent_hash="cafef00d", slug="x"),
        _epic_spec(agent_hash="cafef00d", slug="x"),
        _freeform_spec(agent_hash="cafef00d", branch="spike/foo"),
    ],
)
def test_hash_never_appears_in_any_branch(spec):
    assert spec.agent_hash not in plan(spec).branch


# --------------------------------------------------------------------------
# shape exclusivity — exactly one shape, else ValueError
# --------------------------------------------------------------------------


def test_plan_rejects_no_shape():
    spec = TreeSpec(org="o", repo="r", agent_hash="h", root=ROOT)
    with pytest.raises(ValueError, match="exactly one shape"):
        plan(spec)


@pytest.mark.parametrize(
    "over",
    [
        dict(issue=1, branch="x"),
        dict(issue=1, epic="HAR02", ws=2),
        dict(branch="x", epic="HAR02", ws=2),
    ],
)
def test_plan_rejects_more_than_one_shape(over):
    spec = TreeSpec(org="o", repo="r", agent_hash="h", root=ROOT, **over)
    with pytest.raises(ValueError, match="exactly one shape"):
        plan(spec)


# --------------------------------------------------------------------------
# central root override
# --------------------------------------------------------------------------


def test_central_root_env_override(monkeypatch):
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, "/custom/trees")
    assert layout.central_root() == Path("/custom/trees")


def test_central_root_default_when_unset(monkeypatch):
    monkeypatch.delenv(layout.CENTRAL_ROOT_ENV, raising=False)
    assert layout.central_root() == Path("~/workspace/trees").expanduser()


def test_central_root_rejects_relative_override(monkeypatch):
    # A relative override would place Trees under the cwd (possibly inside the
    # source checkout), breaking the isolation invariant — reject it loudly.
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, "relative/trees")
    with pytest.raises(ValueError, match="absolute"):
        layout.central_root()


def test_plan_uses_central_root_when_spec_root_is_none(monkeypatch):
    monkeypatch.setenv(layout.CENTRAL_ROOT_ENV, "/env/trees")
    p = plan(_issue_spec(root=None))
    assert p.dir == Path("/env/trees") / "acme" / "widget" / "issues" / "123-deadbeef"
