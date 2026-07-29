"""Resolving a :class:`~shipit.config.SecretSource` to its plaintext value.

Every fetched value is registered with the central redactor at fetch time.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping

from . import execrun, redact
from .config import SecretSource


class SecretSourceError(RuntimeError):
    """A required secret source could not be resolved."""


DOPPLER_TIMEOUT: float = execrun.DEFAULT_TIMEOUT


def doppler_get(key: str) -> str:
    """``doppler secrets get <key> --plain --project github --config prd``."""
    try:
        result = execrun.run(
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
            check=False,
            timeout=DOPPLER_TIMEOUT,
            secret_stdout=True,
        )
    except execrun.ExecError as exc:
        if exc.cause == execrun.CAUSE_MISSING_BINARY:
            raise SecretSourceError("doppler not found on PATH") from exc
        raise SecretSourceError(f"doppler get {key} failed: {exc}") from exc
    if result.rc != 0:
        raise SecretSourceError(f"doppler get {key} failed: {result.stderr.strip()}")
    value = result.stdout.rstrip("\n")
    redact.register_secret(value)
    return value


def resolve(
    source: SecretSource,
    *,
    doppler_get: Callable[[str], str] = doppler_get,
    env: Mapping[str, str] | None = None,
    prompt: Callable[[str], str] | None = None,
) -> str | None:
    """Resolve ``source`` to its value, ``None`` when optional and absent; a missing required source raises."""
    env = os.environ if env is None else env
    try:
        value = _fetch(source, doppler_get=doppler_get, env=env, prompt=prompt)
    except SecretSourceError:
        if source.optional:
            return None
        raise
    redact.register_secret(value)
    return value


def _fetch(
    source: SecretSource,
    *,
    doppler_get: Callable[[str], str],
    env: Mapping[str, str],
    prompt: Callable[[str], str] | None,
) -> str:
    """Fetch ``source``'s value from its boundary, or raise :class:`SecretSourceError`."""
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
