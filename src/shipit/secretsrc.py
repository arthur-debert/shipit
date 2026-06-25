"""Resolving a :class:`~shipit.config.SecretSource` to its plaintext value.

The boundary (the ``doppler`` subprocess, the environment, the interactive
prompt) is injected so the resolution policy — including the optional-skip rule —
is pure and unit-testable.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping

from .config import SecretSource


class SecretSourceError(RuntimeError):
    """A required secret source could not be resolved."""


def doppler_get(key: str) -> str:
    """``doppler secrets get <key> --plain --project github --config prd``."""
    try:
        proc = subprocess.run(
            [
                "doppler",
                "secrets",
                "get",
                key,
                "--plain",
                "--project",
                "github",
                "--config",
                "prd",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise SecretSourceError("doppler not found on PATH") from exc
    if proc.returncode != 0:
        raise SecretSourceError(
            f"doppler get {key} failed: {proc.stderr.strip()}"
        )
    return proc.stdout.rstrip("\n")


def resolve(
    source: SecretSource,
    *,
    doppler_get: Callable[[str], str] = doppler_get,
    env: Mapping[str, str] | None = None,
    prompt: Callable[[str], str] | None = None,
) -> str | None:
    """Resolve ``source`` to its value, or ``None`` when optional and absent.

    A missing REQUIRED source raises :class:`SecretSourceError`; a missing
    OPTIONAL one returns ``None`` (the caller skips it — never fatal).
    """
    env = os.environ if env is None else env
    try:
        if source.kind == "doppler":
            assert source.key is not None
            return doppler_get(source.key)
        if source.kind == "env":
            assert source.key is not None
            value = env.get(source.key)
            if not value:
                raise SecretSourceError(f"env {source.key} not set")
            return value
        if source.kind == "prompt":
            if prompt is None:
                raise SecretSourceError(
                    f"{source.name}: prompt source needs an interactive prompt"
                )
            value = prompt(source.name)
            if not value:
                raise SecretSourceError(f"{source.name}: empty prompt input")
            return value
        raise SecretSourceError(f"{source.name}: unknown source kind {source.kind!r}")
    except SecretSourceError:
        if source.optional:
            return None
        raise
