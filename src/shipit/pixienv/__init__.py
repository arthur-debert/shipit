"""``shipit.pixienv`` — the pixi Tool adapter: env model, reads, execution, scrub.

Every piece of pixi knowledge lives here, in exactly one adapter.
See docs/adr/0022-layer-boundary-model-vs-borrow-pixi.md.
"""

from __future__ import annotations

from .model import (
    Activation,
    EnvIdentity,
    EnvironmentInfo,
    Info,
    InstalledPackage,
    Platform,
    ProjectInfo,
    activated_env,
    activation_delta,
    activation_from_dict,
    env_identity_from_dict,
    info_from_dict,
    installed_package_from_dict,
    parse_activation,
    parse_env_identity,
    parse_info,
    parse_installed_packages,
    path_entries,
)
from .read import (
    READ_TIMEOUT,
    info,
    list_packages,
    read_env_identity,
    read_fingerprint,
    shell_hook,
)
from .run import (
    DEFAULT_ENV_DIR,
    INSTALL_TIMEOUT,
    MANIFEST_NAME,
    cache_dir,
    has_default_env,
    install,
    run_argv,
    run_in_env,
    run_task,
)
from .scrub import (
    BUILD_ENV_VARS,
    CONDA_ACTIVATION_VARS,
    PIXI_CACHE_VARS,
    is_leaked_env_var,
    scrub_env,
)

__all__ = [
    "BUILD_ENV_VARS",
    "CONDA_ACTIVATION_VARS",
    "DEFAULT_ENV_DIR",
    "INSTALL_TIMEOUT",
    "MANIFEST_NAME",
    "PIXI_CACHE_VARS",
    "READ_TIMEOUT",
    "Activation",
    "EnvIdentity",
    "EnvironmentInfo",
    "Info",
    "InstalledPackage",
    "Platform",
    "ProjectInfo",
    "activated_env",
    "activation_delta",
    "activation_from_dict",
    "cache_dir",
    "env_identity_from_dict",
    "has_default_env",
    "info",
    "info_from_dict",
    "install",
    "installed_package_from_dict",
    "is_leaked_env_var",
    "list_packages",
    "parse_activation",
    "parse_env_identity",
    "parse_info",
    "parse_installed_packages",
    "path_entries",
    "read_env_identity",
    "read_fingerprint",
    "run_argv",
    "run_in_env",
    "run_task",
    "scrub_env",
    "shell_hook",
]
