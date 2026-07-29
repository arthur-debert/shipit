"""``shipit.agent`` — the Backend / Model / Invocation axes, orthogonal by design."""

from __future__ import annotations

from .backend import (
    ANTIGRAVITY,
    CLAUDE,
    CODEX,
    REGISTRY,
    Backend,
    by_check_run_name,
    by_funnel_agent,
    by_name,
    funnel_backends,
)
from .invocation import (
    Invocation,
    Model,
    Provider,
    ReasoningLevel,
    intended_from_meta,
    model_of_id,
    observed_from_meta,
    supports,
)

__all__ = [
    "ANTIGRAVITY",
    "CLAUDE",
    "CODEX",
    "REGISTRY",
    "Backend",
    "Invocation",
    "Model",
    "Provider",
    "ReasoningLevel",
    "by_check_run_name",
    "by_funnel_agent",
    "by_name",
    "funnel_backends",
    "intended_from_meta",
    "model_of_id",
    "observed_from_meta",
    "supports",
]
