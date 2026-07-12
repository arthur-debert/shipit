"""Tests for the Work Env value and its write-Run resolver (RPE01-WS05).

The spec's Work Env testing contract (docs/spec/role-profiles-work-env.md
§Testing — Work Env value and resolution), for the boundary this workstream
lands: the NEW write Tree. Everything is asserted typed-in/typed-out — the
resolver is pure over supplied facts, so no filesystem, process, pixi, or
network is touched anywhere in this module (the nonexistent paths are the
point). The launch-side CONSUMER (:func:`shipit.spawn.launch.route_argv`) is
covered in ``test_spawn_launch.py`` and the end-to-end write tail in
``test_spawn_subagent.py``.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError, fields

import pytest

from shipit import pixienv
from shipit.harness.roleprofile import ExistingPrWriteTree, NewWriteTree
from shipit.identity import Revision, WorkingDir, repo_from_slug
from shipit.workenv import (
    ExecutionRouting,
    TreeProvenance,
    WorkEnv,
    resolve_existing_pr_write_env,
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
