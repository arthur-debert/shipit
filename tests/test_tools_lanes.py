"""The pure lane planner (TOL01-WS05) — trigger ladder, thin/full scope,
commit/push-check derivation, and the legacy-consumer coverage fixtures.

Fixture-driven over typed :class:`shipit.config.Lane` values (and, for the
legacy shapes, whole ``.shipit.toml`` texts through the real config boundary),
no I/O: event normalization from both vocabularies, the trigger ladder
(pr < push < nightly < dispatch), scope thinning only on PR events with a
known diff (full forced everywhere else), runner defaulting, declaration
order, and the required∩local derivation — asserted on shipit's OWN
declarations to equal exactly ``lint`` + the fast ``test`` set (never by
convention).

The legacy fixtures encode the two retiring reusable-workflow shapes —
``rust-ci.yml`` (umbrella check + release-binary bats e2e + wasm matrix) and
``go-ci.yml`` (umbrella check + pre-check hook + golangci pin) — as lane
declarations + toolchain-map entries, proving the TOL01 vocabulary covers
them with NO caller inputs (issue #557's research digest).
"""

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


# ---------------------------------------------------------------------------
# event normalization — both vocabularies, one closed set
# ---------------------------------------------------------------------------


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
    # The wf-checks block passes `github.event_name` VERBATIM; the mapping
    # lives here, fixture-tested, never in YAML (ADR-0040).
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


# ---------------------------------------------------------------------------
# trigger ladder — a lane's trigger is the most frequent event that runs it
# ---------------------------------------------------------------------------

LADDERED = (
    _lane("lint", required=True, local=True),  # trigger defaults to "pr"
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
    # The merge-blocking verdict travels with the job: the block spares an
    # advisory lane's failure from the `check` verdict by reading `required`,
    # so dropping it here would make every advisory lane merge-blocking.
    planned = lanes.plan(LADDERED, event="dispatch")
    required = {job.name: job.required for job in planned}
    assert required == {
        "lint": True,  # required = true
        "deploy-preview": False,  # required defaults to false → advisory
        "gpu-e2e": False,
        "fleet-sweep": False,
    }
    assert planned[1].as_matrix_entry()["required"] is False


def test_pixi_task_env_sets_resolve_feature_tasks_to_their_environments():
    pixi = {
        "tasks": {"changelog": "./bin/shipit changelog"},
        "feature": {
            "lint": {"tasks": {"lint-full": "./bin/shipit lint"}},
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
        "lint-full": ("lint",),
        "test": ("test",),
        "verify": ("dogfood",),
    }


def test_pixi_task_commands_resolve_string_and_cmd_table_tasks():
    pixi = {
        "tasks": {"changelog": "./bin/shipit changelog"},
        "feature": {
            "lint": {"tasks": {"lint-full": {"cmd": "./bin/shipit lint"}}},
            "test": {"tasks": {"test": "./bin/shipit test"}},
        },
    }
    assert lanes.task_commands(pixi) == {
        "changelog": "./bin/shipit changelog",
        "lint-full": "./bin/shipit lint",
        "test": "./bin/shipit test",
    }


def test_matrix_carries_env_set_and_cache_descriptors_from_the_planner():
    declared = (
        _lane("lint", run="lint-full", required=True),
        _lane("test", run="test rust", required=True),
    )
    toolchains = (
        config.ToolchainEntry(path="crates/a", toolchain="rust", commands={}),
        config.ToolchainEntry(path="web", toolchain="npm", commands={}),
    )
    planned = lanes.plan(
        declared,
        event="pr",
        task_envs={"lint-full": ("lint",), "test": ("test",)},
        toolchains=toolchains,
    )
    assert planned[0].as_matrix_entry() == {
        "name": "lint",
        "run": "lint-full",
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
    # The declared-secrets seam (#778): the lane's allowlist rides the matrix
    # verbatim as a JSON ARRAY (not a joined string), so the block's
    # `contains(matrix.secrets, 'lane_token')` gate is an EXACT membership test.
    # A token-less lane emits `[]` — it can never accidentally match the gate.
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


# ---------------------------------------------------------------------------
# scope — thin on an unrelated PR, full forced everywhere else (story 16)
# ---------------------------------------------------------------------------

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
    # Fail-safe: uncertainty runs MORE checks, never fewer.
    planned = lanes.plan(SCOPED, event="pr", changed_paths=None)
    assert [job.name for job in planned] == ["lint", "wasm"]


@pytest.mark.parametrize("event", ["push", "nightly", "dispatch"])
def test_full_scope_is_forced_on_non_pr_events(event):
    # The path-diff only ever THINS a pr plan; coverage events run everything
    # their ladder admits, whatever the diff says.
    planned = lanes.plan(SCOPED, event=event, changed_paths=["README.md"])
    assert [job.name for job in planned] == ["lint", "wasm"]


def test_an_all_scoped_repo_can_plan_an_empty_matrix():
    only_scoped = (_lane("wasm", scope="crates/wasm"),)
    assert lanes.plan(only_scoped, event="pr", changed_paths=["README.md"]) == ()


# ---------------------------------------------------------------------------
# commit/push checks — required∩local, ONE definition (story 13)
# ---------------------------------------------------------------------------


def test_commit_push_checks_are_exactly_the_required_and_local_lanes():
    declared = (
        _lane("lint", required=True, local=True),
        _lane("test", required=True, local=True),
        config.CHANGELOG_SYNC_LANE,  # required but NOT local
        _lane("bench", required=False, local=True),  # local but advisory
    )
    assert [lane.name for lane in lanes.commit_push_checks(declared)] == [
        "lint",
        "test",
    ]


def test_shipits_own_commit_push_checks_are_lint_plus_the_fast_test_set():
    # Asserted against the REAL .shipit.toml, not a fixture: on shipit's own
    # declarations the required∩local set IS `lint` + the fast `test` set —
    # the one definition lefthook and CI both enforce (PRD story 13). A lane
    # edit that breaks this equality must break the build, not drift silently.
    own = config.load(Path(__file__).resolve().parents[1] / config.CONFIG_NAME)
    declared = config.load_lanes(own)
    assert [lane.name for lane in lanes.commit_push_checks(declared)] == [
        "lint",
        "test",
    ]
    # Both run thin pixi callers of the shipit verbs (ADR-0039): the lint lane
    # rides `lint-full` (the provisioned twin of the managed `lint` task — see
    # pixi.toml [feature.lint.tasks]), the test lane the `test` task itself.
    by_name = {lane.name: lane for lane in declared}
    assert by_name["lint"].run == "lint-full"
    assert by_name["test"].run == "test"


def test_shipits_own_plan_covers_the_pre_cutover_ci_on_every_event():
    # The cutover guarantee: on shipit's declarations every event plans
    # lint + test (trigger "pr" sits at the ladder's most frequent rung), so
    # the block reproduces the retired explicit lint/test steps everywhere.
    own = config.load(Path(__file__).resolve().parents[1] / config.CONFIG_NAME)
    declared = config.load_lanes(own)
    for event in lanes.EVENTS:
        assert [job.name for job in lanes.plan(declared, event=event)] == [
            "lint",
            "test",
        ]


# ---------------------------------------------------------------------------
# legacy coverage fixtures — the retiring per-stack workflows as declarations
# ---------------------------------------------------------------------------

# The legacy `rust-ci.yml` consumer shape (issue #557 research digest): the
# `bin/check` umbrella job, a release-binary build feeding a bats e2e job
# (`binary-name` + `bats` caller inputs), and a wasm clippy/wasm-pack matrix
# (`wasm-packages` input) on a mac runner (`runner` input). In TOL01 terms the
# caller inputs DISSOLVE: the umbrella -> the lint/test lanes; the binary+bats
# pair -> an e2e lane (the e2e Tool builds-or-reuses the artifact and injects
# <NAME>_BIN — WS03; this fixture only needs the Lane vocabulary to express
# it); the wasm matrix -> a lane whose `run` names the wasm LEG (`build
# crates/wasm`, ADR-0039: a lane's run may name a leg) scoped to its subtree;
# the `runner` input -> the lane's runner field. No inputs remain.
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

# The legacy `go-ci.yml` consumer shape: the check job (golangci-lint at a
# pinned version — a registry/provisioning fact in TOL01, never a caller
# input), a `pre-check` consumer hook script, and a `check-command` override.
# In TOL01 terms: the pin rides provisioning; pre-check/check-command become a
# per-path producing-command OVERRIDE on the map entry (PRD story 4) — data in
# .shipit.toml, not a second workflow input surface.
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

    # An unrelated PR runs thin: the wasm matrix job drops, the umbrella
    # lanes and the e2e lane (unscoped, like the legacy always-on bats job)
    # still run — on the runner the LANE declares, not a caller input.
    thin = lanes.plan(declared, event="pr", changed_paths=["src/main.rs"])
    assert [(job.name, job.runner) for job in thin] == [
        ("lint", "ubuntu-latest"),
        ("test", "ubuntu-latest"),
        ("e2e", "macos-14"),
    ]

    # Nightly forces full: the wasm leg-lane joins, `run` naming the leg the
    # build Tool will dispatch (`pixi run build crates/wasm`).
    full = lanes.plan(declared, event="nightly")
    assert [job.name for job in full] == ["lint", "test", "e2e", "wasm"]
    assert full[-1].run == "build crates/wasm"

    # And the commit/push checks stay the umbrella pair — the e2e/wasm lanes
    # are required at merge but never block a commit (they are not local).
    assert [lane.name for lane in lanes.commit_push_checks(declared)] == [
        "lint",
        "test",
    ]


def test_go_ci_shape_dissolves_the_hook_inputs_into_a_map_override():
    declared, toolchains = _load_shape(GO_CI_SHAPE)
    # The pre-check/check-command inputs became the entry's producing-command
    # override — consumer DATA the test Tool dispatches, no workflow input.
    assert toolchains[0].commands["test"] == ("make", "check")
    planned = lanes.plan(declared, event="pr", changed_paths=["main.go"])
    assert [job.name for job in planned] == ["lint", "test"]
    assert [lane.name for lane in lanes.commit_push_checks(declared)] == [
        "lint",
        "test",
    ]
