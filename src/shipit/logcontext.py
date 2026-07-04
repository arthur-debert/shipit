"""Domain-key log context (ADR-0029/0032) — bind correlation keys, carry them across processes.

Correlation in shipit's durable JSONL record is **domain keys only** — the
closed set :data:`DOMAIN_KEYS` (``session``, ``tree``, ``pr``, ``run``,
``repo``, plus the dev-cycle four ADR-0032 added: ``epic``, ``ws``, ``agent``,
``role``) — never synthetic trace/span ids. A key binds via structlog's
contextvars at the seam where its value becomes known — the CLI entry, a
spawn/detach seam, or the moment a subsystem starts working on the noun (the
PR-engine's fetch, the review service's detach; LOG02) — and from that point
:data:`shipit.logsetup._PIPELINE`'s :func:`merge_domain_keys` step lands it on
every subsequent record in-process. :func:`bind` is process-lifetime — right
when the whole remaining run is about that noun; :func:`scoped` (LOG02) is the
``with``-block form for a correlation LOCAL to one bounded operation (a single
Tree's creation), unwound on exit so unrelated later records don't inherit it.
An unbound key is simply ABSENT from the record — never ``None`` — so both
forms drop ``None`` values instead of binding them.

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
from collections.abc import Iterable, Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from typing import Any

import structlog

#: The closed correlation-key set (ADR-0029, grown to nine by ADR-0032 / LOG04).
#: Agents slice the record by these nouns; anything else on a record is an
#: event extra, not a correlation key. The dev-cycle four: ``epic`` is the
#: human-assigned code string (``RVW01``); ``ws`` is the Work Stream index as
#: an **int** (``WS01`` is a display form, never data — it joins the int-typed
#: set below); ``agent`` is the spawn id; ``role`` is the Role registry name.
DOMAIN_KEYS = ("session", "tree", "pr", "run", "repo", "epic", "ws", "agent", "role")

#: The env-var prefix a bound key exports under (``pr`` → ``SHIPIT_LOG_CTX_PR``),
#: shared by the writer (:func:`env_export`) and the reader (:func:`bind_from_env`)
#: so the two sides of the process boundary can never disagree on naming.
ENV_PREFIX = "SHIPIT_LOG_CTX_"

#: The numeric domain keys. The environment carries only strings, so these are
#: cast back to ``int`` on rebind — a record's ``pr`` must compare equal under
#: ``jq 'select(.pr==231)'`` whether it was bound in-process or via a parent.
#: ``ws`` joins them (ADR-0032): the Work Stream index is data as an int;
#: ``WS01`` is rendering.
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


@contextmanager
def scoped(**keys: Any) -> Iterator[None]:
    """Bind domain keys for the duration of the ``with`` block, then restore.

    Same present-when-bound / absent-not-null contract as :func:`bind` (``None``
    values dropped, unknown key names raise :class:`ValueError`), but the binding
    is UNWOUND on exit — every record emitted inside the block carries the keys,
    and a later in-process record does not inherit them. Use at a seam whose
    correlation is LOCAL to a bounded operation (a single Tree's creation), where
    a process-lifetime :func:`bind` would corrupt the correlation fields of every
    subsequent, unrelated record. Prior values of the same keys are restored, so
    nesting under an outer :func:`bind` leaves that outer binding intact.
    """
    _check_names(keys)
    values = {name: value for name, value in keys.items() if value is not None}
    with structlog.contextvars.bound_contextvars(**values):
        yield


def unbind(*names: str) -> None:
    """Remove domain keys from the log context (absent from records again).

    Unbinding a key that is not bound is a no-op, mirroring structlog. An
    unknown key name raises :class:`ValueError`.
    """
    _check_names(names)
    if names:
        structlog.contextvars.unbind_contextvars(*names)


@contextmanager
def cleared(*names: str) -> Iterator[None]:
    """Ensure domain keys are ABSENT for the duration of the block, then restore.

    The scoped inverse of :func:`scoped`: where ``scoped`` binds a value local
    to a block, this removes a key local to a block — every record emitted
    inside carries it as absent (never ``None``), and the ENTRY STATE is
    restored on exit: a key bound at entry gets its prior value back, and a key
    unbound at entry comes back out unbound, even if the block bound it (the
    same unwind contract as ``scoped`` — an in-block :func:`bind` of a named
    key never leaks past the block). A seam needs this when the LOCAL truth is
    that a key does not apply: an umbrella branch carries an epic but no Work
    Stream, so it must suppress an env-propagated ``ws`` for its emission
    rather than let a stale value fuse into a mixed identity. An unknown key
    name raises :class:`ValueError`.
    """
    _check_names(names)
    saved = {name: value for name, value in bound().items() if name in names}
    unbind(*names)
    try:
        yield
    finally:
        # Restore ABSENCE too, not just values: unbind every named key again
        # (dropping any in-block bind), then rebind only what entry saved.
        unbind(*names)
        if saved:
            structlog.contextvars.bind_contextvars(**saved)


def bound() -> dict[str, Any]:
    """The currently-bound domain keys — ONLY the domain keys.

    Any other contextvar someone bound through structlog directly is not
    correlation vocabulary and is not reported (nor exported) here.
    """
    ctx = structlog.contextvars.get_contextvars()
    return {name: ctx[name] for name in DOMAIN_KEYS if name in ctx}


def merge_domain_keys(
    logger: object, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: merge ONLY the bound domain keys into the record.

    The vocabulary-preserving replacement for structlog's ``merge_contextvars``
    in :data:`shipit.logsetup._PIPELINE`: where ``merge_contextvars`` would
    land EVERY structlog contextvar on the durable record — letting a direct
    ``structlog.contextvars.bind_contextvars(request_id=...)`` call mint a
    top-level JSONL field outside the closed set — this merges exactly
    :func:`bound`, i.e. :data:`DOMAIN_KEYS` and nothing else. Ambient context
    is correlation vocabulary or it is nothing; per-event extras stay what
    they always were (explicit keys on the log call). An explicit event key
    wins over a bound one, mirroring ``merge_contextvars``.
    """
    for name, value in bound().items():
        event_dict.setdefault(name, value)
    return event_dict


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
