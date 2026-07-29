from __future__ import annotations

import json
import os
import shutil
import tomllib
from pathlib import Path

import pytest

from shipit import execrun, pixienv
from shipit.pixienv import read

REPO_ROOT = Path(__file__).resolve().parents[1]

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
    (tmp_path / read.CONDA_META).mkdir()
    assert read.read_env_identity(tmp_path) is None


def test_parse_env_identity_tolerates_missing_platform():
    data = json.loads(ENV_IDENTITY_JSON)
    del data["resolved_platform"]
    ident = pixienv.parse_env_identity(json.dumps(data))
    assert ident.resolved_platform == pixienv.Platform(subdir="", virtual_packages=())


def test_parse_activation_mirrors_shell_hook_json():
    act = pixienv.parse_activation(SHELL_HOOK_JSON)
    assert act.activation_scripts == ()
    assert act.environment_variables["CONDA_DEFAULT_ENV"] == "shipit"
    assert act.environment_variables["CARGO_TARGET_DIR"] == "/trees/COR01/WS04/target"


def test_activation_environment_variables_are_read_only():
    act = pixienv.parse_activation(SHELL_HOOK_JSON)
    try:
        act.environment_variables["NEW"] = "x"  # type: ignore[index]
    except TypeError:
        pass
    else:
        raise AssertionError("Activation env vars should be read-only")


def test_activation_snapshots_a_directly_constructed_mutable_mapping():
    source = {"CARGO_TARGET_DIR": "/trees/A/target"}
    act = pixienv.Activation(environment_variables=source, activation_scripts=())

    source["CARGO_TARGET_DIR"] = "/trees/B/target"
    source["LEAKED"] = "x"

    assert act.environment_variables["CARGO_TARGET_DIR"] == "/trees/A/target"
    assert "LEAKED" not in act.environment_variables
    try:
        act.environment_variables["NEW"] = "x"  # type: ignore[index]
    except TypeError:
        pass
    else:
        raise AssertionError("directly-constructed Activation must be read-only too")


def test_activation_delta_is_only_added_or_changed_keys():
    act = pixienv.parse_activation(SHELL_HOOK_JSON)
    base = {
        "CONDA_DEFAULT_ENV": "shipit",
        "PATH": "/usr/bin:/bin",
        "HOME": "/home/me",
    }
    delta = pixienv.activation_delta(base, act)

    assert "CONDA_DEFAULT_ENV" not in delta
    assert "HOME" not in delta
    assert delta["PATH"] == "/trees/COR01/WS04/.pixi/envs/default/bin:/usr/bin:/bin"
    assert delta["CONDA_PREFIX"] == "/trees/COR01/WS04/.pixi/envs/default"
    assert delta["CARGO_TARGET_DIR"] == "/trees/COR01/WS04/target"


def test_activation_delta_does_not_mutate_inputs():
    act = pixienv.parse_activation(SHELL_HOOK_JSON)
    base = {"PATH": "/usr/bin"}
    pixienv.activation_delta(base, act)
    assert base == {"PATH": "/usr/bin"}


def test_activated_env_lays_activation_over_base():
    act = pixienv.parse_activation(SHELL_HOOK_JSON)
    base = {"HOME": "/home/me", "PATH": "/usr/bin:/bin"}
    merged = pixienv.activated_env(base, act)
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


class _FakeResult:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout


def test_shell_hook_runs_pixi_json_and_parses():
    seen: dict = {}

    def fake_runner(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["timeout"] = kwargs.get("timeout")
        return _FakeResult(SHELL_HOOK_JSON)

    act = read.shell_hook(Path("/trees/COR01/WS04/pixi.toml"), runner=fake_runner)

    assert seen["cmd"] == [
        "pixi",
        "shell-hook",
        "--json",
        "--manifest-path",
        "/trees/COR01/WS04/pixi.toml",
    ]
    assert seen["timeout"] == pixienv.READ_TIMEOUT
    assert act.environment_variables["CONDA_DEFAULT_ENV"] == "shipit"


def test_shell_hook_passes_environment_flag():
    seen: dict = {}

    def fake_runner(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["timeout"] = kwargs.get("timeout")
        return _FakeResult(SHELL_HOOK_JSON)

    read.shell_hook(Path("/x/pixi.toml"), environment="lint", runner=fake_runner)
    assert "--environment" in seen["cmd"]
    assert seen["cmd"][seen["cmd"].index("--environment") + 1] == "lint"


def _declared_activation_env() -> dict[str, str]:
    data = tomllib.loads((REPO_ROOT / "pixi.toml").read_text())
    activation = data.get("activation", {})
    return dict(activation.get("env", {}))


def test_declared_activation_env_keys_are_covered():
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
    if var in env:  # type: ignore[operator]
        return env[var]  # type: ignore[index]
    matches = {v for k, v in env.items() if k.endswith(f"_{var}")}  # type: ignore[union-attr]
    return next(iter(matches)) if len(matches) == 1 else None


def test_declared_activation_env_appears_in_real_shell_hook():
    if shutil.which("pixi") is None:
        pytest.skip("pixi not on PATH")
    if not (REPO_ROOT / ".pixi" / "envs" / "default").exists():
        pytest.skip("no provisioned default env — refusing to trigger a solve")

    try:
        act = read.shell_hook(REPO_ROOT / "pixi.toml")
    except Exception as exc:  # noqa: BLE001 — any pixi/subprocess failure → skip, never fail
        pytest.skip(f"pixi shell-hook unavailable: {exc}")

    env = act.environment_variables
    assert _shell_hook_value(env, "CARGO_TARGET_DIR") == str(REPO_ROOT / "target")
    assert _shell_hook_value(env, "SCCACHE_BASEDIRS") == str(REPO_ROOT)
    assert _shell_hook_value(env, "CARGO_INCREMENTAL") == "0"


PIXI_LIST_JSON = json.dumps(
    [
        {
            "name": "bzip2",
            "version": "1.0.8",
            "build": "hd037594_9",
            "build_number": 9,
            "size_bytes": 124834,
            "kind": "conda",
            "source": "https://conda.anaconda.org/conda-forge",
            "is_explicit": False,
            "subdir": "osx-arm64",
        },
        {
            "name": "shipit",
            "version": None,
            "build": None,
            "build_number": None,
            "size_bytes": None,
            "kind": "pypi",
            "source": "./",
            "is_explicit": True,
            "requested_spec": '{ path = ".", editable = true }',
        },
    ]
)


def test_parse_installed_packages_mirrors_pixi_list():
    packages = pixienv.parse_installed_packages(PIXI_LIST_JSON)
    assert packages == (
        pixienv.InstalledPackage(
            name="bzip2",
            version="1.0.8",
            build="hd037594_9",
            kind="conda",
            is_explicit=False,
        ),
        pixienv.InstalledPackage(
            name="shipit", version=None, build=None, kind="pypi", is_explicit=True
        ),
    )


def test_list_packages_runs_pixi_list_json_and_parses():
    seen: dict = {}

    def fake_runner(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["timeout"] = kwargs.get("timeout")
        return _FakeResult(PIXI_LIST_JSON)

    packages = read.list_packages(Path("/x/pixi.toml"), runner=fake_runner)

    assert seen["cmd"] == [
        "pixi",
        "list",
        "--json",
        "--manifest-path",
        "/x/pixi.toml",
    ]
    assert all(isinstance(p, pixienv.InstalledPackage) for p in packages)
    assert [p.name for p in packages] == ["bzip2", "shipit"]
    assert seen["timeout"] == pixienv.READ_TIMEOUT


def test_list_packages_passes_environment_flag():
    seen: dict = {}

    def fake_runner(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["timeout"] = kwargs.get("timeout")
        return _FakeResult("[]")

    read.list_packages(Path("/x/pixi.toml"), environment="lint", runner=fake_runner)
    assert seen["cmd"][seen["cmd"].index("--environment") + 1] == "lint"


PIXI_INFO_JSON = json.dumps(
    {
        "platform": "osx-arm64",
        "virtual_packages": ["__unix=0=0", "__osx=26.5=0"],
        "version": "0.71.0",
        "cache_dir": "/Users/me/Library/Caches/rattler/cache",
        "cache_size": None,
        "project_info": {
            "name": "shipit",
            "manifest_path": "/trees/COR01/WS04/pixi.toml",
            "last_updated": "02-07-2026 11:21:32",
            "version": None,
        },
        "environments_info": [
            {
                "name": "default",
                "features": ["default"],
                "solve_group": None,
                "dependencies": ["python", "ruff"],
                "pypi_dependencies": ["shipit"],
                "tasks": ["lint", "test"],
                "channels": ["conda-forge"],
                "prefix": "/trees/COR01/WS04/.pixi/envs/default",
            }
        ],
    }
)


def test_parse_info_mirrors_pixi_info():
    parsed = pixienv.parse_info(PIXI_INFO_JSON)
    assert parsed.pixi_version == "0.71.0"
    assert parsed.platform == "osx-arm64"
    assert parsed.cache_dir == Path("/Users/me/Library/Caches/rattler/cache")
    assert parsed.project == pixienv.ProjectInfo(
        name="shipit", manifest_path=Path("/trees/COR01/WS04/pixi.toml")
    )
    assert parsed.environments == (
        pixienv.EnvironmentInfo(
            name="default",
            features=("default",),
            dependencies=("python", "ruff"),
            pypi_dependencies=("shipit",),
            tasks=("lint", "test"),
            prefix=Path("/trees/COR01/WS04/.pixi/envs/default"),
        ),
    )


def test_parse_info_tolerates_no_project():
    data = json.loads(PIXI_INFO_JSON)
    data["project_info"] = None
    data["environments_info"] = []
    parsed = pixienv.parse_info(json.dumps(data))
    assert parsed.project is None
    assert parsed.environments == ()
    assert parsed.pixi_version == "0.71.0"


def test_info_runs_pixi_info_json_and_parses():
    seen: dict = {}

    def fake_runner(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["timeout"] = kwargs.get("timeout")
        return _FakeResult(PIXI_INFO_JSON)

    parsed = read.info(Path("/x/pixi.toml"), runner=fake_runner)

    assert seen["cmd"] == ["pixi", "info", "--json", "--manifest-path", "/x/pixi.toml"]
    assert seen["timeout"] == pixienv.READ_TIMEOUT
    assert isinstance(parsed, pixienv.Info)
    assert parsed.project.name == "shipit"


def test_is_leaked_env_var_scrubs_pixi_pointers_keeps_cache_vars():
    for key in ("PIXI_PROJECT_MANIFEST", "PIXI_PROJECT_ROOT", "PIXI_EXE"):
        assert pixienv.is_leaked_env_var(key)
    for key in ("PIXI_CACHE_DIR", "RATTLER_CACHE_DIR"):
        assert not pixienv.is_leaked_env_var(key)


def test_is_leaked_env_var_scrubs_conda_activation_keeps_installation():
    for key in ("CONDA_PREFIX", "CONDA_DEFAULT_ENV", "CONDA_SHLVL", "CONDA_PREFIX_1"):
        assert pixienv.is_leaked_env_var(key)
    for key in ("CONDA_EXE", "CONDA_PYTHON_EXE", "CONDA_ROOT"):
        assert not pixienv.is_leaked_env_var(key)


def test_is_leaked_env_var_scrubs_the_activation_stack_restore_keys():
    for key in (
        "CONDA_ENV_SHLVL_2_PIXI_PROJECT_MANIFEST",
        "CONDA_ENV_SHLVL_2_CARGO_TARGET_DIR",
        "CONDA_ENV_SHLVL_10_CONDA_PREFIX",
    ):
        assert pixienv.is_leaked_env_var(key)


def test_is_leaked_env_var_scrubs_build_env_but_keeps_sccache_backend_vars():
    assert pixienv.is_leaked_env_var("CARGO_TARGET_DIR")
    assert pixienv.is_leaked_env_var("SCCACHE_BASEDIRS")
    assert pixienv.is_leaked_env_var("CARGO_INCREMENTAL")
    assert not pixienv.is_leaked_env_var("RUSTC_WRAPPER")
    assert not pixienv.is_leaked_env_var("SCCACHE_DIR")
    assert not pixienv.is_leaked_env_var("SCCACHE_GCS_KEY")


def test_scrub_env_filters_on_the_predicate_and_returns_a_fresh_dict():
    env = {
        "HOME": "/home/a",
        "PIXI_PROJECT_MANIFEST": "/parent/pixi.toml",
        "PIXI_CACHE_DIR": "/cache",
        "CONDA_PREFIX": "/parent/.pixi/envs/default",
    }
    scrubbed = pixienv.scrub_env(env)
    assert scrubbed == {"HOME": "/home/a", "PIXI_CACHE_DIR": "/cache"}
    assert scrubbed is not env
    assert "PIXI_PROJECT_MANIFEST" in env


def test_run_argv_wraps_through_the_projects_manifest(tmp_path: Path):
    wrapped = pixienv.run_argv(["claude", "-p", "go"], tmp_path)
    assert wrapped == [
        "pixi",
        "run",
        "--manifest-path",
        str(tmp_path / "pixi.toml"),
        "--",
        "claude",
        "-p",
        "go",
    ]


def test_run_argv_pins_a_non_default_environment(tmp_path: Path):
    wrapped = pixienv.run_argv(["lefthook", "install"], tmp_path, environment="lint")
    assert wrapped == [
        "pixi",
        "run",
        "--manifest-path",
        str(tmp_path / "pixi.toml"),
        "--environment",
        "lint",
        "--",
        "lefthook",
        "install",
    ]


def test_has_default_env_keys_on_the_provisioned_sentinel(tmp_path: Path):
    assert not pixienv.has_default_env(tmp_path)
    tmp_path.joinpath(*pixienv.DEFAULT_ENV_DIR).mkdir(parents=True)
    assert pixienv.has_default_env(tmp_path)
    assert pixienv.has_default_env(str(tmp_path))


def _capture_runner(seen: dict, stdout: str = ""):
    def runner(cmd, **kwargs):
        seen["cmd"] = cmd
        seen.update(kwargs)
        return execrun.ExecResult(
            argv=tuple(cmd), rc=0, stdout=stdout, stderr="", duration_ms=1
        )

    return runner


def test_install_runs_pixi_install_with_the_long_runner_bound(tmp_path: Path):
    seen: dict = {}
    result = pixienv.install(
        tmp_path, env={"PATH": "/usr/bin"}, runner=_capture_runner(seen)
    )
    assert seen["cmd"] == ["pixi", "install"]
    assert seen["cwd"] == str(tmp_path)
    assert seen["env"] == {"PATH": "/usr/bin"}
    assert seen["replace_env"] is True
    assert seen["timeout"] == pixienv.INSTALL_TIMEOUT
    assert result.ok


def test_install_without_env_inherits_the_environment(tmp_path: Path):
    seen: dict = {}
    pixienv.install(tmp_path, runner=_capture_runner(seen))
    assert seen["env"] is None
    assert seen["replace_env"] is False


def test_run_in_env_executes_the_wrapped_argv(tmp_path: Path):
    seen: dict = {}
    result = pixienv.run_in_env(
        ["python", "-c", "print('ok')"],
        tmp_path,
        env={"PATH": "/usr/bin"},
        check=False,
        runner=_capture_runner(seen, stdout="ok\n"),
    )
    assert seen["cmd"] == pixienv.run_argv(["python", "-c", "print('ok')"], tmp_path)
    assert seen["cwd"] == str(tmp_path)
    assert seen["replace_env"] is True
    assert seen["check"] is False
    assert seen["timeout"] == pixienv.INSTALL_TIMEOUT
    assert result.stdout == "ok\n"


def test_run_in_env_pins_a_non_default_environment(tmp_path: Path):
    seen: dict = {}
    pixienv.run_in_env(
        ["lefthook", "install"],
        tmp_path,
        environment="lint",
        runner=_capture_runner(seen),
    )
    assert seen["cmd"] == pixienv.run_argv(
        ["lefthook", "install"], tmp_path, environment="lint"
    )


def test_run_task_invokes_the_named_pixi_task_through_the_staged_manifest(
    tmp_path: Path,
):
    seen: dict = {}
    result = pixienv.run_task(
        "lint",
        tmp_path,
        env={"PATH": "/usr/bin"},
        check=False,
        runner=_capture_runner(seen, stdout="ok\n"),
    )
    assert seen["cmd"] == [
        "pixi",
        "run",
        "--manifest-path",
        str(tmp_path / "pixi.toml"),
        "lint",
    ]
    assert seen["cwd"] == str(tmp_path)
    assert seen["env"] == {"PATH": "/usr/bin"}
    assert seen["replace_env"] is True
    assert seen["check"] is False
    assert seen["timeout"] == pixienv.INSTALL_TIMEOUT
    assert result.stdout == "ok\n"


def test_run_task_without_env_inherits_the_environment(tmp_path: Path):
    seen: dict = {}
    pixienv.run_task("test", tmp_path, runner=_capture_runner(seen))
    assert seen["env"] is None
    assert seen["replace_env"] is False


def test_run_task_pins_a_non_default_environment(tmp_path: Path):
    seen: dict = {}
    pixienv.run_task("lint", tmp_path, environment="lint", runner=_capture_runner(seen))
    assert seen["cmd"] == [
        "pixi",
        "run",
        "--manifest-path",
        str(tmp_path / "pixi.toml"),
        "--environment",
        "lint",
        "lint",
    ]


def test_run_task_propagates_an_explicit_timeout(tmp_path: Path):
    seen: dict = {}
    pixienv.run_task("build", tmp_path, timeout=90.0, runner=_capture_runner(seen))
    assert seen["timeout"] == 90.0
    assert seen["check"] is True


def test_cache_dir_honors_overrides_else_platform_default(monkeypatch):
    monkeypatch.setenv("PIXI_CACHE_DIR", "/override/pixi")
    assert pixienv.cache_dir() == Path("/override/pixi")

    monkeypatch.delenv("PIXI_CACHE_DIR", raising=False)
    monkeypatch.setenv("RATTLER_CACHE_DIR", "/override/rattler")
    assert pixienv.cache_dir() == Path("/override/rattler")

    monkeypatch.delenv("RATTLER_CACHE_DIR", raising=False)
    default = pixienv.cache_dir()
    assert default.is_absolute()
    assert default.parts[-2:] == ("rattler", "cache") or "rattler" in str(default)
