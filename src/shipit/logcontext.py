"""Domain-key log context: bind correlation keys and carry them across processes.
See docs/adr/0029-agents-first-jsonl-logging.md
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from typing import Any

import structlog

DOMAIN_KEYS = ("session", "tree", "pr", "run", "repo", "epic", "ws", "agent", "role")

ENV_PREFIX = "SHIPIT_LOG_CTX_"

_INT_KEYS = frozenset({"pr", "run", "ws"})


def _check_names(names: Iterable[str]) -> None:
    """Reject any name outside :data:`DOMAIN_KEYS` — fail loud at the bind site."""
    unknown = sorted(set(names) - set(DOMAIN_KEYS))
    if unknown:
        raise ValueError(
            f"unknown domain key(s) {unknown}; the closed correlation set is "
            f"{list(DOMAIN_KEYS)} (ADR-0029)"
        )


def bind(**keys: Any) -> None:
    """Bind domain keys for the rest of the process; ``None`` values are DROPPED, not bound."""
    _check_names(keys)
    values = {name: value for name, value in keys.items() if value is not None}
    if values:
        structlog.contextvars.bind_contextvars(**values)


@contextmanager
def scoped(**keys: Any) -> Iterator[None]:
    """Bind domain keys for the ``with`` block, restoring any prior values on exit."""
    _check_names(keys)
    values = {name: value for name, value in keys.items() if value is not None}
    with structlog.contextvars.bound_contextvars(**values):
        yield


def unbind(*names: str) -> None:
    _check_names(names)
    if names:
        structlog.contextvars.unbind_contextvars(*names)


@contextmanager
def cleared(*names: str) -> Iterator[None]:
    """Keep domain keys ABSENT for the block, restoring the ENTRY state — value or absence — on exit."""
    _check_names(names)
    saved = {name: value for name, value in bound().items() if name in names}
    unbind(*names)
    try:
        yield
    finally:
        unbind(*names)
        if saved:
            structlog.contextvars.bind_contextvars(**saved)


def bound() -> dict[str, Any]:
    """The currently-bound domain keys, and ONLY those — any other contextvar is not reported."""
    ctx = structlog.contextvars.get_contextvars()
    return {name: ctx[name] for name in DOMAIN_KEYS if name in ctx}


def merge_domain_keys(
    logger: object, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor merging ONLY the bound domain keys; an explicit event key wins over a bound one."""
    for name, value in bound().items():
        event_dict.setdefault(name, value)
    return event_dict


def env_export(env: Mapping[str, str] | None = None, **extra: Any) -> dict[str, str]:
    """A COPY of ``env`` carrying the bound keys as ``SHIPIT_LOG_CTX_<KEY>``; inherited exports are scrubbed and ``extra`` keys ride to the child unbound here."""
    _check_names(extra)
    merged = scrub_env(os.environ if env is None else env)
    for name, value in {**bound(), **extra}.items():
        if value is not None:
            merged[ENV_PREFIX + name.upper()] = str(value)
    return merged


def scrub_env(env: Mapping[str, str]) -> dict[str, str]:

    scrubbed = dict(env)
    for name in DOMAIN_KEYS:
        scrubbed.pop(ENV_PREFIX + name.upper(), None)
    return scrubbed


def role_from_env(env: Mapping[str, str] | None = None) -> str | None:
    """The spawned ``role`` a parent exported, read straight from the environment; ``None`` when unset or blank."""
    env = os.environ if env is None else env
    return (env.get(ENV_PREFIX + "ROLE") or "").strip() or None


def bind_from_env(env: Mapping[str, str] | None = None) -> None:
    """Rebind the keys a parent exported; numeric keys become ``int``, and a malformed one degrades to its raw string."""
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
