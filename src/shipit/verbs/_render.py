"""The render seam — the one place a typed result becomes terminal output."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any


def emit(
    result: Any, format_text: Callable[[Any], str], *, as_json: bool = False
) -> None:
    """Render ``result`` to stdout: ``to_dict()`` JSON with ``as_json``, else ``format_text(result)``."""
    if as_json:
        print(json.dumps(result.to_dict(), indent=2))
        return
    print(format_text(result))
