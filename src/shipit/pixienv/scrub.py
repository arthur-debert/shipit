"""``pixienv/scrub`` — the env-scrub rules: which inherited vars bind to the PARENT.

Pure predicates + one pure transform, absorbed out of the Tree code (PROC02-WS02,
ADR-0028): "which env vars leak a parent pixi/Conda project into a child rooted in a
DIFFERENT clone" is pixi domain knowledge, so it lives in the pixi adapter — beside
the activation model it protects — not in the Tree orchestrator that happens to
consume it.

The single source of truth is :func:`is_leaked_env_var`; every scrub site
(:func:`shipit.tree.create.provision_env`, :func:`shipit.spawn.launch.scrub_tree_env`,
the dogfood probe) relies SOLELY on it, so the carve-outs can never drift between
paths. Everything here is pure — a key predicate and a mapping→dict transform — so
the truth tables are unit-tested straight off fixtures.
"""

from __future__ import annotations

from collections.abc import Mapping

#: ``PIXI_*`` variables the parent ``pixi run`` injects that bind to the PARENT
#: project/manifest/environment. They MUST NOT leak into a child shipit/pixi
#: operating inside a DIFFERENT clone: a leaked ``PIXI_PROJECT_MANIFEST`` makes the
#: clone's ``pixi run lint`` resolve the parent manifest, where ``lint`` is
#: ambiguous across the ``default``/``lint``/``review`` environments, so the
#: install commit's pre-commit hook dies (#167). This is the same env-leak class as
#: ADR-0019's ``ANTHROPIC_API_KEY`` finding — an inherited var breaking a child
#: rooted elsewhere — and the fix is the same: scrub it. Cache-location vars are
#: user-level (not project-bound), so they are kept (see :func:`is_leaked_env_var`)
#: to preserve cross-Tree package-cache sharing.
PIXI_CACHE_VARS = frozenset({"PIXI_CACHE_DIR", "RATTLER_CACHE_DIR"})

#: The Conda **activation** vars that bind a process to the PARENT env — exactly the
#: ones a ``conda activate`` (and pixi's own activation, which is conda-shaped) set on
#: entry. They MUST be scrubbed for the same reason as the ``PIXI_*`` pointers: a leaked
#: ``CONDA_PREFIX`` / ``CONDA_DEFAULT_ENV`` keeps a child bound to the PARENT env's
#: activation, so ``python`` / tooling resolve there instead of the child's own Tree. The
#: stacked ``CONDA_PREFIX_<n>`` an activation *stack* leaves behind is caught by prefix in
#: :func:`is_leaked_env_var`. **Installation-level** vars (``CONDA_EXE``,
#: ``CONDA_PYTHON_EXE``, ``CONDA_ROOT``, ``_CE_*``) are user-/install-level, NOT project
#: pointers, so they are KEPT — dropping them wholesale could change subprocess behavior
#: (including ``pixi run`` itself in a Conda-managed shell).
CONDA_ACTIVATION_VARS = frozenset(
    {"CONDA_PREFIX", "CONDA_DEFAULT_ENV", "CONDA_SHLVL", "CONDA_PROMPT_MODIFIER"}
)

#: The ADR-0015 build-env vars that pixi ``[activation.env]`` now OWNS and re-sets to a
#: PER-TREE value (via ``$PIXI_PROJECT_ROOT``) on every activation (COR01 / ADR-0022).
#: These are exactly the three keys declared in ``pixi.toml``'s ``[activation.env]``. Because
#: the build env now comes from pixi ``[activation.env]`` (no longer injected in Python), an
#: inherited PARENT value would
#: SHADOW the Tree's activation value — a leaked ``CARGO_TARGET_DIR`` / ``SCCACHE_BASEDIRS``
#: points the child's ``cargo`` at the PARENT Tree's ``target/`` and keys sccache on the
#: PARENT path, so build artifacts land in — and cache-hit against — the WRONG Tree. They
#: are the same leak class as the ``PIXI_*`` / Conda pointers: strip the inherited value so
#: pixi's per-Tree ``[activation.env]`` value is authoritative. NOT scrubbed (kept, same as
#: the cache/installation carve-outs): ``RUSTC_WRAPPER`` (the install-level sccache binary
#: pointer — dropping it would DISABLE sccache in the child, and it is not per-Tree) and the
#: ``SCCACHE_*`` cache/credential vars (``SCCACHE_DIR`` / ``SCCACHE_GCS_KEY`` — the child
#: NEEDS them to reach the shared cache backend; they are user-/backend-level, not per-Tree
#: paths).
BUILD_ENV_VARS = frozenset(
    {"CARGO_TARGET_DIR", "SCCACHE_BASEDIRS", "CARGO_INCREMENTAL"}
)


def is_leaked_env_var(key: str) -> bool:
    """Whether ``key`` is a parent-project env pointer to scrub from a Tree child.

    The single source of truth for "which inherited vars bind to the PARENT project and
    must not leak into a child rooted in a different clone". Three leak classes:

    - ``PIXI_*`` project pointers (all ``PIXI_*`` except the user-level cache vars in
      :data:`PIXI_CACHE_VARS`).
    - Conda **activation** vars (:data:`CONDA_ACTIVATION_VARS` and the stacked
      ``CONDA_PREFIX_<n>``) — SCOPED to activation-binding vars only; installation-level
      ``CONDA_*`` (``CONDA_EXE`` / ``CONDA_PYTHON_EXE`` / ``CONDA_ROOT`` / ``_CE_*``) is
      KEPT, since scrubbing all ``CONDA_*`` could break ``pixi run`` in a Conda shell.
    - ADR-0015 **build-env** vars (:data:`BUILD_ENV_VARS`) that pixi ``[activation.env]``
      re-sets PER-TREE — SCOPED to the three per-Tree-path keys; install-/backend-level
      ``RUSTC_WRAPPER`` and ``SCCACHE_*`` cache/credential vars are KEPT (dropping them
      would disable sccache or cut the child off from the shared cache).

    Every scrub path — the provisioning env (:func:`shipit.tree.create.provision_env`),
    the launch env (:func:`shipit.spawn.launch.scrub_tree_env`), the dogfood probe —
    scrubs SOLELY on this predicate, so no carve-out can drift between them.
    """
    if key.startswith("PIXI_"):
        return key not in PIXI_CACHE_VARS
    if key in CONDA_ACTIVATION_VARS or key.startswith("CONDA_PREFIX_"):
        return True
    if key in BUILD_ENV_VARS:
        return True
    return False


def scrub_env(env: Mapping[str, str]) -> dict[str, str]:
    """``env`` minus every leaked parent-project pointer — a pure snapshot transform.

    Filters ``env`` on :func:`is_leaked_env_var` and returns a FRESH dict (never the
    caller's mapping), so the result can be handed to the Exec runner as the COMPLETE
    child environment (``replace_env=True``) — a merge over ``os.environ`` could re-add
    the very vars this drops.
    """
    return {key: value for key, value in env.items() if not is_leaked_env_var(key)}
