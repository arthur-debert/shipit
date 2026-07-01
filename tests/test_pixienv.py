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
import shutil
import tomllib
from pathlib import Path

import pytest

from shipit import pixienv
from shipit.pixienv import read

#: The repo root (tests/ lives directly under it) — used to read the real `pixi.toml`
#: `[activation.env]` and, when a provisioned env is present, to smoke `pixi shell-hook`.
REPO_ROOT = Path(__file__).resolve().parents[1]

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

# A faithful `pixi shell-hook --json` blob: the env vars pixi sets on activation plus the
# (usually empty) activation_scripts list. The three ADR-0015 build vars are present with
# `$PIXI_PROJECT_ROOT` already EXPANDED to the per-Tree prefix — exactly what pixi emits
# from `pixi.toml`'s `[activation.env]` (`test_declared_activation_env_keys_are_covered`
# ties this fixture back to the manifest so the two cannot drift).
TREE_ROOT = "/trees/COR01/WS04"
SHELL_HOOK_JSON = json.dumps(
    {
        "environment_variables": {
            "PATH": f"{TREE_ROOT}/.pixi/envs/default/bin:/usr/bin:/bin",
            "CONDA_PREFIX": f"{TREE_ROOT}/.pixi/envs/default",
            "CONDA_DEFAULT_ENV": "shipit",
            "CARGO_TARGET_DIR": f"{TREE_ROOT}/target",
            "SCCACHE_BASEDIRS": TREE_ROOT,
            "CARGO_INCREMENTAL": "0",
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


def test_read_env_identity_absent_is_none(tmp_path: Path):
    # An un-provisioned prefix has no conda-meta/pixi yet: read_env_identity returns
    # None (like read_fingerprint), it does NOT raise FileNotFoundError.
    (tmp_path / read.CONDA_META).mkdir()
    assert read.read_env_identity(tmp_path) is None


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


def test_activation_snapshots_a_directly_constructed_mutable_mapping():
    # frozen=True freezes the field binding, not the referent: a caller passing a plain
    # dict must NOT retain a mutation handle. __post_init__ snapshots into a private dict
    # and exposes a read-only view, so post-construction mutation of the caller's dict
    # cannot reach the value object (ADR-0021 value-object discipline).
    source = {"CARGO_TARGET_DIR": "/trees/A/target"}
    act = pixienv.Activation(environment_variables=source, activation_scripts=())

    source["CARGO_TARGET_DIR"] = "/trees/B/target"  # mutate the caller-held dict
    source["LEAKED"] = "x"

    assert act.environment_variables["CARGO_TARGET_DIR"] == "/trees/A/target"
    assert "LEAKED" not in act.environment_variables
    try:
        act.environment_variables["NEW"] = "x"  # type: ignore[index]
    except TypeError:
        pass
    else:
        raise AssertionError("directly-constructed Activation must be read-only too")


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


def test_shell_hook_runs_pixi_json_and_parses():
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


# --------------------------------------------------------------------------
# Load-bearing [activation.env] behavior — fixture pinned to the real manifest,
# plus a skip-guarded real `pixi shell-hook` smoke (ADR-0015 / ADR-0022).
# --------------------------------------------------------------------------


def _declared_activation_env() -> dict[str, str]:
    """The repo's real ``pixi.toml`` ``[activation.env]`` table (raw, unexpanded)."""
    data = tomllib.loads((REPO_ROOT / "pixi.toml").read_text())
    activation = data.get("activation", {})
    return dict(activation.get("env", {}))


def test_declared_activation_env_keys_are_covered():
    # The SHELL_HOOK_JSON fixture is only trustworthy if it mirrors what the manifest
    # actually declares. Read the real `[activation.env]` and assert (a) it still carries
    # the three ADR-0015 build vars, and (b) the fixture parses a value for EACH declared
    # key whose EXPANSION matches the raw `$PIXI_PROJECT_ROOT` template — so a manifest
    # edit (new/renamed/removed var, or a template that no longer roots under the project)
    # fails here instead of silently leaving the parse test green (codex WARNING).
    declared = _declared_activation_env()
    assert {"CARGO_TARGET_DIR", "SCCACHE_BASEDIRS", "CARGO_INCREMENTAL"} <= set(
        declared
    )

    act = pixienv.parse_activation(SHELL_HOOK_JSON)
    for key, raw in declared.items():
        assert key in act.environment_variables, f"fixture missing declared {key}"
        expected = raw.replace("$PIXI_PROJECT_ROOT", TREE_ROOT)
        assert act.environment_variables[key] == expected, (
            f"fixture value for {key} does not match the manifest template {raw!r}"
        )


def _shell_hook_value(env: object, var: str) -> str | None:
    """The effective value pixi's shell-hook reports for ``var``.

    When shell-hook runs OUTSIDE an activation (SHLVL 1) the vars are bare keys; when it
    runs INSIDE one (SHLVL 2 — e.g. the suite under `pixi run`) pixi instead emits the
    restore value under a stacked ``CONDA_ENV_SHLVL_<n>_<VAR>`` backup key. Resolve either
    shape so the smoke is robust to the activation depth it happens to run at.
    """
    if var in env:  # type: ignore[operator]
        return env[var]  # type: ignore[index]
    matches = {v for k, v in env.items() if k.endswith(f"_{var}")}  # type: ignore[union-attr]
    return next(iter(matches)) if len(matches) == 1 else None


def test_declared_activation_env_appears_in_real_shell_hook():
    # Smoke the real thing when (and only when) pixi and a provisioned default env are
    # present: a typo, a pixi expansion-behavior change, or unsupported
    # `$PIXI_PROJECT_ROOT` syntax in the manifest would leave every fixture test green
    # while breaking activation for real. `pixi shell-hook` is cheap (no solve when the
    # lock is unchanged); we skip rather than provision so the suite stays hermetic
    # off a provisioned checkout (codex WARNING).
    if shutil.which("pixi") is None:
        pytest.skip("pixi not on PATH")
    if not (REPO_ROOT / ".pixi" / "envs" / "default").exists():
        pytest.skip("no provisioned default env — refusing to trigger a solve")

    try:
        act = read.shell_hook(REPO_ROOT / "pixi.toml")
    except Exception as exc:  # noqa: BLE001 — any pixi/subprocess failure → skip, never fail
        pytest.skip(f"pixi shell-hook unavailable: {exc}")

    env = act.environment_variables
    # `$PIXI_PROJECT_ROOT` really expanded to THIS repo's absolute root, per-project —
    # the assurance the fabricated fixture alone cannot give.
    assert _shell_hook_value(env, "CARGO_TARGET_DIR") == str(REPO_ROOT / "target")
    assert _shell_hook_value(env, "SCCACHE_BASEDIRS") == str(REPO_ROOT)
    assert _shell_hook_value(env, "CARGO_INCREMENTAL") == "0"
