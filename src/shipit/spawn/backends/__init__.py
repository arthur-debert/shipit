"""The registry of per-backend launch adapters that ``--backend`` selects from."""

from __future__ import annotations

from .antigravity import AntigravityAdapter
from .base import BackendAdapter
from .claude import ClaudeAdapter
from .codex import CodexAdapter

#: ``--backend`` token → the shared default adapter instance. Registering here is
#: all that makes a token selectable.
_ADAPTERS: dict[str, BackendAdapter] = {
    ClaudeAdapter.name: ClaudeAdapter(),
    CodexAdapter.name: CodexAdapter(),
    AntigravityAdapter.name: AntigravityAdapter(),
}


def supported_backends() -> tuple[str, ...]:
    """The launchable backends, in registration order."""
    return tuple(_ADAPTERS)


def resolve(backend: str) -> BackendAdapter:
    """Resolve a ``--backend`` token to its adapter, or raise :class:`KeyError`."""
    return _ADAPTERS[backend]


__all__ = [
    "AntigravityAdapter",
    "BackendAdapter",
    "ClaudeAdapter",
    "CodexAdapter",
    "resolve",
    "supported_backends",
]
