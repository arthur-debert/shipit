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
# central root override + WS01 scope guard
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


def test_plan_rejects_non_issue_shape_in_ws01():
    spec = TreeSpec(org="o", repo="r", agent_hash="h", issue=None, root=ROOT)
    with pytest.raises(NotImplementedError):
        plan(spec)
