"""``shipit.pixienv`` — pixi's env/activation model, borrowed through pixi's JSON.

The Core Model env layer (ADR-0022): shipit **rides pixi's environment/path/activation
model instead of reinventing it**. pixi is a Rust CLI consumed via subprocess + JSON;
there is no pixi Python library and this module deliberately does **not** reach under it
to ``py-rattler`` (the conda layer beneath pixi). Instead it mirrors two pixi shapes as
thin, frozen value objects and expresses env handling as pure transforms over immutable
snapshots (ADR-0021):

- :class:`EnvIdentity` (+ :class:`Platform`) ← ``conda-meta/pixi``: which manifest / env /
  lock-hash / pixi-version materialised a prefix.
- :class:`Activation` ← ``pixi shell-hook --json``: the vars pixi sets on activation;
  :func:`activation_delta` / :func:`activated_env` are the pure "what activation adds"
  transforms — shipit never re-derives activation.

The pure core (parse + transforms) is :mod:`shipit.pixienv.model`; the I/O boundary
(read the file, shell out to ``pixi``) is :mod:`shipit.pixienv.read`. The sccache build
env that used to be hand-built in Python now lives in pixi ``[activation.env]``, so pixi
sets it on every activation and it reaches the agent's own in-Tree ``cargo`` — one more
"stop computing what pixi already computes" (docs/dev/pixi).
"""

from __future__ import annotations

from .model import (
    Activation,
    EnvIdentity,
    Platform,
    activated_env,
    activation_delta,
    activation_from_dict,
    env_identity_from_dict,
    parse_activation,
    parse_env_identity,
    path_entries,
)
from .read import (
    read_env_identity,
    read_fingerprint,
    shell_hook,
)

__all__ = [
    "Activation",
    "EnvIdentity",
    "Platform",
    "activated_env",
    "activation_delta",
    "activation_from_dict",
    "env_identity_from_dict",
    "parse_activation",
    "parse_env_identity",
    "path_entries",
    "read_env_identity",
    "read_fingerprint",
    "shell_hook",
]
