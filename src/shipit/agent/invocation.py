"""``agent/invocation`` — the Model / Provider / ReasoningLevel / Invocation axes.

See docs/adr/0025-agent-model-invocation-axes.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Provider(Enum):
    """The vendor of a :class:`Model` — a closed registry."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GOOGLE = "google"

    @classmethod
    def coerce(cls, value: object) -> Provider | None:
        """The :class:`Provider` for ``value``, or ``None``; never raises on an unknown one."""
        if isinstance(value, cls):
            return value
        if not value:
            return None
        text = str(value).strip().lower()
        for member in cls:
            if member.value == text or member.name.lower() == text:
                return member
        return None


class ReasoningLevel(Enum):
    """The thinking-effort CHOSEN for one Invocation, distinct from a Model's capability."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @classmethod
    def coerce(cls, value: object) -> ReasoningLevel | None:
        """The :class:`ReasoningLevel` for ``value``, or ``None``; never raises on an unknown one."""
        if isinstance(value, cls):
            return value
        if not value:
            return None
        text = str(value).strip().lower()
        for member in cls:
            if member.value == text or member.name.lower() == text:
                return member
        return None


@dataclass(frozen=True)
class Model:
    """The LLM = ``(id, provider, reasoning_capability)``; its IDENTITY is the id alone."""

    id: str
    provider: Provider | None = field(default=None, compare=False)
    reasoning_capability: frozenset[ReasoningLevel] = field(
        default_factory=frozenset, compare=False
    )


#: A minimal known-model → provider map, not a catalogue; an unlisted id is still valid.
_KNOWN_PROVIDERS: dict[str, Provider] = {
    "gpt-5.5": Provider.OPENAI,
    "gpt-5.6-sol": Provider.OPENAI,
    "gpt-5.4-mini": Provider.OPENAI,
    "gemini 3.1 pro (high)": Provider.GOOGLE,
    "gemini 3.5 flash (high)": Provider.GOOGLE,
    "gemini 3.5 flash (low)": Provider.GOOGLE,
}


def model_of_id(model_id: str | None) -> Model | None:
    """A :class:`Model` for a verbatim id; ``None`` only for a blank/absent id."""
    if not model_id:
        return None
    provider = _KNOWN_PROVIDERS.get(str(model_id).strip().lower())
    if provider is None and str(model_id).strip().lower().startswith(
        ("claude", "sonnet", "opus", "haiku")
    ):
        provider = Provider.ANTHROPIC
    return Model(id=str(model_id), provider=provider)


@dataclass(frozen=True)
class Invocation:
    """One Run's configured launch; any field may be ``None``, and any pairing constructs."""

    backend: str | None = None
    model: Model | None = None
    reasoning_level: ReasoningLevel | None = None
    permission_mode: str | None = None

    def as_record(self) -> dict[str, Any]:
        """The flat, null-safe dict stamped into the eval record; the report groups by its keys."""
        return {
            "backend": self.backend,
            "model": self.model.id if self.model else None,
            "provider": (
                self.model.provider.value
                if self.model and self.model.provider
                else None
            ),
            "reasoning_level": (
                self.reasoning_level.value if self.reasoning_level else None
            ),
            "permission_mode": self.permission_mode,
        }


def supports(backend_name: str | None, model: Model | None) -> bool:
    """Whether ``model`` is a KNOWN-good pairing for that backend — a lookup, not a constraint."""
    from . import backend as backend_mod

    if not backend_name or model is None:
        return False
    try:
        be = backend_mod.by_name(backend_name)
    except KeyError:
        return False
    known = {be.resolve_model(alias) for alias in be.model_aliases} | set(
        be.model_aliases.values()
    )
    if be.default_model is not None:
        known.add(be.resolve_model(be.default_model))
    return model.id in known


def observed_from_meta(meta: Mapping[str, Any] | None) -> Invocation:
    """The OBSERVED Invocation for a run; a missing field is ``None``, backend defaults ``claude``."""
    data = meta or {}
    backend = str(data.get("backend") or "").strip() or "claude"
    model = model_of_id(data.get("model"))
    level = ReasoningLevel.coerce(data.get("reasoning") or data.get("reasoningLevel"))
    permission = data.get("spawnMode") or data.get("permissionMode")
    return Invocation(
        backend=backend,
        model=model,
        reasoning_level=level,
        permission_mode=str(permission) if permission else None,
    )


def intended_from_meta(meta: Mapping[str, Any] | None) -> Invocation | None:
    """The INTENDED Invocation from a meta ``invocation`` block, or ``None`` when unstamped."""
    data = meta or {}
    intent = data.get("invocation")
    if not isinstance(intent, Mapping):
        return None
    model_id = intent.get("model")
    model = None
    if model_id:
        provider = Provider.coerce(intent.get("provider"))
        # `model_id` is truthy here, so `model_of_id` never returns None.
        base = model_of_id(model_id)
        assert base is not None
        model = Model(id=base.id, provider=provider or base.provider)
    permission = intent.get("permission_mode") or intent.get("permissionMode")
    return Invocation(
        backend=(str(intent.get("backend")).strip() or None)
        if intent.get("backend")
        else None,
        model=model,
        reasoning_level=ReasoningLevel.coerce(intent.get("reasoning_level")),
        permission_mode=str(permission) if permission else None,
    )
