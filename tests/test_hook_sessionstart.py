"""Hook boundary + pure core: SessionStart → toolchain activation into CLAUDE_ENV_FILE.

The coordinator-activation seam (ADR-0027, SES01-WS01). Covers the three things the
slice owns: the PURE toolchain→activation mapping (pixi → export lines rendered from
pixi's `shell-hook --json` snapshot; non-pixi → empty), the manifest resolution from
the session's cwd, and the boundary's FAIL-OPEN contract (exit 0 always; a repo with
no activatable toolchain — or any error — writes nothing and never errors, because
activation is additive, never load-bearing).
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest
from shipit.harness import activation
from shipit.pixienv import Activation, parse_activation
from shipit.verbs.hook import sessionstart

TREE_ROOT = "/trees/SES01/WS01"

# A faithful `pixi shell-hook --json` blob (mirrors tests/test_pixienv.py's fixture):
# the complete env-var snapshot pixi's activation produces, plus a value that NEEDS
# quoting so the export rendering is exercised end to end.
SHELL_HOOK_JSON = json.dumps(
    {
        "environment_variables": {
            "PATH": f"{TREE_ROOT}/.pixi/envs/default/bin:/usr/bin:/bin",
            "CONDA_PREFIX": f"{TREE_ROOT}/.pixi/envs/default",
            "CONDA_DEFAULT_ENV": "shipit",
            "PIXI_PROMPT": "(shipit) ",
        },
        "activation_scripts": [],
    }
)


def _fake_runner(captured: dict):
    """A `proc.run`-shaped stub that records argv and returns the fixture JSON."""

    def runner(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=SHELL_HOOK_JSON, stderr="")

    return runner


def _run(payload: dict | str, env: dict, runner=None) -> int:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    kwargs = {"runner": runner} if runner is not None else {}
    return sessionstart.run(stdin=io.StringIO(text), environ=env, **kwargs)


@pytest.fixture
def pixi_repo(tmp_path):
    """A checkout with a pixi.toml at its root and a nested working dir."""
    (tmp_path / "pixi.toml").write_text('[project]\nname = "x"\n')
    nested = tmp_path / "src" / "pkg"
    nested.mkdir(parents=True)
    return tmp_path


# --------------------------------------------------------------------------
# Pure core — the toolchain→activation mapping
# --------------------------------------------------------------------------


def test_pixi_toolchain_maps_to_export_lines():
    # pixi → the shell-hook snapshot rendered as pure `export` lines, in pixi's
    # order — sourceable as a preamble (no functions, nothing interactive).
    toolchain = activation.Toolchain(kind=activation.PIXI, manifest=Path("pixi.toml"))
    script = activation.activation_script(toolchain, parse_activation(SHELL_HOOK_JSON))
    assert script.splitlines() == [
        f"export PATH={TREE_ROOT}/.pixi/envs/default/bin:/usr/bin:/bin",
        f"export CONDA_PREFIX={TREE_ROOT}/.pixi/envs/default",
        "export CONDA_DEFAULT_ENV=shipit",
        "export PIXI_PROMPT='(shipit) '",  # embedded space+parens → quoted
    ]


def test_no_toolchain_maps_to_empty():
    # Non-pixi → the EMPTY script: the graceful-no-op half of the mapping.
    act = parse_activation(SHELL_HOOK_JSON)
    assert activation.activation_script(None, act) == ""
    assert activation.activation_script(None, None) == ""


def test_unknown_toolchain_kind_maps_to_empty():
    # The kind-keyed dispatch is the extension seam: an unmapped kind degrades to
    # the no-op script rather than guessing an activation.
    toolchain = activation.Toolchain(kind="npm", manifest=Path("package.json"))
    assert (
        activation.activation_script(toolchain, parse_activation(SHELL_HOOK_JSON)) == ""
    )


def test_export_lines_skip_non_identifier_keys():
    # A key that cannot be a shell identifier cannot become an `export` line;
    # it is dropped rather than written broken into a sourced preamble.
    act = Activation(
        environment_variables={"OK": "1", "BAD-KEY": "x", "2BAD": "y"},
        activation_scripts=(),
    )
    assert activation.export_lines(act) == "export OK=1"


def test_export_lines_quote_hostile_values():
    # A value with quotes/spaces/expansions survives sourcing VERBATIM.
    act = Activation(
        environment_variables={"HOSTILE": "a 'b' $(rm -rf /) $HOME"},
        activation_scripts=(),
    )
    line = activation.export_lines(act)
    assert line == """export HOSTILE='a '"'"'b'"'"' $(rm -rf /) $HOME'"""


# --------------------------------------------------------------------------
# Manifest resolution — from the session's cwd
# --------------------------------------------------------------------------


def test_detect_toolchain_walks_up_to_the_manifest(pixi_repo):
    # Resolved from the session cwd like pixi's own discovery: a nested cwd still
    # finds the root pixi.toml.
    toolchain = activation.detect_toolchain(pixi_repo / "src" / "pkg")
    assert toolchain == activation.Toolchain(
        kind=activation.PIXI, manifest=(pixi_repo / "pixi.toml").resolve()
    )


def test_detect_toolchain_none_without_a_manifest(tmp_path):
    assert activation.detect_toolchain(tmp_path) is None


# --------------------------------------------------------------------------
# Boundary — fail-open, no-op without a toolchain, append-only writes
# --------------------------------------------------------------------------


def test_pixi_repo_activation_lands_in_the_env_file(pixi_repo, tmp_path):
    # The happy path: payload cwd → manifest → `pixi shell-hook --json` (default
    # env: no `--environment` flag) → export lines appended to CLAUDE_ENV_FILE.
    env_file = tmp_path / "claude-env"
    captured: dict = {}
    code = _run(
        {"hook_event_name": "SessionStart", "cwd": str(pixi_repo)},
        {"CLAUDE_ENV_FILE": str(env_file)},
        runner=_fake_runner(captured),
    )
    assert code == 0
    assert captured["cmd"][:3] == ["pixi", "shell-hook", "--json"]
    assert "--environment" not in captured["cmd"]  # default env
    assert str((pixi_repo / "pixi.toml").resolve()) in captured["cmd"]
    content = env_file.read_text()
    assert f"export CONDA_PREFIX={TREE_ROOT}/.pixi/envs/default\n" in content
    assert "export CONDA_DEFAULT_ENV=shipit" in content


def test_env_file_is_appended_never_clobbered(pixi_repo, tmp_path):
    # CLAUDE_ENV_FILE is a shared seam other SessionStart hooks write to; this
    # boundary owns only its own lines.
    env_file = tmp_path / "claude-env"
    env_file.write_text("export OTHER_HOOK=kept\n")
    code = _run(
        {"cwd": str(pixi_repo)},
        {"CLAUDE_ENV_FILE": str(env_file)},
        runner=_fake_runner({}),
    )
    assert code == 0
    content = env_file.read_text()
    assert content.startswith("export OTHER_HOOK=kept\n")
    assert "export CONDA_DEFAULT_ENV=shipit" in content


def test_non_pixi_repo_is_a_clean_noop(tmp_path):
    # No activatable toolchain → exit 0, nothing written, pixi never invoked.
    env_file = tmp_path / "claude-env"

    def exploding_runner(cmd, **kwargs):  # pragma: no cover — must not be reached
        raise AssertionError("no toolchain — pixi must not run")

    code = _run(
        {"cwd": str(tmp_path)},
        {"CLAUDE_ENV_FILE": str(env_file)},
        runner=exploding_runner,
    )
    assert code == 0
    assert not env_file.exists()


def test_missing_env_file_var_is_a_noop(pixi_repo):
    # Without CLAUDE_ENV_FILE there is nowhere to write — no-op, pixi never runs.
    def exploding_runner(cmd, **kwargs):  # pragma: no cover — must not be reached
        raise AssertionError("no CLAUDE_ENV_FILE — pixi must not run")

    assert _run({"cwd": str(pixi_repo)}, {}, runner=exploding_runner) == 0


def test_malformed_payload_falls_back_to_process_cwd(pixi_repo, tmp_path, monkeypatch):
    # Hooks run in the project dir, so a garbage payload degrades to Path.cwd()
    # and activation still lands (fail-open never means fail-useless).
    monkeypatch.chdir(pixi_repo)
    env_file = tmp_path / "claude-env"
    code = _run(
        "not json at all",
        {"CLAUDE_ENV_FILE": str(env_file)},
        runner=_fake_runner({}),
    )
    assert code == 0
    assert "export CONDA_DEFAULT_ENV=shipit" in env_file.read_text()


def test_pixi_failure_fails_open(pixi_repo, tmp_path):
    # A pixi error (missing binary, solve failure, …) costs the session NOTHING:
    # exit 0, no partial write. Activation is additive, never load-bearing.
    env_file = tmp_path / "claude-env"

    def failing_runner(cmd, **kwargs):
        raise RuntimeError("pixi exploded")

    code = _run(
        {"cwd": str(pixi_repo)},
        {"CLAUDE_ENV_FILE": str(env_file)},
        runner=failing_runner,
    )
    assert code == 0
    assert not env_file.exists()


def test_unwritable_env_file_fails_open(pixi_repo, tmp_path):
    # Even the final write failing must not surface: the env-file path is a
    # directory here, so open() raises — swallowed, exit 0.
    code = _run(
        {"cwd": str(pixi_repo)},
        {"CLAUDE_ENV_FILE": str(tmp_path)},
        runner=_fake_runner({}),
    )
    assert code == 0
