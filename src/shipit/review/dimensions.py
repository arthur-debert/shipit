"""dimensions — the closed **Dimension pass** registry (RVW02-WS04, ADR-0045).

A **Dimension pass** is one scoped finder inside a local-agent reviewer's
round-1 review run: a single pass whose prompt is narrowed to one dimension,
run on that reviewer's Backend against the shared read-only Tree (CONTEXT.md
"Dimension pass"). Passes run in parallel; their union feeds the **Calibrator**
(:mod:`shipit.review.calibrator`). This module owns the dimension vocabulary —
the closed registry of known dimensions, the shipped default set, and the ONE
resolver config names route through — so the Roster loader and the fan-out
orchestrator agree on exactly which dimensions exist.

Dimensions scope the SEARCH; severity is assigned at calibration (ADR-0045).
A severity-scoped finder (a "highs-only pass") is explicitly rejected by the
decision record — do not add one here. The registry is a closed tuple
(ADR-0021 closed-registry-over-hierarchy): adding a dimension is one entry,
referenced everywhere else (the Roster `dimensions` option validates against
:func:`known_dimension_names`; the pass prompt reads :attr:`Dimension.focus`).
"""

from __future__ import annotations

from collections.abc import Sequence
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


#: The closed dimension registry — the ADR-0045 default decomposition. Each
#: ``focus`` deliberately tells the pass to IGNORE everything outside its
#: dimension: narrowed attention is the recall mechanism (single-pass recall
#: <50% at every tier — the evidence behind the fan-out), and overlap between
#: passes is fine (the Calibrator dedups), so a pass never self-budgets.
DIMENSIONS: tuple[Dimension, ...] = (
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

#: The shipped default dimension set — every registered dimension, registry
#: order. A per-reviewer Roster ``dimensions`` option narrows or reorders it.
DEFAULT_DIMENSION_NAMES: tuple[str, ...] = tuple(d.name for d in DIMENSIONS)

_BY_NAME: dict[str, Dimension] = {d.name: d for d in DIMENSIONS}


def known_dimension_names() -> tuple[str, ...]:
    """Every registered dimension's config token, registry order — what the
    Roster loader validates a ``dimensions`` option against (unknown names fail
    loud there, roster prior art)."""
    return DEFAULT_DIMENSION_NAMES


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
    shipped default set (every registered dimension), else the named subset in
    the given order. Raises ``KeyError`` on an unknown name (the config
    boundary validates first; see :func:`by_name`)."""
    if not names:
        return DIMENSIONS
    return tuple(by_name(name) for name in names)
