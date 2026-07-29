from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from shipit import config
from shipit.tools import lanes


def _lane(name, run=None, **kwargs):
    return config.Lane(name=name, run=run or name, **kwargs)


PLAIN = (
    _lane("lint", required=True, local=True),
    _lane("test", required=True, local=True),
)


def test_normalize_accepts_the_planner_vocabulary_verbatim():
    assert [lanes.normalize_event(e) for e in lanes.EVENTS] == list(lanes.EVENTS)


@pytest.mark.parametrize(
    ("github_name", "event"),
    [
        ("pull_request", "pr"),
        ("push", "push"),
        ("schedule", "nightly"),
        ("workflow_dispatch", "dispatch"),
    ],
)
def test_normalize_maps_the_github_event_names(github_name, event):
    assert lanes.normalize_event(github_name) == event


def test_normalize_rejects_an_unknown_event_naming_both_vocabularies():
    with pytest.raises(lanes.LanePlanError) as exc_info:
        lanes.normalize_event("PR")
    message = str(exc_info.value)
    assert "unknown event 'PR'" in message
    assert "pr" in message and "workflow_dispatch" in message


def test_plan_refuses_an_unnormalized_event_as_a_caller_bug():
    with pytest.raises(ValueError):
        lanes.plan(PLAIN, event="pull_request")


LADDERED = (
    _lane("lint", required=True, local=True),
    _lane("deploy-preview", trigger="push"),
    _lane("gpu-e2e", trigger="nightly", runner="gpu-runner"),
    _lane("fleet-sweep", trigger="dispatch"),
)


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ("pr", ["lint"]),
        ("push", ["lint", "deploy-preview"]),
        ("nightly", ["lint", "deploy-preview", "gpu-e2e"]),
        ("dispatch", ["lint", "deploy-preview", "gpu-e2e", "fleet-sweep"]),
    ],
)
def test_trigger_ladder_runs_everything_at_or_before_the_event(event, expected):
    planned = lanes.plan(LADDERED, event=event)
    assert [job.name for job in planned] == expected


def test_matrix_preserves_declaration_order_and_fills_the_default_runner():
    planned = lanes.plan(LADDERED, event="nightly")
    assert [job.runner for job in planned] == [
        lanes.DEFAULT_RUNNER,
        lanes.DEFAULT_RUNNER,
        "gpu-runner",
    ]
    assert planned[0].as_matrix_entry() == {
        "name": "lint",
        "run": "lint",
        "runner": "ubuntu-latest",
        "required": True,
        "envs": "default",
        "envset": "default",
        "caches": {"rust": False, "sccache": False, "uv": False},
        "rust_workspaces": "",
        "secrets": [],
    }


def test_matrix_carries_the_required_flag_so_advisory_lanes_never_block_merge():
    planned = lanes.plan(LADDERED, event="dispatch")
    required = {job.name: job.required for job in planned}
    assert required == {
        "lint": True,
        "deploy-preview": False,
        "gpu-e2e": False,
        "fleet-sweep": False,
    }
    assert planned[1].as_matrix_entry()["required"] is False


def test_pixi_task_env_sets_resolve_feature_tasks_to_their_environments():
    pixi = {
        "tasks": {"changelog": "./bin/shipit changelog"},
        "feature": {
            "lint": {"tasks": {"lint": "./bin/shipit lint"}},
            "test": {"tasks": {"test": "./bin/shipit test"}},
            "shared": {"tasks": {"verify": "verify"}},
        },
        "environments": {
            "lint": ["lint"],
            "test": ["test"],
            "dogfood": {"features": ["shared"]},
        },
    }
    assert lanes.task_env_sets(pixi) == {
        "changelog": ("default",),
        "lint": ("lint",),
        "test": ("test",),
        "verify": ("dogfood",),
    }


def test_pixi_task_commands_resolve_string_and_cmd_table_tasks():
    pixi = {
        "tasks": {"changelog": "./bin/shipit changelog"},
        "feature": {
            "lint": {"tasks": {"lint": {"cmd": "./bin/shipit lint"}}},
            "test": {"tasks": {"test": "./bin/shipit test"}},
        },
    }
    assert lanes.task_commands(pixi) == {
        "changelog": "./bin/shipit changelog",
        "lint": "./bin/shipit lint",
        "test": "./bin/shipit test",
    }


def test_matrix_carries_env_set_and_cache_descriptors_from_the_planner():
    declared = (
        _lane("lint", run="lint", required=True),
        _lane("test", run="test rust", required=True),
    )
    toolchains = (
        config.ToolchainEntry(path="crates/a", toolchain="rust", commands={}),
        config.ToolchainEntry(path="web", toolchain="npm", commands={}),
    )
    planned = lanes.plan(
        declared,
        event="pr",
        task_envs={"lint": ("lint",), "test": ("test",)},
        toolchains=toolchains,
    )
    assert planned[0].as_matrix_entry() == {
        "name": "lint",
        "run": "lint",
        "runner": "ubuntu-latest",
        "required": True,
        "envs": "lint",
        "envset": "lint",
        "caches": {"rust": False, "sccache": False, "uv": False},
        "rust_workspaces": "",
        "secrets": [],
    }
    assert planned[1].as_matrix_entry() == {
        "name": "test",
        "run": "test rust",
        "runner": "ubuntu-latest",
        "required": True,
        "envs": "test",
        "envset": "test",
        "caches": {"rust": True, "sccache": False, "uv": False},
        "rust_workspaces": "crates/a -> ../../target",
        "secrets": [],
    }


def test_matrix_carries_the_declared_secrets_allowlist_as_a_json_array():
    declared = (
        _lane("wasm", run="test wasm", required=True, secrets=("lane_token",)),
        _lane("lint", run="lint", required=True),
    )
    planned = lanes.plan(declared, event="pr")
    assert planned[0].as_matrix_entry()["secrets"] == ["lane_token"]
    assert planned[1].as_matrix_entry()["secrets"] == []


def test_matrix_infers_rust_cache_from_pixi_task_aliases():
    declared = (_lane("wasm", run="build crates/wasm", required=True),)
    toolchains = (
        config.ToolchainEntry(path="crates/wasm", toolchain="rust", commands={}),
        config.ToolchainEntry(path="web", toolchain="npm", commands={}),
    )
    planned = lanes.plan(
        declared,
        event="pr",
        task_cmds={"build": "./bin/shipit build"},
        toolchains=toolchains,
    )
    assert planned[0].as_matrix_entry()["caches"] == {
        "rust": True,
        "sccache": False,
        "uv": False,
    }
    assert planned[0].as_matrix_entry()["rust_workspaces"] == (
        "crates/wasm -> ../../target"
    )


def test_matrix_cache_selector_skips_options_before_the_leg_selector():
    declared = (_lane("test", run="test --fail-fast crates/a", required=True),)
    toolchains = (
        config.ToolchainEntry(path="crates/a", toolchain="rust", commands={}),
        config.ToolchainEntry(path="web", toolchain="npm", commands={}),
    )
    planned = lanes.plan(declared, event="pr", toolchains=toolchains)
    assert planned[0].as_matrix_entry()["caches"]["rust"] is True
    assert planned[0].as_matrix_entry()["rust_workspaces"] == (
        "crates/a -> ../../target"
    )


def test_blank_lane_run_keeps_default_provisioning_and_no_cache():
    planned = lanes.plan((_lane("blank", run="   "),), event="pr")
    assert planned[0].as_matrix_entry() == {
        "name": "blank",
        "run": "   ",
        "runner": "ubuntu-latest",
        "required": False,
        "envs": "default",
        "envset": "default",
        "caches": {"rust": False, "sccache": False, "uv": False},
        "rust_workspaces": "",
        "secrets": [],
    }


SCOPED = (
    _lane("lint", required=True, local=True),
    _lane("wasm", run="build crates/wasm", scope="crates/wasm"),
)


def test_scoped_lane_drops_on_a_pr_that_never_enters_its_subtree():
    planned = lanes.plan(SCOPED, event="pr", changed_paths=["README.md", "src/x.py"])
    assert [job.name for job in planned] == ["lint"]


def test_scoped_lane_runs_on_a_pr_touching_its_subtree():
    planned = lanes.plan(SCOPED, event="pr", changed_paths=["crates/wasm/src/lib.rs"])
    assert [job.name for job in planned] == ["lint", "wasm"]


def test_scope_matches_whole_segments_never_a_name_prefix():
    planned = lanes.plan(SCOPED, event="pr", changed_paths=["crates/wasm2/src/a.rs"])
    assert [job.name for job in planned] == ["lint"]


def test_dot_scope_names_the_whole_tree():
    dotted = (_lane("e2e", scope="."),)
    planned = lanes.plan(dotted, event="pr", changed_paths=["docs/README.md"])
    assert [job.name for job in planned] == ["e2e"]


def test_unknown_diff_forces_full_scope_on_a_pr():
    planned = lanes.plan(SCOPED, event="pr", changed_paths=None)
    assert [job.name for job in planned] == ["lint", "wasm"]


@pytest.mark.parametrize("event", ["push", "nightly", "dispatch"])
def test_full_scope_is_forced_on_non_pr_events(event):
    planned = lanes.plan(SCOPED, event=event, changed_paths=["README.md"])
    assert [job.name for job in planned] == ["lint", "wasm"]


def test_an_all_scoped_repo_can_plan_an_empty_matrix():
    only_scoped = (_lane("wasm", scope="crates/wasm"),)
    assert lanes.plan(only_scoped, event="pr", changed_paths=["README.md"]) == ()


def test_commit_push_checks_are_exactly_the_required_and_local_lanes():
    declared = (
        _lane("lint", required=True, local=True),
        _lane("test", required=True, local=True),
        config.CHANGELOG_SYNC_LANE,
        _lane("bench", required=False, local=True),
    )
    assert [lane.name for lane in lanes.commit_push_checks(declared)] == [
        "lint",
        "test",
    ]


def test_shipits_own_commit_push_checks_are_lint_plus_the_fast_test_set():
    own = config.load(Path(__file__).resolve().parents[1] / config.CONFIG_NAME)
    declared = config.load_lanes(own)
    assert [lane.name for lane in lanes.commit_push_checks(declared)] == [
        "lint",
        "test",
    ]
    by_name = {lane.name: lane for lane in declared}
    assert by_name["lint"].run == "lint"
    assert by_name["test"].run == "test"


def test_shipits_own_plan_covers_the_pre_cutover_ci_on_every_event():
    own = config.load(Path(__file__).resolve().parents[1] / config.CONFIG_NAME)
    declared = config.load_lanes(own)
    for event in lanes.EVENTS:
        assert [job.name for job in lanes.plan(declared, event=event)] == [
            "lint",
            "test",
            "changelog",
        ]


def test_shipits_own_changelog_lane_is_pr_gated_but_not_local():
    own = config.load(Path(__file__).resolve().parents[1] / config.CONFIG_NAME)
    declared = config.load_lanes(own)
    changelog = {lane.name: lane for lane in declared}["changelog"]
    assert changelog.run == "changelog check-fragment"
    assert changelog.required and not changelog.local
    assert "changelog" in [job.name for job in lanes.plan(declared, event="pr")]
    assert "changelog" not in [lane.name for lane in lanes.commit_push_checks(declared)]


RUST_CI_SHAPE = """
[toolchains]
"." = "rust"
"crates/wasm" = "rust"

[lanes.lint]
run = "lint"
required = true
local = true

[lanes.test]
run = "test"
required = true
local = true

[lanes.e2e]
run = "e2e"
required = true
runner = "macos-14"

[lanes.wasm]
run = "build crates/wasm"
required = true
scope = "crates/wasm"
"""

GO_CI_SHAPE = """
[toolchains]
"." = { toolchain = "go", test = ["make", "check"] }

[lanes.lint]
run = "lint"
required = true
local = true

[lanes.test]
run = "test"
required = true
local = true
"""


def _load_shape(text):
    cfg = tomllib.loads(text)
    return config.load_lanes(cfg), config.load_toolchains(cfg)


def test_rust_ci_shape_dissolves_into_declarations_with_no_caller_inputs():
    declared, toolchains = _load_shape(RUST_CI_SHAPE)
    assert [e.path for e in toolchains] == [".", "crates/wasm"]

    thin = lanes.plan(declared, event="pr", changed_paths=["src/main.rs"])
    assert [(job.name, job.runner) for job in thin] == [
        ("lint", "ubuntu-latest"),
        ("test", "ubuntu-latest"),
        ("e2e", "macos-14"),
    ]

    full = lanes.plan(declared, event="nightly")
    assert [job.name for job in full] == ["lint", "test", "e2e", "wasm"]
    assert full[-1].run == "build crates/wasm"

    assert [lane.name for lane in lanes.commit_push_checks(declared)] == [
        "lint",
        "test",
    ]


def test_go_ci_shape_dissolves_the_hook_inputs_into_a_map_override():
    declared, toolchains = _load_shape(GO_CI_SHAPE)
    assert toolchains[0].commands["test"] == ("make", "check")
    planned = lanes.plan(declared, event="pr", changed_paths=["main.go"])
    assert [job.name for job in planned] == ["lint", "test"]
    assert [lane.name for lane in lanes.commit_push_checks(declared)] == [
        "lint",
        "test",
    ]
