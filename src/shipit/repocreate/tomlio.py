"""``repocreate/tomlio`` — the one TOML renderer for creation's structured data.

Not a general TOML library: an unmodeled shape raises rather than guessing.
See docs/adr/0058-templates-render-text-not-structured-data.md.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Inline:
    """Force a mapping to render as a TOML inline table (``{ k = v, ... }``)."""

    data: Mapping[str, object]


def _quote(text: str) -> str:
    """Render ``text`` as a TOML basic string, fully escaped.

    JSON escape syntax covers every case creation emits; only ``U+007F``, which
    TOML requires escaped and JSON does not, is closed by hand.
    """
    return json.dumps(text, ensure_ascii=False).replace("\x7f", "\\u007f")


def _render_scalar(value: object) -> str:
    if isinstance(value, Inline):
        inner = ", ".join(
            f"{_render_key(k)} = {_render_scalar(v)}" for k, v in value.data.items()
        )
        return "{ " + inner + " }" if inner else "{}"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, Sequence):
        return "[" + ", ".join(_render_scalar(v) for v in value) + "]"
    raise TypeError(f"cannot serialize {type(value).__name__} to TOML: {value!r}")


def _render_key(key: str) -> str:
    """Render a key bare when it is a plain or dotted identifier, else quoted."""
    if all(part and _is_bare(part) for part in key.split(".")):
        return key
    return _quote(key)


def _is_bare(part: str) -> bool:
    # TOML bare keys are ASCII-only, and `str.isalnum()` alone accepts non-ASCII
    # letters — so require ASCII before alnum.
    return all((c.isascii() and c.isalnum()) or c in "-_" for c in part)


def _is_table(value: object) -> bool:
    return isinstance(value, Mapping) and not isinstance(value, Inline)


def _is_array_of_tables(value: object) -> bool:
    return (
        isinstance(value, Sequence)
        and not isinstance(value, str)
        and len(value) > 0
        and all(_is_table(v) for v in value)
    )


def _render_table(prefix: str, table: Mapping[str, object], lines: list[str]) -> None:
    scalars = {
        k: v
        for k, v in table.items()
        if not _is_table(v) and not _is_array_of_tables(v)
    }
    for key, value in scalars.items():
        lines.append(f"{_render_key(key)} = {_render_scalar(value)}")
    for key, value in table.items():
        header = f"{prefix}.{_render_key(key)}" if prefix else _render_key(key)
        if _is_table(value):
            if lines and lines[-1] != "":
                lines.append("")
            lines.append(f"[{header}]")
            _render_table(header, value, lines)
        elif _is_array_of_tables(value):
            for element in value:
                if lines and lines[-1] != "":
                    lines.append("")
                lines.append(f"[[{header}]]")
                _render_table(header, element, lines)


def dumps(doc: Mapping[str, object]) -> str:
    """Serialize a document mapping to TOML text, scalars before ``[header]`` sections."""
    lines: list[str] = []
    _render_table("", doc, lines)
    return "\n".join(lines) + "\n"
