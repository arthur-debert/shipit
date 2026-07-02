"""Resolving a :class:`~shipit.config.SecretSource` to its plaintext value.

The boundary (the ``doppler`` subprocess, the environment, the interactive
prompt) is injected so the resolution policy — including the optional-skip rule —
is pure and unit-testable.

Every value fetched here is registered with the central redactor
(:mod:`shipit.redact`, ADR-0028/0029) at fetch time — the one moment the
application provably holds a secret — so it can never appear in any log
record, on any sink.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping

from . import redact
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
        raise SecretSourceError(f"doppler get {key} failed: {proc.stderr.strip()}")
    value = proc.stdout.rstrip("\n")
    # Registered HERE as well as in resolve(): ghauth calls doppler_get
    # directly, and that path must be masked too.
    redact.register_secret(value)
    return value


def resolve(
    source: SecretSource,
    *,
    doppler_get: Callable[[str], str] = doppler_get,
    env: Mapping[str, str] | None = None,
    prompt: Callable[[str], str] | None = None,
) -> str | None:
    """Resolve ``source`` to its value, or ``None`` when optional and absent.

    A missing REQUIRED source raises :class:`SecretSourceError`; a missing
    OPTIONAL one returns ``None`` (the caller skips it — never fatal). Every
    resolved value is registered with the central redactor before it is
    returned, whatever the kind and whatever boundary was injected.
    """
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
