"""Strict text templating for creation's authored text (ADR-0058).

Creation profiles own TEXT — Markdown, Rust source, licence text, CI YAML —
and substitute into it here. Structured data (TOML) is NEVER templated; it is
built as values and serialized by :mod:`.tomlio`. This renderer is deliberately
tiny and STRICT (the StrictUndefined contract ADR-0058 names): a ``{{ name }}``
placeholder whose key is absent from the context, or any placeholder left
unrendered, raises rather than emitting an empty string or a literal brace pair
into a generated file. shipit ships no Jinja2 in its zero-transitive-dependency
runtime (``pyproject.toml``), and the tracer's substitutions are single
identifiers, so a full template engine would be dead weight; this keeps the
Jinja-flavored ``{{ }}`` surface and the fail-loud-on-undefined guarantee that
matter, and nothing else.
"""

from __future__ import annotations

import re

from .errors import CreationError

#: A ``{{ identifier }}`` placeholder, tolerant of surrounding whitespace.
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def render_text(template: str, context: dict[str, str]) -> str:
    """Substitute ``{{ key }}`` placeholders from ``context``; fail on undefined.

    Every placeholder must resolve to a context key (StrictUndefined): an
    unknown key raises :class:`CreationError` naming it, so a template typo
    fails creation loudly instead of writing a broken file. The renderer also
    fails closed on any ``{{``/``}}`` in the TEMPLATE that the identifier
    pattern rejects (e.g. ``{{ cli-pkg }}``, whose hyphen is not an identifier
    char) — such a malformed placeholder would otherwise ship a literal brace
    pair into a generated file, contradicting this module's no-unrendered-braces
    contract. The malformed-brace scan runs over the template with its valid
    placeholders stripped out, NOT over the rendered output: a substituted
    context value may legitimately contain ``{{``/``}}`` (e.g. a code snippet),
    and scanning the output would wrongly reject those value contents.
    """

    def _resolve(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in context:
            raise CreationError(
                f"template referenced undefined variable {key!r}; "
                f"available: {sorted(context)}"
            )
        return context[key]

    # Scan the template (minus its valid placeholders) for malformed brace pairs
    # before substituting, so we catch template defects without restricting what
    # a context value may contain.
    stripped = _PLACEHOLDER.sub("", template)
    leftover = re.search(r"\{\{|\}\}", stripped)
    if leftover is not None:
        near = stripped[leftover.start() : leftover.start() + 40]
        raise CreationError(
            f"template left an unrendered brace pair near {near!r}; "
            "placeholder keys must be identifiers "
            "([A-Za-z_][A-Za-z0-9_]*)"
        )
    return _PLACEHOLDER.sub(_resolve, template)
