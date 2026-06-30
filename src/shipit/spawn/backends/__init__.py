"""``spawn/backends`` — the registry of per-backend launch adapters (ADR-0020).

``--backend`` selects an adapter; this package is where the adapters register and where
the verb resolves one. The registry is the **single source of truth** for which backends
exist: :func:`supported_backends` derives the ``SUPPORTED_BACKENDS`` tuple the CLI gate
uses *from the registered adapters* (ADR-0020 §Decision 2), so wiring a new backend is
one registry entry, not a constant edited in two places.

``claude`` (adapter #0, ADR-0019) and ``codex`` (WS02, ADR-0020 §codex) are wired today;
``antigravity`` lands in WS03 from the WS00 spike's recorded findings — NOT guessed here.
"""

from __future__ import annotations

from .base import BackendAdapter
from .claude import ClaudeAdapter
from .codex import CodexAdapter

#: The backend registry: ``--backend`` token → the (stateless, shared) adapter instance.
#: WS03 adds the ``"antigravity"`` entry; nothing else changes, because the CLI gate and
#: the verb both read the registry (not a hand-maintained constant), so registering an
#: adapter here makes ``--backend <token>`` selectable automatically.
_ADAPTERS: dict[str, BackendAdapter] = {
    ClaudeAdapter.name: ClaudeAdapter(),
    CodexAdapter.name: CodexAdapter(),
}


def supported_backends() -> tuple[str, ...]:
    """The backends ``spawn subagent`` can launch — derived from the registry.

    The adapter-driven ``SUPPORTED_BACKENDS`` (ADR-0020 §Decision 2): the order is the
    registration order, ``claude`` first. The verb gates ``--backend`` on this and
    re-checks it in :func:`shipit.verbs.spawn.run_subagent`, so an unknown backend fails
    loud at the verb boundary with no silent default to ``claude``.
    """
    return tuple(_ADAPTERS)


def resolve(backend: str) -> BackendAdapter:
    """Resolve a ``--backend`` token to its adapter, or raise :class:`KeyError`.

    Called by the verb *after* its explicit ``SUPPORTED_BACKENDS`` guard, so the key is
    already known to be present; the ``KeyError`` is a belt-and-braces guard for a
    programmatic caller that skipped the check, never a user-facing path (the verb owns
    the loud, clean unsupported-backend message).
    """
    return _ADAPTERS[backend]


__all__ = [
    "BackendAdapter",
    "ClaudeAdapter",
    "CodexAdapter",
    "resolve",
    "supported_backends",
]
