"""Resolving a :class:`~shipit.config.SecretSource` to its plaintext value.

The boundary (the ``doppler`` Exec, the environment, the interactive prompt) is
injected so the resolution policy — including the optional-skip rule — is pure
and unit-testable.

The ``doppler`` call goes through the one Exec runner (:mod:`shipit.execrun`,
ADR-0028) with ``check=False``: a nonzero rc is this layer's *semantic* failure
(:class:`SecretSourceError`), not a transport error — and, crucially, a
completed run under ``check=False`` records argv only (never the streams), so
the fetched secret in stdout can never ride the Exec record to a sink. The one
path that DOES capture stdout on failure is a timeout (partial output of a
killed child), so the call also passes ``secret_stdout=True`` — the runner then
suppresses that stdout from the failure record and the raised ``ExecError``,
closing the last gap through which the secret could reach a sink.

Every value fetched here is also registered with the central redactor
(:mod:`shipit.redact`, ADR-0028/0029) at fetch time — the one moment the
application provably holds a secret — so it can never appear in any log
record, on any sink.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping

from . import execrun, redact
from .config import SecretSource


class SecretSourceError(RuntimeError):
    """A required secret source could not be resolved."""


#: The ``doppler`` Exec's stated timeout, in seconds (ADR-0028: every Exec
#: states its bound deliberately — never the runner's implicit default). A
#: secrets fetch is a network round-trip to Doppler's API, so the runner's
#: generous default IS the right bound — stated on the wire rather than
#: inherited, so the no-implicit-timeout sweep stays grep-verifiable.
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
            # stdout carries the fetched secret; mark it so a timeout (which
            # captures the partial secret the child had written) never rides an
            # Exec failure record or a re-logged ExecError to a sink.
            secret_stdout=True,
        )
    except execrun.ExecError as exc:
        # The transport failures (missing binary, timeout, OS launch error) all
        # normalize into ExecError; re-shape them as this layer's semantic error.
        if exc.cause == execrun.CAUSE_MISSING_BINARY:
            raise SecretSourceError("doppler not found on PATH") from exc
        raise SecretSourceError(f"doppler get {key} failed: {exc}") from exc
    if result.rc != 0:
        raise SecretSourceError(f"doppler get {key} failed: {result.stderr.strip()}")
    value = result.stdout.rstrip("\n")
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
