"""Domain-key log context (ADR-0029) — bind correlation keys, carry them across processes.

Correlation in shipit's durable JSONL record is **domain keys only** — the
closed set :data:`DOMAIN_KEYS` (``session``, ``tree``, ``pr``, ``run``,
``repo``) — never synthetic trace/span ids. A key is bound ONCE, at the CLI
entry or at a spawn/detach seam, via structlog's contextvars; from that point
:data:`shipit.logsetup._PIPELINE`'s ``merge_contextvars`` step lands it on
every subsequent record in-process. An unbound key is simply ABSENT from the
record — never ``None`` — so :func:`bind` drops ``None`` values instead of
binding them.

Cross-process propagation is the environment (ADR-0029: no package exists for
this; it is ~10 lines at the seams): :func:`env_export` returns a child
environment carrying every bound key as ``SHIPIT_LOG_CTX_<KEY>``, and
:func:`bind_from_env` — called by ``logsetup.configure_logging``, i.e. at the
child's logging setup — rebinds them, so a detached review child's records
carry the parent's ``pr``/``repo`` without any shared state. Numeric keys
(:data:`_INT_KEYS`) rebind as ``int`` so the round-trip preserves the jq
ergonomics the record is designed for (``jq 'select(.pr==231)'``).

The key set is CLOSED: an unknown name raises :class:`ValueError` at the bind
site, so a typo can never silently mint a new correlation vocabulary.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import Any

import structlog

#: The closed correlation-key set (ADR-0029). Agents slice the record by these
#: nouns; anything else on a record is an event extra, not a correlation key.
DOMAIN_KEYS = ("session", "tree", "pr", "run", "repo")

#: The env-var prefix a bound key exports under (``pr`` → ``SHIPIT_LOG_CTX_PR``),
#: shared by the writer (:func:`env_export`) and the reader (:func:`bind_from_env`)
#: so the two sides of the process boundary can never disagree on naming.
ENV_PREFIX = "SHIPIT_LOG_CTX_"

#: The numeric domain keys. The environment carries only strings, so these are
#: cast back to ``int`` on rebind — a record's ``pr`` must compare equal under
#: ``jq 'select(.pr==231)'`` whether it was bound in-process or via a parent.
_INT_KEYS = frozenset({"pr", "run"})


def _check_names(names: Iterable[str]) -> None:
    """Reject any name outside :data:`DOMAIN_KEYS` — fail loud at the bind site."""
    unknown = sorted(set(names) - set(DOMAIN_KEYS))
    if unknown:
        raise ValueError(
            f"unknown domain key(s) {unknown}; the closed correlation set is "
            f"{list(DOMAIN_KEYS)} (ADR-0029)"
        )


def bind(**keys: Any) -> None:
    """Bind domain keys into the log context for the rest of the process.

    ``None`` values are DROPPED, not bound: the record contract is
    present-when-bound / absent-not-null, so a seam can pass a maybe-known value
    (``run=run_id``) without guarding. An unknown key name raises
    :class:`ValueError`.
    """
    _check_names(keys)
    values = {name: value for name, value in keys.items() if value is not None}
    if values:
        structlog.contextvars.bind_contextvars(**values)


def unbind(*names: str) -> None:
    """Remove domain keys from the log context (absent from records again).

    Unbinding a key that is not bound is a no-op, mirroring structlog. An
    unknown key name raises :class:`ValueError`.
    """
    _check_names(names)
    if names:
        structlog.contextvars.unbind_contextvars(*names)


def bound() -> dict[str, Any]:
    """The currently-bound domain keys — ONLY the domain keys.

    Any other contextvar someone bound through structlog directly is not
    correlation vocabulary and is not reported (nor exported) here.
    """
    ctx = structlog.contextvars.get_contextvars()
    return {name: ctx[name] for name in DOMAIN_KEYS if name in ctx}


def env_export(env: Mapping[str, str] | None = None, **extra: Any) -> dict[str, str]:
    """A child-process environment carrying the bound domain keys (ADR-0029).

    Returns a COPY of ``env`` (default: ``os.environ``) with one
    ``SHIPIT_LOG_CTX_<KEY>`` entry per bound key; the input mapping is never
    mutated. Any ``SHIPIT_LOG_CTX_*`` entry the input environment already
    carried (e.g. inherited from THIS process's own parent) is scrubbed first,
    so the export reflects exactly the keys bound HERE — a key that is unbound
    (or explicitly ``None``) is ABSENT from the child environment, never a
    stale inherited value (the absent-when-unbound contract crosses the seam
    intact). ``extra`` adds seam-known keys to the CHILD's context without
    binding them in this parent (e.g. the detach seam threads ``run=run_id``,
    which belongs to the child's story); an ``extra`` overrides a bound key of
    the same name, and ``None`` extras are dropped like :func:`bind` drops them.
    The child rebinds via :func:`bind_from_env` at its logging setup.
    """
    _check_names(extra)
    merged = dict(os.environ if env is None else env)
    for name in DOMAIN_KEYS:
        merged.pop(ENV_PREFIX + name.upper(), None)
    for name, value in {**bound(), **extra}.items():
        if value is not None:
            merged[ENV_PREFIX + name.upper()] = str(value)
    return merged


def bind_from_env(env: Mapping[str, str] | None = None) -> None:
    """Rebind the domain keys a parent exported — the child half of the seam.

    Called at logging setup (``logsetup.configure_logging``), so ANY child
    shipit process spawned with an :func:`env_export` environment carries its
    parent's keys from its first record. Absent (or empty) vars bind nothing —
    the absent-not-null contract crosses the process boundary intact. Numeric
    keys (:data:`_INT_KEYS`) are cast back to ``int``; a malformed numeric
    value degrades to its raw string rather than crashing logging setup.
    """
    env = os.environ if env is None else env
    values: dict[str, Any] = {}
    for name in DOMAIN_KEYS:
        raw = env.get(ENV_PREFIX + name.upper())
        if not raw:
            continue
        value: Any = raw
        if name in _INT_KEYS:
            try:
                value = int(raw)
            except ValueError:
                pass
        values[name] = value
    if values:
        structlog.contextvars.bind_contextvars(**values)
