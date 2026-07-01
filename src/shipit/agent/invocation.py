"""agent.invocation — the Model / Provider / ReasoningLevel / Invocation axes (ADR-0025).

The launch-config value objects, **orthogonal to** :class:`shipit.agent.backend.Backend`
and modeled as thin frozen values with free functions over them (ADR-0021):

  * :class:`Provider` — the model vendor, a closed registry (``anthropic | openai |
    google``). The hook for auth/billing and cross-backend model use; never part of a
    Repo/Run identity.
  * :class:`ReasoningLevel` — the thinking-effort knob (``low | medium | high``),
    normalized across backends so eval compares them, CHOSEN per-invocation (distinct
    from a Model's reasoning *capability*).
  * :class:`Model` — the LLM = ``(id, provider, reasoning_capability)``, **decoupled
    from Backend** (identity is the canonical model id alone; provider + capability are
    ``compare=False``). A model of one provider may be paired with a backend of another.
  * :class:`Invocation` — the configured launch of one Run = **Backend × Model ×
    ReasoningLevel** (+ ``permission_mode``). Backend×Model validity is a **lookup**
    (:func:`supports`), NOT a structural constraint — an :class:`Invocation` pairing a
    cross-provider Backend and Model is *expressible* (constructs without error); the
    lookup merely reports whether it is a known-good pairing.

:class:`Invocation` is threaded spawn → Run → **eval record**: the *observed* config is
read from a run's ``.meta.json`` (:func:`observed_from_meta`) alongside the *intended*
(:func:`intended_from_meta` — a clean seam, ``None`` until the spawn surface stamps
intent into the meta), and both ride the record as ``eval.invocation`` where
``shipit eval report`` groups by them. Distinct from **Variant** (the prompt/policy
content-hash axis).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Provider(Enum):
    """The vendor of a :class:`Model` — a closed registry (CONTEXT.md "Provider")."""

    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GOOGLE = "google"

    @classmethod
    def coerce(cls, value: object) -> Provider | None:
        """The :class:`Provider` for ``value`` (a member, its ``.value``, or a name),
        or ``None`` — tolerant so an unknown/absent observed provider never raises."""
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
    """The thinking-effort chosen for one :class:`Invocation` — a closed registry
    (``low | medium | high``), normalized so eval compares across backends. A *chosen
    level*, distinct from a :class:`Model`'s reasoning *capability*."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @classmethod
    def coerce(cls, value: object) -> ReasoningLevel | None:
        """The :class:`ReasoningLevel` for ``value`` (a member, its ``.value``, or a
        name), or ``None`` — tolerant so an unknown/absent observed level never raises."""
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
    """The LLM = ``(id, provider, reasoning_capability)`` — identity is the id alone.

    ``provider`` and ``reasoning_capability`` are ``compare=False`` so the identity is
    the canonical model id (CONTEXT.md "Model"): the same id enriched with a provider
    hashes/compares identically. ``reasoning_capability`` is the set of
    :class:`ReasoningLevel`\\ s the model supports (possibly empty), intrinsic to it and
    distinct from the level *chosen* on an :class:`Invocation`.
    """

    id: str
    provider: Provider | None = field(default=None, compare=False)
    reasoning_capability: frozenset[ReasoningLevel] = field(
        default_factory=frozenset, compare=False
    )


#: A minimal known-model → provider map: enough to enrich an OBSERVED model id read
#: from a run's meta with its provider for the eval group-by, without pretending to be
#: a full catalogue. An id not listed resolves to a :class:`Model` with ``provider=None``
#: (still a valid identity) — the config *optimizer*, not a model catalogue, is the
#: future work (ADR-0025 §Consequences).
_KNOWN_PROVIDERS: dict[str, Provider] = {
    "gpt-5.5": Provider.OPENAI,
    "gpt-5.4-mini": Provider.OPENAI,
    "gemini 3.1 pro (high)": Provider.GOOGLE,
    "gemini 3.5 flash (high)": Provider.GOOGLE,
    "gemini 3.5 flash (low)": Provider.GOOGLE,
}


def model_of_id(model_id: str | None) -> Model | None:
    """A :class:`Model` for a verbatim id, with a known provider filled in when we
    recognize it (else ``provider=None``). ``None`` for a blank/absent id."""
    if not model_id:
        return None
    provider = _KNOWN_PROVIDERS.get(str(model_id).strip().lower())
    # A `claude-*` / `anthropic` id is the first-party default — recognize the family.
    if provider is None and str(model_id).strip().lower().startswith(
        ("claude", "sonnet", "opus", "haiku")
    ):
        provider = Provider.ANTHROPIC
    return Model(id=str(model_id), provider=provider)


@dataclass(frozen=True)
class Invocation:
    """The configured launch of one Run — **Backend × Model × ReasoningLevel** (+
    ``permission_mode``).

    Holds the composed value objects (a ``backend`` NAME string, a :class:`Model`, a
    :class:`ReasoningLevel`, a ``permission_mode`` string) — any field may be ``None``
    when a run did not record it. Backend×Model validity is a **lookup** (:func:`supports`),
    never enforced here: a cross-provider pairing constructs freely so the harness can
    *express* and later measure it. :meth:`as_record` flattens it to the JSON object the
    eval record carries under ``eval.invocation`` and the report groups by.
    """

    backend: str | None = None
    model: Model | None = None
    reasoning_level: ReasoningLevel | None = None
    permission_mode: str | None = None

    def as_record(self) -> dict[str, Any]:
        """The JSON-serializable dict stamped into the eval record. Flat, null-safe,
        and stable (the report's group-by keys read these field names)."""
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
    """Whether ``model`` is a KNOWN-good pairing for the backend named ``backend_name``.

    A **lookup, not a structural constraint** (ADR-0025): a pairing that returns
    ``False`` is still *expressible* as an :class:`Invocation`; this only reports
    membership in the backend's known model set (its alias table's resolved ids). An
    unknown backend, or a model with no id, is ``False``.
    """
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
    """The OBSERVED :class:`Invocation` for a run, from its ``.meta.json`` (PURE).

    The meta sidecar the harness writes carries the observed ``model`` and ``spawnMode``
    (the permission mode); an optional ``backend`` / ``reasoning`` (or ``reasoningLevel``)
    key is read when present. A missing field is ``None`` — the record is still valid.
    Backend defaults to ``claude`` when unspecified because the terminal eval hooks fire
    for Claude Code coordinator / subagent runs, whose backend IS ``claude``.
    """
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
    """The INTENDED :class:`Invocation`, from a meta ``invocation`` intent block (PURE).

    A clean seam (mirroring how WS01 left ``variant`` a placeholder): the spawn surface
    MAY stamp the intended launch config into the meta under ``invocation`` (``{backend,
    model, provider, reasoning_level, permission_mode}``); until it does, this is
    ``None`` and only the observed side is recorded. When present, the block is read
    tolerantly (any field may be absent).
    """
    data = meta or {}
    intent = data.get("invocation")
    if not isinstance(intent, Mapping):
        return None
    model_id = intent.get("model")
    model = None
    if model_id:
        provider = Provider.coerce(intent.get("provider"))
        # `model_id` is truthy here, so `model_of_id` never returns None (it only
        # returns None for a blank/absent id) — the base Model always exists.
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
