"""The bump-adapter registry's tests (TOL02-WS01, PRD story 22/25).

The registry is CLOSED and mirrors the toolchain registry exactly — pinned
here, along with the "tauri is never a dispatch label" invariant. Command
lines are asserted exactly (recorded-invocation discipline: the argv IS the
adapter's contract), and the pure rewrites (python's pyproject bump, the
bundle-config hook) are fixture-tested.
"""

import pytest

from shipit.release import ReleaseError, bump
from shipit.tools import registry


def test_registry_mirrors_the_toolchain_set():
    """One bump adapter per registered toolchain — the closed mirror (ADR-0041)."""
    assert set(bump.ADAPTERS) == set(registry.names())


def test_tauri_is_never_a_dispatch_label():
    """Story 25: bundle-level files ride the artifact-declared hook, so no
    "tauri" key may ever appear in the bump dispatch registry."""
    assert "tauri" not in bump.ADAPTERS


def test_rust_command_lines():
    """Workspace-wide bump (intra-workspace deps included), lock refreshed."""
    assert bump.adapter_for("rust").commands("1.2.3") == (
        ("cargo", "set-version", "--workspace", "1.2.3"),
        ("cargo", "update", "--workspace"),
    )


def test_rust_stages_workspace_manifests_and_lock():
    assert bump.adapter_for("rust").stage == (
        "Cargo.toml",
        "**/Cargo.toml",
        "Cargo.lock",
    )


def test_npm_command_line():
    """The package's own version bump, git side suppressed (prepare owns it)."""
    assert bump.adapter_for("npm").commands("2.0.0-rc.1") == (
        ("npm", "version", "2.0.0-rc.1", "--no-git-tag-version"),
    )


def test_python_is_a_pure_edit_with_no_commands():
    """Deliberately toolchain-free: a pyproject rewrite, zero commands."""
    adapter = bump.adapter_for("python")
    assert adapter.commands("1.2.3") == ()
    assert adapter.edit_path == "pyproject.toml"
    assert adapter.stage == ("pyproject.toml",)


def test_go_is_a_first_class_zero_file_adapter():
    """PRD story 22 / ADR-0041: go's projection set is EMPTY — the tag alone
    carries the version (injected at build via -ldflags) — and that is a
    registry entry, not an exception."""
    adapter = bump.adapter_for("go")
    assert adapter.commands("1.2.3") == ()
    assert adapter.edit_path is None
    assert adapter.stage == ()
    assert not adapter.projects_files


def test_adapter_for_unknown_toolchain_is_loud():
    with pytest.raises(ReleaseError, match="no bump adapter"):
        bump.adapter_for("tauri")


# --------------------------------------------------------------------------
# bump_pyproject — the toolchain-free python projection
# --------------------------------------------------------------------------

_PYPROJECT = """\
[build-system]
requires = ["hatchling"]

[project]
name = "demo"
authors = [{ name = "A" }]
version = "0.1.0"
description = "a [bracketed] description"

[tool.other]
version = "9.9.9"
"""


def test_bump_pyproject_rewrites_only_the_project_version():
    out = bump.bump_pyproject(_PYPROJECT, "0.2.0")
    assert 'version = "0.2.0"' in out
    assert 'version = "9.9.9"' in out  # [tool.other] untouched
    assert out == _PYPROJECT.replace('version = "0.1.0"', 'version = "0.2.0"')


def test_bump_pyproject_crosses_arrays_but_not_tables():
    text = '[project]\nname = "x"\nclassifiers = [\n  "A :: B",\n]\nversion = "1.0.0"\n'
    assert 'version = "2.0.0"' in bump.bump_pyproject(text, "2.0.0")


def test_bump_pyproject_preserves_single_quote_style():
    """A TOML literal string (single-quoted) is a valid version line; the bump
    keeps the consumer's quote style."""
    text = "[project]\nname = 'x'\nversion = '1.0.0'\n"
    assert (
        bump.bump_pyproject(text, "2.0.0")
        == "[project]\nname = 'x'\nversion = '2.0.0'\n"
    )


def test_bump_pyproject_without_project_version_is_loud():
    with pytest.raises(ReleaseError, match="no \\[project\\] version"):
        bump.bump_pyproject('[project]\nname = "x"\ndynamic = ["version"]\n', "1.0.0")


def test_bump_pyproject_ignores_version_of_other_tables_only():
    """A version line in a LATER table never satisfies the [project] match."""
    with pytest.raises(ReleaseError):
        bump.bump_pyproject(
            '[project]\nname = "x"\n\n[tool.y]\nversion = "1.0"\n', "2.0.0"
        )


# --------------------------------------------------------------------------
# bump_bundle_config — the artifact-declared hook's rewrite (story 25)
# --------------------------------------------------------------------------

_TAURI_CONF = """{
  "productName": "demo",
  "version": "0.1.0",
  "app": {
    "windows": [{ "title": "demo", "version": "ignored" }]
  }
}
"""


def test_bump_bundle_config_rewrites_top_level_version_preserving_format():
    out = bump.bump_bundle_config(_TAURI_CONF, "0.2.0")
    assert out == _TAURI_CONF.replace('"version": "0.1.0"', '"version": "0.2.0"')


def test_bump_bundle_config_rejects_non_json():
    with pytest.raises(ReleaseError, match="not valid JSON"):
        bump.bump_bundle_config("nope {", "1.0.0")


def test_bump_bundle_config_requires_top_level_version():
    with pytest.raises(ReleaseError, match='no top-level string "version"'):
        bump.bump_bundle_config('{"productName": "x"}', "1.0.0")


def test_bump_bundle_config_refuses_a_nested_first_version():
    """A nested "version" appearing before the top-level member would make the
    textual rewrite ambiguous — refused, never a silent wrong edit."""
    text = '{"app": {"version": "0.0.9"}, "version": "0.1.0"}'
    with pytest.raises(ReleaseError, match="not the top-level"):
        bump.bump_bundle_config(text, "0.2.0")


# --------------------------------------------------------------------------
# explain_command_failure — the #793 unprovisioned-cargo-edit translation
# --------------------------------------------------------------------------


def test_missing_cargo_set_version_gets_the_reconcile_remedy():
    """Issue #793: `cargo set-version` dying with cargo's unknown-subcommand
    error means cargo-edit is unprovisioned — the message names the managed
    block and the install reconcile, NEVER a run-time install (#582)."""
    message = bump.explain_command_failure(
        ("cargo", "set-version", "--workspace", "1.2.3"),
        "error: no such command: `set-version`",
    )
    assert message is not None
    assert "cargo-edit" in message
    assert "`shipit install`" in message
    assert "pixi.toml#shipit-rust-release-deps" in message
    assert "pixi.lock" in message  # the reconcile commit must carry the lock
    assert "cargo install" not in message  # the superseded #795/#796 shape


def test_a_different_cargo_set_version_failure_stays_untranslated():
    """A failing bump for any OTHER reason (broken manifest, dirty workspace)
    is not the provisioning gap — it re-raises as the original ExecError."""
    assert (
        bump.explain_command_failure(
            ("cargo", "set-version", "--workspace", "1.2.3"),
            "error: failed to parse manifest at Cargo.toml",
        )
        is None
    )


def test_other_commands_never_match_even_with_the_marker():
    """The translation is argv-scoped: only the rust adapter's set-version
    command maps to the cargo-edit remedy, whatever the stderr says."""
    assert (
        bump.explain_command_failure(("npm", "version", "1.2.3"), "no such command")
        is None
    )
    assert (
        bump.explain_command_failure(
            ("cargo", "update", "--workspace"), "error: no such command: `whatever`"
        )
        is None
    )
