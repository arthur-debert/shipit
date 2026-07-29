"""``agent/backend`` — the one agent-backend identity/alias registry.

See docs/adr/0025-agent-axes.md.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


@dataclass(frozen=True)
class Backend:
    """Identity is the canonical name ALONE; every other field is ``compare=False``."""

    name: str
    binary: str = field(compare=False)
    #: ``None`` for a backend that is not a funnel App reviewer; the funnel-only
    #: aliases derive from it and raise rather than fabricate an identity.
    funnel_agent: str | None = field(default=None, compare=False)
    #: The Doppler ``github/prd`` key PREFIX for this backend's review GitHub App.
    doppler_app_prefix: str | None = field(default=None, compare=False)
    model_aliases: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({}), compare=False
    )
    default_model: str | None = field(default=None, compare=False)

    @property
    def has_funnel_identity(self) -> bool:
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
        return f"adr-{self._require_funnel('app_slug')}-review"

    @property
    def funnel_login(self) -> str:
        return f"{self.app_slug}[bot]"

    @property
    def bot_slug_fragment(self) -> str:
        return f"{self._require_funnel('bot_slug_fragment')}-review"

    @property
    def check_run_name(self) -> str:
        return f"{self._require_funnel('check_run_name')}-local"

    @property
    def doppler_pem_key(self) -> str:
        return f"{self._require_doppler()}_PRIVATE_KEY"

    @property
    def doppler_app_id_key(self) -> str:
        return f"{self._require_doppler()}_ID"

    def _require_doppler(self) -> str:
        if self.doppler_app_prefix is None:
            raise ValueError(
                f"backend {self.name!r} has no review-App Doppler prefix "
                "(it is not a funnel App reviewer)."
            )
        return self.doppler_app_prefix

    def resolve_model(self, model: str | None = None) -> str:
        """``model`` or :pyattr:`default_model` as a verbatim id; a non-alias passes through."""
        chosen = model if model is not None else self.default_model
        if chosen is None:
            raise ValueError(
                f"backend {self.name!r} has no default model; pass one explicitly."
            )
        return self.model_aliases.get(chosen, chosen)


CLAUDE = Backend(name="claude", binary="claude")

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

#: The ``pro`` default MUST resolve to a capable, NON-agentic model: a bare ``pro``
#: resolves to Gemini Flash, which goes agentic in ``--print`` mode.
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

REGISTRY: tuple[Backend, ...] = (CLAUDE, CODEX, ANTIGRAVITY)

_BY_NAME: dict[str, Backend] = {b.name: b for b in REGISTRY}
_BY_FUNNEL_AGENT: dict[str, Backend] = {
    b.funnel_agent: b for b in REGISTRY if b.funnel_agent is not None
}
_BY_CHECK_RUN_NAME: dict[str, Backend] = {
    b.check_run_name: b for b in REGISTRY if b.has_funnel_identity
}


def by_name(name: str) -> Backend:
    return _BY_NAME[name]


def by_funnel_agent(agent: str) -> Backend:
    return _BY_FUNNEL_AGENT[agent]


def by_check_run_name(name: str) -> Backend:
    return _BY_CHECK_RUN_NAME[name]


def funnel_backends() -> tuple[Backend, ...]:
    return tuple(b for b in REGISTRY if b.has_funnel_identity)
