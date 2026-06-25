"""backends — the review-backend registry.

Adding a backend is adding it to ``_REGISTRY``; callers resolve one by name via
``get_backend`` and depend only on the :class:`~.base.Backend` interface, never
on a backend's concrete type.
"""

from __future__ import annotations

from .agy import AgyBackend
from .base import Backend, BackendError, BackendUnavailable
from .codex import CodexBackend

_REGISTRY: dict[str, type[Backend]] = {
    "codex": CodexBackend,
    "agy": AgyBackend,
}

__all__ = [
    "AgyBackend",
    "Backend",
    "BackendError",
    "BackendUnavailable",
    "CodexBackend",
    "get_backend",
]


def get_backend(name: str, **kwargs) -> Backend:
    """Return a fresh backend instance for ``name`` (one of ``codex`` / ``agy``).

    Extra keyword arguments (e.g. ``model``) are forwarded to the backend
    constructor. Raises :class:`ValueError` for an unknown name.
    """
    try:
        cls = _REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY))
        raise ValueError(
            f"Unknown review backend '{name}'. Known backends: {known}."
        ) from None
    return cls(**kwargs)
