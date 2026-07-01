"""agent — the agent-harness deep module: Backend / Model / Invocation axes (ADR-0025).

shipit's agent layer is **orthogonal axes sharing a single identity** — not one merged
"reviewer/backend" type. This package is that model:

  * :mod:`shipit.agent.backend` — the ONE agent-backend identity/alias registry
    (:class:`Backend`): every alias (spawn ``--backend`` token, funnel login
    ``adr-<agent>-review[bot]``, check-run ``<agent>-local``, Doppler App keys, model
    aliases) defined once and referenced by BOTH the launch axis
    (:mod:`shipit.spawn.backends`) and the PR-funnel axis
    (:mod:`shipit.review.ghauth` / :mod:`shipit.prstate.reviewers`). Backend ⊥ Reviewer,
    Backend ⊥ Role.
  * :mod:`shipit.agent.invocation` — the launch-config value objects
    (:class:`Provider`, :class:`ReasoningLevel`, :class:`Model`, :class:`Invocation`),
    threaded spawn → Run → eval record as a group-by dimension for
    ``shipit eval report``. Distinct from :class:`shipit.harness.eval.variant.Variant`.
"""

from __future__ import annotations

from .backend import (
    ANTIGRAVITY,
    CLAUDE,
    CODEX,
    REGISTRY,
    Backend,
    by_funnel_agent,
    by_name,
    funnel_backends,
    funnel_doppler_keys,
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
    "by_funnel_agent",
    "by_name",
    "funnel_backends",
    "funnel_doppler_keys",
    "intended_from_meta",
    "model_of_id",
    "observed_from_meta",
    "supports",
]
