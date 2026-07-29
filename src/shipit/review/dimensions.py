"""The closed Dimension-pass registry, its default set, and the one name resolver.

See docs/adr/0045-dimension-fanout-single-calibrator.md.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Dimension:
    """One reviewable dimension: its config token, human label, and prompt focus slice."""

    name: str
    title: str
    focus: str


#: The concern-scoped decomposition — the fan-out's default set. Each ``focus``
#: tells its pass to ignore everything outside its dimension; overlap between
#: passes is fine, but defects belonging to no bucket can fall between them.
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

#: The experiment-only severity-tier set, never part of the default: only an
#: explicit ``dimensions`` list selects these. The focus texts are experiment
#: material — editing one changes the variant hash and orphans banked lab points.
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

#: The closed registry — everything a ``dimensions`` config list may name.
DIMENSIONS: tuple[Dimension, ...] = _CONCERN_DIMENSIONS + _SEVERITY_TIER_DIMENSIONS

#: The set an explicitly-selected fan-out runs when no list is named.
DEFAULT_DIMENSION_NAMES: tuple[str, ...] = tuple(d.name for d in _CONCERN_DIMENSIONS)

_BY_NAME: dict[str, Dimension] = {d.name: d for d in DIMENSIONS}


def known_dimension_names() -> tuple[str, ...]:
    """Every registered dimension's config token, in registry order."""
    return tuple(d.name for d in DIMENSIONS)


def by_name(name: str) -> Dimension:
    """The :class:`Dimension` for config token ``name``, or raise ``KeyError``."""
    return _BY_NAME[name]


def resolve_dimensions(names: Sequence[str] | None) -> tuple[Dimension, ...]:
    """The dimension set for ``names``; ``None``/empty means the default set."""
    if not names:
        return _CONCERN_DIMENSIONS
    return tuple(by_name(name) for name in names)


def fanout_variant_text(
    instructions_text: str,
    names: Sequence[str] | None,
    overrides: Mapping[str, Mapping[str, str]] | None = None,
) -> str:
    """The text a fan-out round's instructions-variant hash covers; pure.

    Dimensions sort by name because passes run in parallel, so a reordered
    config list is the same experiment. Every folded field is JSON-encoded so no
    value can forge a line boundary and collide two distinct sets into one hash.
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
