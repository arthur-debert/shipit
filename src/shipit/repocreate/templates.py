"""``repocreate/templates`` — strict ``{{ name }}`` substitution for authored text.

See docs/adr/0058-creation-templating.md.
"""

from __future__ import annotations

import re

from .errors import CreationError

#: A ``{{ identifier }}`` placeholder, tolerant of surrounding whitespace.
_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def render_text(template: str, context: dict[str, str]) -> str:
    """Substitute ``{{ key }}`` from ``context``; an undefined key raises."""

    def _resolve(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in context:
            raise CreationError(
                f"template referenced undefined variable {key!r}; "
                f"available: {sorted(context)}"
            )
        return context[key]

    # Scanned over the TEMPLATE, not the output: a substituted value may legitimately
    # contain braces, and scanning the output would reject those.
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
