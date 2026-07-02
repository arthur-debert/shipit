"""``shipit.pixienv`` — the pixi Tool adapter: env model, reads, execution, scrub.

The pixi domain home (ADR-0022 / ADR-0028): shipit **rides pixi's
environment/path/activation model instead of reinventing it**, and every piece of
pixi knowledge — argv, env-scrub rules, cache location, timeout defaults — lives
HERE, in exactly one adapter. pixi is a Rust CLI consumed via subprocess + JSON;
there is no pixi Python library and this module deliberately does **not** reach under
it to ``py-rattler`` (the conda layer beneath pixi). Four layers:

- :mod:`~shipit.pixienv.model` — the pure core: pixi's JSON shapes as thin, frozen
  value objects (:class:`EnvIdentity`, :class:`Activation`,
  :class:`InstalledPackage`, :class:`Info`) plus the pure env transforms
  (:func:`activation_delta` / :func:`activated_env`) — shipit never re-derives
  activation.
- :mod:`~shipit.pixienv.read` — the read-side I/O boundary: ``conda-meta`` files on
  disk, and the native-JSON read verbs ``shell-hook`` / ``list`` / ``info``.
- :mod:`~shipit.pixienv.run` — the execution side: ``pixi install`` (with pixi's
  own long-runner timeout default) and run-wrapping (``pixi run --manifest-path …
  -- <argv>``), plus pixi's on-disk knowledge (manifest name, provisioned-env
  sentinel, cache dir).
- :mod:`~shipit.pixienv.scrub` — the env-scrub rules: which inherited vars bind a
  child to the PARENT pixi/Conda project (the one predicate every scrub path
  shares).

The sccache build env that used to be hand-built in Python now lives in pixi
``[activation.env]``, so pixi sets it on every activation and it reaches the agent's
own in-Tree ``cargo`` — one more "stop computing what pixi already computes"
(docs/dev/pixi).
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
    "scrub_env",
    "shell_hook",
]
