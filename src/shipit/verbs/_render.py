"""The render seam (ADR-0030) — the ONE place a typed result becomes terminal output.

One of the four seam pieces. Domain functions return frozen result values and
never print; verbs render via pure ``format_*(result) -> str`` helpers and
this module's shared :func:`emit`. The convention:

- **Text** — a per-verb pure function ``format_<verb>(result) -> str`` (no
  trailing newline; :func:`emit` owns the terminal write), unit-testable as a
  plain string function.
- **JSON** — serialized here from the result's own ``to_dict()``, so the
  ``--json`` surface is always the typed result's declared field set, indented
  the one house way.

The exit code is NOT this seam's job: it derives from the result in the
verb's ``run()``, with runtime failures mapped by :mod:`._errors`.
"""

from __future__ import annotations

import json
from typing import Any, Callable


def emit(
    result: Any, format_text: Callable[[Any], str], *, as_json: bool = False
) -> None:
    """Render ``result`` to stdout: ``to_dict()`` JSON with ``as_json``, else pure text.

    ``format_text`` is the verb's pure renderer — called only on the text
    path, so a ``--json`` consumer never pays (or depends on) text formatting.
    """
    if as_json:
        print(json.dumps(result.to_dict(), indent=2))
        return
    print(format_text(result))
