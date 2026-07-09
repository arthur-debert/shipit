"""The pure leg planner (TOL01-WS01) — the ADR-0039 selector/passthrough rules.

Fixture-driven over typed :class:`shipit.config.ToolchainEntry` values, no
I/O: fan-out order, selector filtering, the unknown-selector error naming the
known legs, the multi-leg + passthrough + no-selector hard error listing the
legs, the single-leg no-selector sugar, and the per-path override replacing
the registry default for its leg only.
"""

import pytest

from shipit import config
from shipit.tools import legs


def _entry(path, toolchain, commands=None):
    return config.ToolchainEntry(
        path=path, toolchain=toolchain, commands=commands or {}
    )


TAURI_SHAPE = (
    _entry(".", "rust"),
    _entry("web", "npm"),
)


def test_bare_invocation_fans_out_over_all_legs_in_map_order():
    planned = legs.plan_legs(TAURI_SHAPE, tool="test")
    assert [leg.label for leg in planned] == ["rust (.)", "npm (web)"]
    assert planned[0].argv == ("cargo", "nextest", "run")
    assert planned[1].argv == ("npm", "test")
    assert all(leg.tool == "test" for leg in planned)


def test_selector_filters_to_one_toolchains_legs():
    planned = legs.plan_legs(TAURI_SHAPE, tool="test", selector="npm")
    assert [leg.label for leg in planned] == ["npm (web)"]


def test_selector_matches_a_map_path_too():
    # A repo with several legs of ONE toolchain addresses each by its map path.
    two_crates = (_entry("crates/a", "rust"), _entry("crates/b", "rust"))
    planned = legs.plan_legs(two_crates, tool="test", selector="crates/b")
    assert [leg.path for leg in planned] == ["crates/b"]


def test_unknown_selector_errors_naming_the_known_legs():
    with pytest.raises(legs.LegPlanError) as exc_info:
        legs.plan_legs(TAURI_SHAPE, tool="test", selector="python")
    message = str(exc_info.value)
    assert "unknown leg 'python'" in message
    assert "rust (.)" in message and "npm (web)" in message


def test_passthrough_forwards_verbatim_to_the_selected_leg():
    planned = legs.plan_legs(
        TAURI_SHAPE, tool="test", selector="rust", passthrough=("--no-capture",)
    )
    assert planned[0].argv == ("cargo", "nextest", "run", "--no-capture")


def test_passthrough_without_selector_on_a_multi_leg_repo_is_a_hard_error():
    # Never a broadcast: the error lists the legs so the fix is one retype.
    with pytest.raises(legs.LegPlanError) as exc_info:
        legs.plan_legs(TAURI_SHAPE, tool="test", passthrough=("-k", "foo"))
    message = str(exc_info.value)
    assert "rust (.)" in message and "npm (web)" in message
    assert "shipit test rust --" in message


def test_passthrough_with_a_selector_matching_several_legs_is_the_same_hard_error():
    two_crates = (_entry("crates/a", "rust"), _entry("crates/b", "rust"))
    with pytest.raises(legs.LegPlanError) as exc_info:
        legs.plan_legs(
            two_crates, tool="test", selector="rust", passthrough=("--no-capture",)
        )
    message = str(exc_info.value)
    assert "'rust' matches 2" in message
    assert "crates/a" in message and "crates/b" in message


def test_single_leg_repo_omits_the_selector_even_with_passthrough():
    planned = legs.plan_legs(
        (_entry(".", "python"),), tool="test", passthrough=("-k", "foo")
    )
    assert [leg.argv for leg in planned] == [("pytest", "-k", "foo")]


def test_per_path_override_replaces_the_default_for_that_leg_only():
    entries = (
        _entry("crates/a", "rust", {"test": ("cargo", "test", "--workspace")}),
        _entry("crates/b", "rust"),
    )
    planned = legs.plan_legs(entries, tool="test")
    assert planned[0].argv == ("cargo", "test", "--workspace")
    assert planned[1].argv == ("cargo", "nextest", "run")


def test_passthrough_appends_after_an_override_too():
    entries = (_entry(".", "python", {"test": ("python", "-m", "pytest", "-q")}),)
    planned = legs.plan_legs(entries, tool="test", passthrough=("-x",))
    assert planned[0].argv == ("python", "-m", "pytest", "-q", "-x")
