"""Variant resolver — content-hash the role prompt that produced a run (module #4).

Every **eval record** is stamped with a **variant** so results attribute to the
exact harness version that ran (docs/prd/har02-run-eval.md, module #4; CONTEXT.md
"variant"): a *derived content-hash* of the generated **role prompt** that drove
the run — the **content-key** / pristine-hash idea applied to prompts — plus an
optional explicit A/B **label** for deliberate experiments. Identical prompts hash
identically (runs pool across commits); a changed prompt hashes differently (runs
separate within a commit).

Pure core / thin boundary (the eval-wire shape):

  - :func:`variant_of` is the PURE core — content-hash a prompt string (+ carry a
    label), REUSING :func:`shipit.config.content_hash` (the same ``sha256:`` key
    ``shipit install`` pristine-hashes the managed set with), so there is one
    hashing scheme, not a parallel one. Stability/poolability is unit-testable on
    plain strings, no I/O.
  - :func:`role_of_meta` is pure too — map a run's ``.meta.json`` to the **role**
    whose prompt ran (an absent/unknown ``agentType`` ⇒ the ``coordinator``, the
    same default :mod:`shipit.harness.eval.record` uses).
  - :func:`role_prompt_text` / :func:`resolve_variant` are the BOUNDARY — read the
    bundled role-prompt fragments (:mod:`shipit.harness.prompts`) and the
    environment, then call the pure core.
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

#: Optional explicit A/B label for the running variant, read at the hook boundary.
#: Set it to separate two runs of the SAME prompt into distinct experiment arms.
VARIANT_LABEL_ENV = "SHIPIT_EVAL_VARIANT_LABEL"


@dataclass(frozen=True)
class Variant:
    """A run's variant attribution: the role-prompt content-hash + optional label.

    ``content_hash`` is the ``sha256:`` key of the generated role prompt that ran;
    ``label`` is an optional explicit A/B tag (``None`` for ordinary runs).
    """

    content_hash: str
    label: str | None = None

    def as_record(self) -> dict[str, Any]:
        """The JSON-serializable dict stamped into the eval record's ``eval.variant``."""
        return {"content_hash": self.content_hash, "label": self.label}


def variant_of(prompt_text: str, *, label: str | None = None) -> Variant:
    """The variant of a run from the role prompt that drove it. PURE.

    Content-hashes ``prompt_text`` with :func:`shipit.config.content_hash` — the
    same pristine-hash machinery the install reconciler keys the managed set on —
    so identical prompts yield identical variants (runs pool) and any change yields
    a different one (runs separate). ``label`` rides through verbatim.
    """
    return Variant(
        content_hash=config.content_hash(prompt_text.encode("utf-8")),
        label=label,
    )


def role_of_meta(meta: Mapping[str, Any] | None) -> Role:
    """The **role** whose prompt ran, from a run's ``.meta.json``. PURE.

    Mirrors the hook-role resolver (:func:`shipit.harness.role.resolve_role`) so
    the two agree: only an **absent/blank** ``agentType`` is the ``coordinator``
    (the coordinator run has no meta and no agent-def prompt of its own kind — the
    same default :mod:`shipit.harness.eval.record` stamps for an absent meta). A
    subagent meta whose ``agentType`` *drifted* (a new/renamed role, corruption,
    casing) is still a worker, NOT the coordinator, so it resolves to the generic
    worker role (``implementer``) rather than pooling under the coordinator's
    prompt hash; the mismatch is logged.
    """
    agent_type = str((meta or {}).get("agentType") or "").strip().lower()
    if not agent_type:
        return Role.COORDINATOR
    for role in Role:
        if role.value == agent_type:
            return role
    logger.debug(
        "unrecognized agentType %r — attributing to a non-coordinator worker",
        agent_type,
    )
    return Role.IMPLEMENTER


def role_prompt_text(role: Role) -> str:
    """The generated role-prompt text for ``role``. BOUNDARY (reads the fragments).

    Composes the bundled lex-fragment mirrors exactly as the installed agent-defs /
    coordinator slice are generated (:mod:`shipit.harness.prompts`), so the hashed
    bytes are the prompt that actually drove the run.
    """
    return render(load_role_defs()).role_prompts[role]


def resolve_variant(
    meta: Mapping[str, Any] | None,
    env: Mapping[str, str] | None = None,
) -> Variant:
    """Resolve a run's variant from its meta + the environment. BOUNDARY.

    Maps the meta to its role, reads that role's generated prompt, and content-
    hashes it, carrying any :data:`VARIANT_LABEL_ENV` A/B label. The label is
    normalized like the other hook-boundary env reads (e.g.
    :func:`shipit.verbs.hook.pretooluse._break_glass_armed`): surrounding
    whitespace is stripped and an empty string is treated as ``None``, so a label
    accidentally padded by shell quoting or CI templating does not split an arm
    from itself. ``env`` is injectable for tests (defaults to ``os.environ``).
    """
    environ = os.environ if env is None else env
    label = (environ.get(VARIANT_LABEL_ENV) or "").strip() or None
    prompt = role_prompt_text(role_of_meta(meta))
    return variant_of(prompt, label=label)
