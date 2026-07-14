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
    fails creation loudly instead of writing a broken file. A stray ``{{`` that
    is not a well-formed placeholder is left verbatim and would surface in the
    output — creation's own tests over the packaged templates are the guard
    against that, exactly as they are for any authored source.
    """

    def _resolve(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in context:
            raise CreationError(
                f"template referenced undefined variable {key!r}; "
                f"available: {sorted(context)}"
            )
        return context[key]

    return _PLACEHOLDER.sub(_resolve, template)
