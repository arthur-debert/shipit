"""Tests for the Work Env value and boundary-specific resolvers (RPE01).

The spec's Work Env testing contract (docs/spec/role-profiles-work-env.md
§Testing — Work Env value and resolution). Everything is asserted
typed-in/typed-out — each resolver is pure over supplied facts, so no
filesystem, process, pixi, or network is touched anywhere in this module (the
nonexistent paths are the point). The launch-side CONSUMER
(:func:`shipit.spawn.launch.route_argv`) is covered in ``test_spawn_launch.py``
and the end-to-end write tail in ``test_spawn_subagent.py``.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields

import pytest

from shipit import pixienv
from shipit.harness.roleprofile import (
    AmbientWorkingDir,
    ExistingPrWriteTree,
    NewWriteTree,
    SessionTree,
    SharedReadOnlyTree,
)
from shipit.identity import Revision, Sha, WorkingDir, repo_from_slug
from shipit.workenv import (
    ExecutionRouting,
    TreeProvenance,
    WorkEnv,
    resolution_record,
    resolve_ambient_env,
    resolve_existing_pr_write_env,
    resolve_readonly_review_env,
    resolve_session_env,
    resolve_write_run_env,
)

_REPO = repo_from_slug("acme/widget")

# A faithful `conda-meta/pixi` shape (docs/dev/pixi §2) — parsed through the
# pixi adapter's OWN builder, because a Work Env borrows pixi's value objects
# (ADR-0022), never a hand-rolled stand-in.
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

# Deliberately checkout-neutral: each session case supplies its own Tree path,
# while this value stands only for a previously captured shell-hook snapshot.
_ACTIVATION = pixienv.Activation(
    environment_variables={
        "PATH": "/captured/pixi-env/bin:/usr/bin",
        "CONDA_PREFIX": "/captured/pixi-env",
    },
    activation_scripts=(),
)

_HEAD = Sha("a" * 40)


def resolve(**overrides) -> WorkEnv:
    """The default provisioned write-Run resolution; override any fact per test."""
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


# --- the provisioned implementer write Run ------------------------------------


def test_provisioned_write_run_routes_through_pixi_run():
    # Acceptance: a provisioned write context selects the EXISTING pixi-run
    # wrapping — the Work Env carries the decision; pixi keeps activation.
    env = resolve()

    assert env.routing is ExecutionRouting.PIXI_RUN
    assert isinstance(env.checkout, NewWriteTree)
    # The pixi identity is BORROWED — the adapter's own EnvIdentity value,
    # threaded through untouched (ADR-0022: never re-derived here).
    assert env.env_identity is _ENV_IDENTITY
    assert env.env_identity.environment_name == "default"
    # A write Run never carries an activation snapshot: `pixi run` computes
    # activation inside the child, so there is nothing to borrow — absence is
    # honest, not fabricated.
    assert env.activation is None


def test_working_dir_and_tree_provenance_compose_without_duplication():
    # Spec: "Tree provenance and WorkingDir identity compose rather than
    # duplicate one another." The WorkingDir is the ONE checkout identity —
    # path, repo, best-effort revision; provenance adds only what the Tree
    # knows beyond it (branch + base), with NO path field to drift.
    env = resolve()

    assert env.working_dir == WorkingDir(
        path="/trees/acme/widget/E/WS01-abc123",
        repo=_REPO,
        # commit=None is the honest best-effort Revision: the boundary supplied
        # no HEAD read and resolution must not add one.
        revision=Revision(branch="E/WS01", commit=None),
    )
    assert env.tree == TreeProvenance(branch="E/WS01", base="origin/E/umbrella")
    assert {f.name for f in fields(TreeProvenance)} == {"branch", "base"}


# --- the coordinator session Tree --------------------------------------------


@pytest.mark.parametrize(
    "session_id",
    [
        "sess-20260712-120000-41",  # Claude: WorktreeCreate pre-launch seam
        "codex-20260712-120000-42",  # Codex: explicit provision-then-exec seam
    ],
)
def test_coordinator_session_hosts_resolve_the_same_work_env_shape(session_id):
    # Acceptance: Claude and Codex keep their narrow host adapters, but once
    # they supply session Tree coordinates + pixi's shell-hook snapshot, Work
    # Env resolution is the same SessionTree semantics.
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
    # The activation is the exact supplied shell-hook snapshot, not a PATH
    # recomputation or a rendered export script.
    assert env.activation is _ACTIVATION
    assert env.env_identity is _ENV_IDENTITY


def test_non_pixi_session_tree_is_a_valid_ambient_work_env():
    # Acceptance: a coordinator session in a non-pixi repo carries no
    # Activation/EnvIdentity and still resolves to a session Tree rather than
    # erroring or fabricating pixi state.
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


# --- the reviewer shared read-only Tree --------------------------------------


def test_reviewer_readonly_tree_records_provenance_without_pixi_activation():
    # Acceptance: reviewer Work Env is a shared read-only Tree with checkout
    # provenance but no provisioned pixi env; ambient routing preserves the
    # existing reviewer launch posture over the chmod'd filesystem guard.
    env = resolve_readonly_review_env(
        repo=_REPO,
        tree_path="/trees/acme/widget/review/rpe01-ws06-12345678",
        branch="RPE01/WS06",
        commit=_HEAD,
    )

    assert isinstance(env.checkout, SharedReadOnlyTree)
    assert env.checkout.tree_backed is True
    assert env.checkout.writable is False
    assert env.working_dir == WorkingDir(
        path="/trees/acme/widget/review/rpe01-ws06-12345678",
        repo=_REPO,
        revision=Revision(branch="RPE01/WS06", commit=_HEAD),
    )
    # A review Tree checks out an existing PR-head branch; it does not cut a new
    # branch from a base ref.
    assert env.tree == TreeProvenance(branch="RPE01/WS06", base=None)
    assert env.routing is ExecutionRouting.AMBIENT
    assert env.activation is None
    assert env.env_identity is None


# --- the explorer ambient WorkingDir -----------------------------------------


def test_explorer_ambient_env_has_no_tree_or_detached_write_path():
    # Acceptance: explorer remains an ambient WorkingDir: no Tree provenance,
    # no activation identity, and a checkout strategy that is neither
    # tree-backed nor writable.
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


# --- the non-pixi write Run ---------------------------------------------------


def test_non_pixi_write_run_is_honestly_ambient():
    # Acceptance: a non-pixi write Run represents absent pixi activation
    # honestly — no activation, no env identity, AMBIENT routing (the existing
    # bare-launch behavior), never a fabricated stand-in.
    env = resolve(pixi_provisioned=False, env_identity=None)

    assert env.routing is ExecutionRouting.AMBIENT
    assert env.activation is None
    assert env.env_identity is None
    # Still a real write Tree with full provenance — only the pixi half is absent.
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
    # Incoherent facts fail LOUD: the identity file lives INSIDE the
    # provisioned env, so identity-without-env cannot both be true.
    with pytest.raises(ValueError, match="incoherent"):
        resolve(pixi_provisioned=False, env_identity=_ENV_IDENTITY)


# --- resolution discipline ----------------------------------------------------


def test_resolution_is_deterministic_over_supplied_facts():
    # Same facts in, same value out — pure resolution (spec: no implicit
    # process, filesystem mutation, provisioning, or network work; the
    # nonexistent tree path proves no probe happens).
    assert resolve() == resolve()
    assert resolve(pixi_provisioned=False, env_identity=None) == resolve(
        pixi_provisioned=False, env_identity=None
    )


def test_work_env_is_a_frozen_value():
    # A RESOLVED description, not mutable state: no consumer can flip the
    # routing (or any other decision) after resolution.
    env = resolve()
    with pytest.raises(FrozenInstanceError):
        env.routing = ExecutionRouting.AMBIENT


def test_routing_vocabulary_is_closed_and_names_existing_mechanisms():
    # The three modes the spec names — pixi-run wrapping, an activation
    # snapshot (the coordinator borrow, resolved by WS06's boundaries), and
    # ambient tools. Work Env selects among EXISTING mechanisms; a new member
    # here would mean a new executor, which is exactly what it must not be.
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
