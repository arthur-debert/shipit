"""dimensions — the closed **Dimension pass** registry (RVW02-WS04, ADR-0045).

A **Dimension pass** is one scoped finder inside a local-agent reviewer's
round-1 review run: a single pass whose prompt is narrowed to one dimension,
run on that reviewer's Backend against the shared read-only Tree (CONTEXT.md
"Dimension pass"). Passes run in parallel; their union feeds the **Calibrator**
(:mod:`shipit.review.calibrator`). This module owns the dimension vocabulary —
the closed registry of known dimensions, the shipped default set, and the ONE
resolver config names route through — so the Roster loader and the fan-out
orchestrator agree on exactly which dimensions exist.

Dimensions scope the SEARCH; on the shipped default path severity is assigned
at calibration (ADR-0045). ADR-0051 narrows ADR-0045's severity-scoped-finder
rejection to that shipped default: the registry also carries an
EXPERIMENT-ONLY severity-tier set (``sev-critical-high`` / ``sev-medium`` /
``sev-low``) for Review-Lab measurement, selectable solely via an explicit
``dimensions`` list (a Lab cell or a Roster override) and never part of the
shipped default. The registry is a closed tuple
(ADR-0021 closed-registry-over-hierarchy): adding a dimension is one entry,
referenced everywhere else (the Roster `dimensions` option validates against
:func:`known_dimension_names`; the pass prompt reads :attr:`Dimension.focus`).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Dimension:
    """One reviewable dimension: its config token and the prompt slice that
    narrows a pass to it.

    ``name`` is the canonical config token (the value a Roster ``dimensions``
    list carries); ``title`` the short human label; ``focus`` the prompt slice
    the pass task embeds — what the narrowed pass hunts for, stated as review
    marching orders (:func:`shipit.review.prompt.build_reviewer_task`).
    """

    name: str
    title: str
    focus: str


#: The ADR-0045 concern-scoped decomposition — the SHIPPED DEFAULT set. Each
#: ``focus`` deliberately tells the pass to IGNORE everything outside its
#: dimension: narrowed attention is the recall mechanism (single-pass recall
#: <50% at every tier — the evidence behind the fan-out), and overlap between
#: passes is fine (the Calibrator dedups), so a pass never self-budgets.
_CONCERN_DIMENSIONS: tuple[Dimension, ...] = (
    Dimension(
        name="correctness",
        title="Correctness",
        focus=(
            "logic errors, broken edge cases, off-by-one and boundary mistakes, "
            "wrong conditionals, error-handling gaps, crashes, data loss, and "
            "behavior that contradicts the function's documented contract. Trace "
            "the changed code paths concretely — what inputs/state make them "
            "misbehave? Ignore style, naming, tests, and security posture: other "
            "passes own those."
        ),
    ),
    Dimension(
        name="cross-file-invariants",
        title="Cross-file invariants",
        focus=(
            "invariants that span files: callers of changed functions that were "
            "not updated, contracts between modules the diff silently breaks, "
            "duplicated constants/tables drifting apart, docstrings or comments "
            "elsewhere now describing stale behavior, and interfaces whose other "
            "implementations were missed. READ BEYOND THE DIFF: follow the "
            "changed symbols to their definitions and callers in this checkout. "
            "Ignore single-file logic bugs, style, tests, and security: other "
            "passes own those."
        ),
    ),
    Dimension(
        name="security-robustness",
        title="Security / robustness",
        focus=(
            "security holes and robustness failures: injection (shell, SQL, "
            "markup), path traversal, secrets or tokens leaking into logs/output, "
            "unvalidated untrusted input, resource leaks, unbounded "
            "growth/recursion, race conditions, and failure modes that corrupt "
            "state instead of failing loud. Ignore style, naming, tests, and "
            "ordinary logic bugs: other passes own those."
        ),
    ),
    Dimension(
        name="test-quality",
        title="Test quality",
        focus=(
            "the tests: changed behavior with no test, tests that assert "
            "incidental internals instead of externally visible behavior, tests "
            "that can never fail (tautologies, over-mocking the unit under "
            "test), missing failure-path coverage, and fixtures that silently "
            "diverge from the real wiring. Ignore production-code style and "
            "security posture: other passes own those."
        ),
    ),
)

#: The EXPERIMENT-ONLY severity-tier set (ADR-0051, amending ADR-0045). Passes
#: scoped by severity tier instead of concern — the literature's strongest
#: configuration — so the Review Lab's `fanout-sevtiers` cell can measure the
#: tier fan-out against the shipped concern-scoped one. NEVER part of the
#: shipped default: only an explicit ``dimensions`` list (a Lab cell or a
#: Roster override) selects these. The focus texts are experiment material —
#: editing them changes the recorded instructions-variant hash and orphans
#: banked lab points, so a wording change means a deliberate re-run.
_SEVERITY_TIER_DIMENSIONS: tuple[Dimension, ...] = (
    Dimension(
        name="sev-critical-high",
        title="Severity: critical/high",
        focus=(
            "merge-blocking defects ONLY: wrong results or outputs, data loss "
            "or corruption, destructive or irreversible side effects (deleting "
            "or overwriting files, dropping records), crashes and panics on "
            "reachable paths, security holes, and silent failure modes that "
            "ship bad state. Trace the concrete inputs/state that reach the "
            "failure. Ignore design taste, missing tests, style, naming, and "
            "docs: other passes own those. Emit only findings you would block "
            "a merge over."
        ),
    ),
    Dimension(
        name="sev-medium",
        title="Severity: medium",
        focus=(
            "worth-fixing-but-not-blocking defects: design flaws and wrong "
            "abstractions, missing robustness (unvalidated input, unbounded "
            "growth, resource leaks, race conditions with limited blast "
            "radius), error handling that degrades quietly rather than failing "
            "loud, and weak or missing tests for the changed behavior. Ignore "
            "merge-blocking corruption/data-loss defects and pure style/docs "
            "polish: other passes own those."
        ),
    ),
    Dimension(
        name="sev-low",
        title="Severity: low",
        focus=(
            "polish: style and naming, formatting, comment and docstring "
            "drift, dead cross-references, typos, and documentation gaps with "
            "no behavioral impact. Ignore anything with correctness, "
            "robustness, security, or test implications: other passes own "
            "those."
        ),
    ),
)

#: The closed dimension registry — everything a ``dimensions`` config list may
#: name: the ADR-0045 concern-scoped four plus the ADR-0051 experiment-only
#: severity-tier three.
DIMENSIONS: tuple[Dimension, ...] = _CONCERN_DIMENSIONS + _SEVERITY_TIER_DIMENSIONS

#: The shipped default dimension set — exactly the ADR-0045 concern-scoped
#: decomposition, registry order; the severity-tier entries are experiment-only
#: and excluded (ADR-0051). A per-reviewer Roster ``dimensions`` option
#: narrows, reorders, or (explicitly) swaps it.
DEFAULT_DIMENSION_NAMES: tuple[str, ...] = tuple(d.name for d in _CONCERN_DIMENSIONS)

_BY_NAME: dict[str, Dimension] = {d.name: d for d in DIMENSIONS}


def known_dimension_names() -> tuple[str, ...]:
    """Every registered dimension's config token, registry order (default set
    first, then the experiment-only tiers) — what the Roster loader validates a
    ``dimensions`` option against (unknown names fail loud there, roster prior
    art). A superset of :data:`DEFAULT_DIMENSION_NAMES`: the severity-tier
    tokens validate but never run unless explicitly listed (ADR-0051)."""
    return tuple(d.name for d in DIMENSIONS)


def by_name(name: str) -> Dimension:
    """The :class:`Dimension` for config token ``name``, or raise ``KeyError``.

    Exact-token lookup (tokens are canonical lowercase); the config boundary
    (:mod:`shipit.prstate.reviewers_config`) has already rejected unknown or
    mis-cased names loud, so a ``KeyError`` here is a programming error, never
    a user-facing path.
    """
    return _BY_NAME[name]


def resolve_dimensions(names: Sequence[str] | None) -> tuple[Dimension, ...]:
    """The :class:`Dimension` set for ``names`` — ``None``/empty means the
    SHIPPED DEFAULT set (the concern-scoped four, never the experiment-only
    tiers; ADR-0051), else the named subset in the given order. Raises
    ``KeyError`` on an unknown name (the config boundary validates first; see
    :func:`by_name`)."""
    if not names:
        return _CONCERN_DIMENSIONS
    return tuple(by_name(name) for name in names)


def fanout_variant_text(
    instructions_text: str,
    names: Sequence[str] | None,
    overrides: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    """The text a fan-out round's instructions-variant hash covers. PURE.

    The shared instructions file is only PART of a fan-out round's prompt
    material: each pass embeds its dimension's title/focus slice
    (:func:`shipit.review.prompt.build_reviewer_task`), a ``dimensions`` list
    selects the set, and a lab cell may override a pass's Invocation. None of
    that lives in the instructions file, so hashing the file alone lets a
    focus-text edit — exactly the experiment material ADR-0051 froze — reuse
    results recorded under the old prompt (#713). This helper folds the
    RESOLVED dimension set (name, title, focus, and any per-dimension
    invocation overrides) into the hashed text, canonically: dimensions sorted
    by name (passes run in parallel, so a reordered config list is the same
    experiment and pools with itself) and override fields sorted within each
    block. Every consumer of the fan-out instructions-variant hash — the lab
    run key (:func:`shipit.review.cell.instructions_variant_text`) and the
    round record's ``round.variant``
    (:func:`shipit.review.roundrecord.record_round`) — derives it from THIS
    text, so they can never disagree.

    ``names`` follows :func:`resolve_dimensions` (``None``/empty = the shipped
    default set; unknown names raise ``KeyError`` there). ``overrides`` is the
    per-dimension Invocation table (``{dimension name: {"model"/"timeout":
    …}}``); an entry naming a dimension outside the set is ignored here — the
    config boundaries already reject it loudly (cell parse, fan-out preflight).

    Every folded field (name, title, focus, and each override key/value) is
    JSON-encoded before it lands on its line, so the canonicalization is
    INJECTIVE: a JSON string literal never contains a bare newline, so no field
    value can forge a line boundary and collide two distinct dimension sets into
    one hash (#713 — a focus text is editable prose and an override value is
    caller-supplied; a raw join would let ``{"model": "x\\noverride.timeout: y"}``
    canonicalize identically to ``{"model": "x", "timeout": "y"}``).
    """
    lines = [instructions_text, "", "--- dimension set (variant material) ---"]
    for dim in sorted(resolve_dimensions(names), key=lambda d: d.name):
        lines.append(f"[dimension: {json.dumps(dim.name, ensure_ascii=False)}]")
        lines.append(f"title: {json.dumps(dim.title, ensure_ascii=False)}")
        lines.append(f"focus: {json.dumps(dim.focus, ensure_ascii=False)}")
        override = (overrides or {}).get(dim.name) or {}
        lines.extend(
            f"override.{json.dumps(key, ensure_ascii=False)}: "
            f"{json.dumps(override[key], ensure_ascii=False)}"
            for key in sorted(override)
        )
    return "\n".join(lines)
