"""``harness/eval/variant`` — content-hash the role prompt that produced a run.

Identical prompts hash identically, so runs pool; a changed prompt separates them.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ... import config
from ..prompts import load_role_defs, render
from ..role import Role

logger = logging.getLogger("shipit.hook")

#: Separates two runs of the SAME prompt into distinct experiment arms.
VARIANT_LABEL_ENV = "SHIPIT_EVAL_VARIANT_LABEL"


@dataclass(frozen=True)
class Variant:
    """``content_hash`` is the ``sha256:`` key of the generated role prompt that ran."""

    content_hash: str
    label: str | None = None

    def as_record(self) -> dict[str, Any]:
        return {"content_hash": self.content_hash, "label": self.label}


def variant_of(prompt_text: str, *, label: str | None = None) -> Variant:
    return Variant(
        content_hash=config.content_hash(prompt_text.encode("utf-8")),
        label=label,
    )


def label_from_env(env: Mapping[str, str] | None = None) -> str | None:
    environ = os.environ if env is None else env
    return (environ.get(VARIANT_LABEL_ENV) or "").strip() or None


def role_of_name(name: str | None) -> Role:
    """The role a raw NAME resolves to: blank ⇒ coordinator, unknown ⇒ implementer.

    An unknown name is still a WORKER, so it must never pool under the
    coordinator's prompt hash.
    """
    agent_type = str(name or "").strip().lower()
    if not agent_type:
        return Role.COORDINATOR
    for role in Role:
        if role.value == agent_type:
            return role
    logger.debug(
        "unrecognized role name %r — attributing to a non-coordinator worker",
        agent_type,
    )
    return Role.IMPLEMENTER


def role_of_meta(meta: Mapping[str, Any] | None) -> Role:
    return role_of_name((meta or {}).get("agentType"))


def role_prompt_text(role: Role) -> str:
    return render(load_role_defs()).role_prompts[role]


def resolve_variant(
    meta: Mapping[str, Any] | None,
    env: Mapping[str, str] | None = None,
) -> Variant:
    prompt = role_prompt_text(role_of_meta(meta))
    return variant_of(prompt, label=label_from_env(env))
