"""``pixienv/scrub`` — the pure env-scrub rules: which inherited vars bind to the PARENT."""

from __future__ import annotations

from collections.abc import Mapping

#: The user-level ``PIXI_*`` cache vars, KEPT so Trees share one package cache.
PIXI_CACHE_VARS = frozenset({"PIXI_CACHE_DIR", "RATTLER_CACHE_DIR"})

#: The Conda ACTIVATION vars that bind a process to the parent env. Installation-level
#: ``CONDA_*`` (``CONDA_EXE``, ``CONDA_ROOT``, ``_CE_*``) is kept: scrubbing it could
#: break ``pixi run`` itself in a Conda-managed shell.
CONDA_ACTIVATION_VARS = frozenset(
    {"CONDA_PREFIX", "CONDA_DEFAULT_ENV", "CONDA_SHLVL", "CONDA_PROMPT_MODIFIER"}
)

#: The three per-Tree keys pixi ``[activation.env]`` re-sets, so an inherited value would
#: shadow it. ``RUSTC_WRAPPER`` and the ``SCCACHE_*`` cache/credential vars are kept:
#: they are install-/backend-level, and dropping them would disable sccache.
BUILD_ENV_VARS = frozenset(
    {"CARGO_TARGET_DIR", "SCCACHE_BASEDIRS", "CARGO_INCREMENTAL"}
)


def is_leaked_env_var(key: str) -> bool:
    """Whether ``key`` is a parent-project env pointer to scrub from a Tree child.

    Every scrub path in shipit relies solely on this predicate.
    """
    if key.startswith("PIXI_"):
        return key not in PIXI_CACHE_VARS
    if key in CONDA_ACTIVATION_VARS or key.startswith("CONDA_PREFIX_"):
        return True
    if key.startswith("CONDA_ENV_SHLVL_"):
        # pixi's activation-stack restore keys. Keeping them while `CONDA_SHLVL`
        # is scrubbed leaves a half-scrubbed stack, against which pixi's own
        # nested activation mis-diffs and OMITS `[activation.env]` vars it should
        # set — so the whole family goes.
        return True
    if key in BUILD_ENV_VARS:
        return True
    return False


def scrub_env(env: Mapping[str, str]) -> dict[str, str]:
    """``env`` minus every leaked parent-project pointer, as a FRESH dict."""
    return {key: value for key, value in env.items() if not is_leaked_env_var(key)}
