"""Unit tests for ``shipit.pixienv`` — pixi JSON → value objects + pure transforms.

The whole module is a functional core over an injected boundary (ADR-0021/0022), so the
tests feed CAPTURED pixi JSON (the shapes observed live against pixi 0.71.0) and assert
the returned value objects and the pure env transforms — no live pixi, no network. The
one I/O helper (:func:`shipit.pixienv.read.shell_hook`) is exercised through an injected
fake runner, and the on-disk readers through a ``tmp_path`` prefix.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from shipit import pixienv
from shipit.pixienv import read

# A faithful `conda-meta/pixi` blob (docs/dev/pixi §2): note environment_lock_file_hash
# is DISTINCT from the bare .pixi-environment-fingerprint.
ENV_IDENTITY_JSON = json.dumps(
    {
        "manifest_path": "/trees/COR01/WS04/pixi.toml",
        "environment_name": "default",
        "pixi_version": "0.71.0",
        "environment_lock_file_hash": "99f00798db0ea80c",
        "resolved_platform": {
            "subdir": "osx-arm64",
            "virtual_packages": ["__unix=0=0", "__osx=13.0", "__archspec=0=m1"],
        },
        "minimum_supported_platform": {
            "subdir": "osx-arm64",
            "virtual_packages": ["__osx=11.0", "__unix=0"],
        },
    }
)

# A faithful `pixi shell-hook --json` blob (trimmed): the env vars pixi sets on activation
# plus the (usually empty) activation_scripts list.
SHELL_HOOK_JSON = json.dumps(
    {
        "environment_variables": {
            "PATH": "/trees/COR01/WS04/.pixi/envs/default/bin:/usr/bin:/bin",
            "CONDA_PREFIX": "/trees/COR01/WS04/.pixi/envs/default",
            "CONDA_DEFAULT_ENV": "shipit",
            "CARGO_TARGET_DIR": "/trees/COR01/WS04/target",
        },
        "activation_scripts": [],
    }
)


# --------------------------------------------------------------------------
# EnvIdentity — parse conda-meta/pixi
# --------------------------------------------------------------------------


def test_parse_env_identity_mirrors_conda_meta_pixi():
    ident = pixienv.parse_env_identity(ENV_IDENTITY_JSON)
    assert ident == pixienv.EnvIdentity(
        manifest_path=Path("/trees/COR01/WS04/pixi.toml"),
        environment_name="default",
        pixi_version="0.71.0",
        environment_lock_file_hash="99f00798db0ea80c",
        resolved_platform=pixienv.Platform(
            subdir="osx-arm64",
            virtual_packages=("__unix=0=0", "__osx=13.0", "__archspec=0=m1"),
        ),
    )


def test_env_identity_lock_hash_is_not_the_bare_fingerprint(tmp_path: Path):
    # docs/dev/pixi §2: the two digests differ for the SAME prefix and must not be
    # conflated. EnvIdentity carries the lock hash; the fingerprint is read separately.
    prefix = tmp_path
    meta = prefix / read.CONDA_META
    meta.mkdir()
    (meta / read.ENV_IDENTITY_FILE).write_text(ENV_IDENTITY_JSON)
    (meta / read.FINGERPRINT_FILE).write_text("99b739d0fedb92eb\n")

    ident = read.read_env_identity(prefix)
    fingerprint = read.read_fingerprint(prefix)

    assert ident.environment_lock_file_hash == "99f00798db0ea80c"
    assert fingerprint == "99b739d0fedb92eb"
    assert ident.environment_lock_file_hash != fingerprint


def test_read_fingerprint_absent_is_none(tmp_path: Path):
    (tmp_path / read.CONDA_META).mkdir()
    assert read.read_fingerprint(tmp_path) is None


def test_parse_env_identity_tolerates_missing_platform():
    data = json.loads(ENV_IDENTITY_JSON)
    del data["resolved_platform"]
    ident = pixienv.parse_env_identity(json.dumps(data))
    assert ident.resolved_platform == pixienv.Platform(subdir="", virtual_packages=())


# --------------------------------------------------------------------------
# Activation — parse shell-hook --json
# --------------------------------------------------------------------------


def test_parse_activation_mirrors_shell_hook_json():
    act = pixienv.parse_activation(SHELL_HOOK_JSON)
    assert act.activation_scripts == ()
    assert act.environment_variables["CONDA_DEFAULT_ENV"] == "shipit"
    assert act.environment_variables["CARGO_TARGET_DIR"] == "/trees/COR01/WS04/target"


def test_activation_environment_variables_are_read_only():
    act = pixienv.parse_activation(SHELL_HOOK_JSON)
    # The snapshot cannot be mutated after capture (immutable snapshot, ADR-0021).
    try:
        act.environment_variables["NEW"] = "x"  # type: ignore[index]
    except TypeError:
        pass
    else:
        raise AssertionError("Activation env vars should be read-only")


# --------------------------------------------------------------------------
# Pure env transforms
# --------------------------------------------------------------------------


def test_activation_delta_is_only_added_or_changed_keys():
    act = pixienv.parse_activation(SHELL_HOOK_JSON)
    base = {
        # unchanged — pixi reports the same value, so it is NOT in the delta
        "CONDA_DEFAULT_ENV": "shipit",
        # changed — a different PATH before activation
        "PATH": "/usr/bin:/bin",
        # a base var pixi does not touch — absent from the delta
        "HOME": "/home/me",
    }
    delta = pixienv.activation_delta(base, act)

    assert "CONDA_DEFAULT_ENV" not in delta  # equal value → not a change
    assert "HOME" not in delta  # base-only → not part of activation
    assert delta["PATH"] == "/trees/COR01/WS04/.pixi/envs/default/bin:/usr/bin:/bin"
    assert delta["CONDA_PREFIX"] == "/trees/COR01/WS04/.pixi/envs/default"
    assert delta["CARGO_TARGET_DIR"] == "/trees/COR01/WS04/target"


def test_activation_delta_does_not_mutate_inputs():
    act = pixienv.parse_activation(SHELL_HOOK_JSON)
    base = {"PATH": "/usr/bin"}
    pixienv.activation_delta(base, act)
    assert base == {"PATH": "/usr/bin"}  # untouched


def test_activated_env_lays_activation_over_base():
    act = pixienv.parse_activation(SHELL_HOOK_JSON)
    base = {"HOME": "/home/me", "PATH": "/usr/bin:/bin"}
    merged = pixienv.activated_env(base, act)
    # base-only survives, pixi's vars win on conflict, and inputs are untouched
    assert merged["HOME"] == "/home/me"
    assert merged["PATH"] == "/trees/COR01/WS04/.pixi/envs/default/bin:/usr/bin:/bin"
    assert merged["CONDA_PREFIX"] == "/trees/COR01/WS04/.pixi/envs/default"
    assert base == {"HOME": "/home/me", "PATH": "/usr/bin:/bin"}


def test_path_entries_splits_pixi_path():
    act = pixienv.parse_activation(SHELL_HOOK_JSON)
    entries = pixienv.path_entries(act)
    assert entries[0] == "/trees/COR01/WS04/.pixi/envs/default/bin"
    assert entries == tuple(act.environment_variables["PATH"].split(os.pathsep))


def test_path_entries_empty_when_unset():
    act = pixienv.activation_from_dict({"environment_variables": {}})
    assert pixienv.path_entries(act) == ()


# --------------------------------------------------------------------------
# shell_hook boundary — injected runner (no real pixi)
# --------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


def test_shell_hook_runs_pixi_json_and_parses(monkeypatch):
    seen: dict[str, list[str]] = {}

    def fake_runner(cmd):
        seen["cmd"] = cmd
        return _FakeResult(SHELL_HOOK_JSON)

    act = read.shell_hook(Path("/trees/COR01/WS04/pixi.toml"), runner=fake_runner)

    assert seen["cmd"] == [
        "pixi",
        "shell-hook",
        "--json",
        "--manifest-path",
        "/trees/COR01/WS04/pixi.toml",
    ]
    assert act.environment_variables["CONDA_DEFAULT_ENV"] == "shipit"


def test_shell_hook_passes_environment_flag():
    seen: dict[str, list[str]] = {}

    def fake_runner(cmd):
        seen["cmd"] = cmd
        return _FakeResult(SHELL_HOOK_JSON)

    read.shell_hook(Path("/x/pixi.toml"), environment="lint", runner=fake_runner)
    assert "--environment" in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("--environment") + 1] == "lint"
