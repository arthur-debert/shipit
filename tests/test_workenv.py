from __future__ import annotations

from dataclasses import FrozenInstanceError, fields

import pytest

from shipit import pixienv
from shipit.harness.roleprofile import (
    AmbientWorkingDir,
    ExistingPrWriteTree,
    NewWriteTree,
    PerRunReadOnlyTree,
    SessionTree,
)
from shipit.identity import Revision, Sha, WorkingDir, repo_from_slug
from shipit.workenv import (
    ExecutionRouting,
    TreeProvenance,
    WorkEnv,
    ci_lane_resolution_record,
    resolution_record,
    resolve_ambient_env,
    resolve_existing_pr_write_env,
    resolve_readonly_review_env,
    resolve_session_env,
    resolve_write_run_env,
)

_REPO = repo_from_slug("acme/widget")

_ENV_IDENTITY = pixienv.env_identity_from_dict(
    {
        "manifest_path": "/trees/acme/widget/E/WS01-abc123/pixi.toml",
        "environment_name": "default",
        "pixi_version": "0.63.2",
        "environment_lock_file_hash": "99f00798db0ea80c",
        "resolved_platform": {
            "subdir": "osx-arm64",
            "virtual_packages": ["__osx=13.0"],
        },
    }
)

_ACTIVATION = pixienv.Activation(
    environment_variables={
        "PATH": "/captured/pixi-env/bin:/usr/bin",
        "CONDA_PREFIX": "/captured/pixi-env",
    },
    activation_scripts=(),
)

_HEAD = Sha("a" * 40)


def resolve(**overrides) -> WorkEnv:
    facts = dict(
        repo=_REPO,
        tree_path="/trees/acme/widget/E/WS01-abc123",
        branch="E/WS01",
        base="origin/E/umbrella",
        pixi_provisioned=True,
        env_identity=_ENV_IDENTITY,
    )
    facts.update(overrides)
    return resolve_write_run_env(**facts)


def test_provisioned_write_run_routes_through_pixi_run():
    env = resolve()

    assert env.routing is ExecutionRouting.PIXI_RUN
    assert isinstance(env.checkout, NewWriteTree)
    assert env.env_identity is _ENV_IDENTITY
    assert env.env_identity.environment_name == "default"
    assert env.activation is None


def test_working_dir_and_tree_provenance_compose_without_duplication():
    env = resolve()

    assert env.working_dir == WorkingDir(
        path="/trees/acme/widget/E/WS01-abc123",
        repo=_REPO,
        revision=Revision(branch="E/WS01", commit=None),
    )
    assert env.tree == TreeProvenance(branch="E/WS01", base="origin/E/umbrella")
    assert {f.name for f in fields(TreeProvenance)} == {"branch", "base"}


@pytest.mark.parametrize(
    "session_id",
    [
        "sess-20260712-120000-41",
        "codex-20260712-120000-42",
    ],
)
def test_coordinator_session_hosts_resolve_the_same_work_env_shape(session_id):
    env = resolve_session_env(
        repo=_REPO,
        tree_path=f"/trees/acme/widget/ephemeral/{session_id}",
        branch=f"ephemeral/{session_id}",
        base="origin/main",
        activation=_ACTIVATION,
        env_identity=_ENV_IDENTITY,
    )

    assert isinstance(env.checkout, SessionTree)
    assert env.working_dir == WorkingDir(
        path=f"/trees/acme/widget/ephemeral/{session_id}",
        repo=_REPO,
        revision=Revision(branch=f"ephemeral/{session_id}", commit=None),
    )
    assert env.tree == TreeProvenance(
        branch=f"ephemeral/{session_id}", base="origin/main"
    )
    assert env.routing is ExecutionRouting.ACTIVATION_SNAPSHOT
    assert env.activation is _ACTIVATION
    assert env.env_identity is _ENV_IDENTITY


def test_non_pixi_session_tree_uses_ambient_routing():
    env = resolve_session_env(
        repo=_REPO,
        tree_path="/trees/acme/widget/ephemeral/sess-no-pixi",
        branch="ephemeral/sess-no-pixi",
        base="origin/main",
        activation=None,
    )

    assert isinstance(env.checkout, SessionTree)
    assert env.routing is ExecutionRouting.AMBIENT
    assert env.activation is None
    assert env.env_identity is None


def test_session_env_identity_without_activation_is_refused():
    with pytest.raises(ValueError, match="incoherent session"):
        resolve_session_env(
            repo=_REPO,
            tree_path="/trees/acme/widget/ephemeral/sess-bad",
            branch="ephemeral/sess-bad",
            base="origin/main",
            activation=None,
            env_identity=_ENV_IDENTITY,
        )


def test_reviewer_readonly_tree_records_provenance_without_pixi_activation():
    env = resolve_readonly_review_env(
        repo=_REPO,
        tree_path="/trees/acme/widget/review/rpe01-ws06-12345678",
        branch="RPE01/WS06",
        commit=_HEAD,
    )

    assert isinstance(env.checkout, PerRunReadOnlyTree)
    assert env.checkout.tree_backed is True
    assert env.checkout.writable is False
    assert env.working_dir == WorkingDir(
        path="/trees/acme/widget/review/rpe01-ws06-12345678",
        repo=_REPO,
        revision=Revision(branch="RPE01/WS06", commit=_HEAD),
    )
    assert env.tree == TreeProvenance(branch="RPE01/WS06", base=None)
    assert env.routing is ExecutionRouting.AMBIENT
    assert env.activation is None
    assert env.env_identity is None


def test_explorer_ambient_env_has_no_tree_or_detached_write_path():
    env = resolve_ambient_env(
        repo=_REPO,
        path="/src/acme/widget",
        branch="main",
        commit=_HEAD,
    )

    assert isinstance(env.checkout, AmbientWorkingDir)
    assert env.checkout.tree_backed is False
    assert env.checkout.writable is False
    assert env.working_dir == WorkingDir(
        path="/src/acme/widget",
        repo=_REPO,
        revision=Revision(branch="main", commit=_HEAD),
    )
    assert env.tree is None
    assert env.routing is ExecutionRouting.AMBIENT
    assert env.activation is None
    assert env.env_identity is None


def test_non_pixi_write_run_uses_ambient_routing():
    env = resolve(pixi_provisioned=False, env_identity=None)

    assert env.routing is ExecutionRouting.AMBIENT
    assert env.activation is None
    assert env.env_identity is None
    assert isinstance(env.checkout, NewWriteTree)
    assert env.tree == TreeProvenance(branch="E/WS01", base="origin/E/umbrella")


def test_existing_pr_write_run_uses_shepherd_checkout_strategy():
    env = resolve_existing_pr_write_env(
        repo=_REPO,
        tree_path="/trees/acme/widget/branches/e-ws01-pr321",
        branch="E/WS01",
        base="origin/E/WS01",
        pixi_provisioned=False,
    )

    assert isinstance(env.checkout, ExistingPrWriteTree)
    assert env.routing is ExecutionRouting.AMBIENT
    assert env.tree == TreeProvenance(branch="E/WS01", base="origin/E/WS01")


def test_env_identity_without_a_provisioned_env_is_refused():
    with pytest.raises(ValueError, match="incoherent"):
        resolve(pixi_provisioned=False, env_identity=_ENV_IDENTITY)


def test_resolution_is_deterministic_over_supplied_facts():
    assert resolve() == resolve()
    assert resolve(pixi_provisioned=False, env_identity=None) == resolve(
        pixi_provisioned=False, env_identity=None
    )


def test_work_env_is_a_frozen_value():
    env = resolve()
    with pytest.raises(FrozenInstanceError):
        env.routing = ExecutionRouting.AMBIENT


def test_routing_vocabulary_is_closed_and_names_existing_mechanisms():
    assert {m.value for m in ExecutionRouting} == {
        "pixi-run",
        "activation-snapshot",
        "ambient",
    }


def test_resolution_record_is_flat_redacted_and_uses_stable_field_names():
    env = resolve_session_env(
        repo=_REPO,
        tree_path="/trees/acme/widget/ephemeral/sess-1",
        branch="ephemeral/sess-1",
        base="origin/main",
        activation=_ACTIVATION,
        env_identity=_ENV_IDENTITY,
    )

    record = resolution_record(env, boundary="session.codex-launch", role="coordinator")

    assert record == {
        "work_env_boundary": "session.codex-launch",
        "working_dir": "/trees/acme/widget/ephemeral/sess-1",
        "working_dir_repo": "acme/widget",
        "working_dir_branch": "ephemeral/sess-1",
        "checkout_strategy": "session-tree",
        "routing": "activation-snapshot",
        "role": "coordinator",
        "tree_branch": "ephemeral/sess-1",
        "tree_base": "origin/main",
        "pixi_environment_name": "default",
        "pixi_environment_lock_hash": "99f00798db0ea80c",
        "pixi_activation": "present",
    }
    assert "working_dir_commit" not in record
    assert "environment_variables" not in record
    assert "PATH" not in record
    assert "CONDA_PREFIX" not in record
    assert "pixi_run_id" not in record


def test_ci_lane_resolution_record_uses_the_shared_projection_vocabulary():
    record = ci_lane_resolution_record(
        working_dir="/checkout",
        repo="acme/widget",
        lane="lint",
        pixi_environment_name="lint",
        ci_event="pr",
        runner="ubuntu-latest",
        required=True,
    )

    assert record == {
        "work_env_boundary": "ci.lane-job",
        "working_dir": "/checkout",
        "working_dir_repo": "acme/widget",
        "checkout_strategy": "direct-checkout",
        "routing": "pixi-run",
        "lane": "lint",
        "pixi_environment_name": "lint",
        "ci_event": "pr",
        "runner": "ubuntu-latest",
        "required": True,
    }
