"""agent.backend — the ONE agent-backend identity/alias registry (ADR-0025).

The agent layer is modeled as **orthogonal axes sharing a single identity**
(ADR-0025 / CONTEXT.md "Backend"). This module owns that single identity: one
:class:`Backend` value object per harness/CLI (``claude | codex | antigravity``),
defining its canonical name plus **every alias** — the spawn ``--backend`` token,
the review-funnel agent name, the funnel App login ``adr-<agent>-review[bot]``, the
check-run ``<agent>-local``, the Doppler App-credential key names, and the
model-alias table — **once**, so the two axes both reference it instead of carrying
duplicate tables:

  * the **launch axis** (:mod:`shipit.spawn.backends`) reads ``model_aliases`` /
    ``default_model`` / ``binary`` off the identity;
  * the **PR-funnel axis** reads the Doppler keys (:mod:`shipit.review.ghauth`) and
    the login / slug / check-run names (:mod:`shipit.prstate.reviewers`) off it.

**Backend ⊥ Reviewer** and **Backend ⊥ Role**: a :class:`Backend` is shared
*identity*, not behaviour — the launch adapter (how-to-launch) and the reviewer
adapter (PR-funnel posture) stay separate classes; they only agree on the *names*
defined here. A backend with no funnel identity (``claude`` — it is never a funnel
App reviewer) simply leaves ``funnel_agent`` ``None`` and the funnel-only aliases
raise if asked for, so a caller can never silently mint a nonsense ``adr-claude-…``.

The registry is a **closed** tuple (:data:`REGISTRY`) — new backends are one entry
here, referenced everywhere else (ADR-0021 closed-registry-over-hierarchy).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


@dataclass(frozen=True)
class Backend:
    """One agent harness/CLI's identity — its canonical name plus every alias.

    Identity is the **canonical name** alone (every other field is ``compare=False``),
    so two references to the same backend are equal/hash the same regardless of the
    alias data hanging off them. The value object is thin and frozen (ADR-0021); the
    per-alias derivations are pure properties over the stored primitives.
    """

    #: The canonical name — ALSO the spawn ``--backend`` token (they are the same
    #: alias; ADR-0025 lists ``--backend`` token as one of the identity's aliases).
    name: str
    #: The CLI binary that must be on PATH to launch/preflight this backend
    #: (``claude`` / ``codex`` / ``agy`` — note ``antigravity`` shells out to ``agy``).
    binary: str = field(compare=False)
    #: The review-funnel agent name (``codex`` / ``agy``), or ``None`` for a backend
    #: that is NOT a funnel App reviewer (``claude``). The funnel-only aliases
    #: (:pyattr:`funnel_login`, :pyattr:`bot_slug_fragment`, :pyattr:`check_run_name`,
    #: the Doppler keys) derive from it, so leaving it ``None`` makes those raise
    #: rather than fabricate an identity the backend does not have.
    funnel_agent: str | None = field(default=None, compare=False)
    #: The Doppler ``github/prd`` key PREFIX for this backend's review GitHub App
    #: credentials (e.g. ``CODEX_REVIEW_APP`` → ``CODEX_REVIEW_APP_PRIVATE_KEY`` /
    #: ``CODEX_REVIEW_APP_ID``). ``None`` when the backend has no funnel App.
    doppler_app_prefix: str | None = field(default=None, compare=False)
    #: Legacy review aliases → this backend's verbatim model ids (the SINGLE copy of
    #: what used to be a per-adapter ``MODEL_ALIASES`` map). An already-verbatim id
    #: passes through :meth:`resolve_model`. Empty for a backend with no aliases
    #: (``claude``).
    model_aliases: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({}), compare=False
    )
    #: The default model for a write Run when the caller names none — resolved
    #: through :meth:`resolve_model` at use. ``None`` for a backend that requires an
    #: explicit model (``claude`` picks its own default).
    default_model: str | None = field(default=None, compare=False)

    @property
    def has_funnel_identity(self) -> bool:
        """True when this backend is a review-funnel App reviewer (codex / agy)."""
        return self.funnel_agent is not None

    def _require_funnel(self, alias: str) -> str:
        if self.funnel_agent is None:
            raise ValueError(
                f"backend {self.name!r} has no funnel identity, so {alias!r} is "
                "undefined (it is not a review-funnel App reviewer)."
            )
        return self.funnel_agent

    @property
    def app_slug(self) -> str:
        """The review GitHub App's slug — ``adr-<agent>-review`` (the App the
        funnel authenticates as; its bot login is :pyattr:`funnel_login`). Raises
        if no funnel identity."""
        return f"adr-{self._require_funnel('app_slug')}-review"

    @property
    def funnel_login(self) -> str:
        """The GitHub App bot login a posted review is attributed to —
        ``adr-<agent>-review[bot]`` (ADR-0025 alias; the :pyattr:`app_slug` with
        GitHub's ``[bot]`` suffix). Raises if no funnel identity."""
        return f"{self.app_slug}[bot]"

    @property
    def bot_slug_fragment(self) -> str:
        """The stable login slug fragment the reviewer adapter matches on —
        ``<agent>-review`` (so ``adr-<agent>-review[bot]`` matches WITHOUT hardcoding
        the ``adr-`` prefix). Raises if no funnel identity."""
        return f"{self._require_funnel('bot_slug_fragment')}-review"

    @property
    def check_run_name(self) -> str:
        """The OBS02 funnel check-run reviewer name — ``<agent>-local`` (ADR-0005;
        the ``review: <agent>-local`` gate). Raises if no funnel identity."""
        return f"{self._require_funnel('check_run_name')}-local"

    @property
    def doppler_pem_key(self) -> str:
        """The Doppler key for the App private key (PEM). Raises if no funnel App."""
        return f"{self._require_doppler()}_PRIVATE_KEY"

    @property
    def doppler_app_id_key(self) -> str:
        """The Doppler key for the App id. Raises if no funnel App."""
        return f"{self._require_doppler()}_ID"

    def _require_doppler(self) -> str:
        if self.doppler_app_prefix is None:
            raise ValueError(
                f"backend {self.name!r} has no review-App Doppler prefix "
                "(it is not a funnel App reviewer)."
            )
        return self.doppler_app_prefix

    def resolve_model(self, model: str | None = None) -> str:
        """Resolve ``model`` (or :pyattr:`default_model` when ``None``) to a verbatim id.

        A legacy alias (``pro`` / ``flash`` / …) maps through :pyattr:`model_aliases`;
        an already-verbatim id passes through unchanged. This is the ONE place the
        alias table is consulted, shared by every launch adapter.
        """
        chosen = model if model is not None else self.default_model
        if chosen is None:
            raise ValueError(
                f"backend {self.name!r} has no default model; pass one explicitly."
            )
        return self.model_aliases.get(chosen, chosen)


#: The ``claude`` backend — the first-party harness. NOT a funnel App reviewer (it is
#: never a codex/agy-style capture reviewer), so it carries no funnel aliases and no
#: model-alias table (claude picks its own default model).
CLAUDE = Backend(name="claude", binary="claude")

#: The ``codex`` backend — OpenAI's Codex CLI. A funnel App reviewer
#: (``adr-codex-review[bot]`` / ``codex-local``). Model aliases + default match the
#: retired per-adapter table (ADR-0020 §codex).
CODEX = Backend(
    name="codex",
    binary="codex",
    funnel_agent="codex",
    doppler_app_prefix="CODEX_REVIEW_APP",
    model_aliases=MappingProxyType(
        {"pro": "gpt-5.5", "flash": "gpt-5.4-mini", "flash_lite": "gpt-5.4-mini"}
    ),
    default_model="gpt-5.5",
)

#: The ``antigravity`` backend — the Antigravity CLI (binary ``agy``). Its funnel
#: agent name is ``agy`` (the ``--backend`` token is ``antigravity``; the CLI binary
#: is ``agy`` — one backend, three surface names, all defined here). A funnel App
#: reviewer (``adr-agy-review[bot]`` / ``agy-local``). The ``pro`` default MUST resolve
#: to a capable, NON-agentic model (a bare ``pro`` silently resolves to Gemini Flash,
#: which goes agentic in ``--print`` mode; ADR-0020 §Decision-per-backend).
ANTIGRAVITY = Backend(
    name="antigravity",
    binary="agy",
    funnel_agent="agy",
    doppler_app_prefix="AGY_REVIEW_APP",
    model_aliases=MappingProxyType(
        {
            "pro": "Gemini 3.1 Pro (High)",
            "flash": "Gemini 3.5 Flash (High)",
            "flash_lite": "Gemini 3.5 Flash (Low)",
        }
    ),
    default_model="pro",
)

#: The closed backend registry, in canonical order (``claude`` first). Wiring a new
#: backend is one entry here; every consumer reads it, never a hand-maintained copy.
REGISTRY: tuple[Backend, ...] = (CLAUDE, CODEX, ANTIGRAVITY)

_BY_NAME: dict[str, Backend] = {b.name: b for b in REGISTRY}
_BY_FUNNEL_AGENT: dict[str, Backend] = {
    b.funnel_agent: b for b in REGISTRY if b.funnel_agent is not None
}
_BY_CHECK_RUN_NAME: dict[str, Backend] = {
    b.check_run_name: b for b in REGISTRY if b.has_funnel_identity
}


def by_name(name: str) -> Backend:
    """The :class:`Backend` whose canonical name (== spawn ``--backend`` token) is
    ``name``, or raise :class:`KeyError`."""
    return _BY_NAME[name]


def by_funnel_agent(agent: str) -> Backend:
    """The :class:`Backend` whose review-funnel agent name is ``agent``
    (``codex`` / ``agy``), or raise :class:`KeyError`."""
    return _BY_FUNNEL_AGENT[agent]


def by_check_run_name(name: str) -> Backend:
    """The :class:`Backend` whose funnel check-run reviewer name is ``name``
    (``codex-local`` / ``agy-local``), or raise :class:`KeyError`.

    The REGISTRY-LOOKUP inverse of :pyattr:`Backend.check_run_name` — the funnel
    path resolves a reviewer name back to its backend HERE, never by slicing a
    ``-local`` suffix off a string (COR02-WS03)."""
    return _BY_CHECK_RUN_NAME[name]


def funnel_backends() -> tuple[Backend, ...]:
    """The backends that ARE review-funnel App reviewers (codex / agy), in order."""
    return tuple(b for b in REGISTRY if b.has_funnel_identity)
