"""The one format-aware TOML renderer for creation's structured data.

ADR-0058 draws a hard line: creation profiles template TEXT (Markdown, source,
scripts) but never template structured data — TOML, JSON, YAML documents are
built as structured Python values and serialized HERE, by one renderer, so
escaping, quoting, and ordering are never an accidental data model spliced out
of string fragments. Cargo manifests, the pixi manifest seed, and ``.shipit.toml``
declarations are dicts in the plan and become text only through :func:`dumps`.

The serializer covers exactly the TOML shapes creation emits — scalars, string
arrays, tables, nested tables, arrays-of-tables, inline tables (:class:`Inline`),
and Cargo's dotted workspace-inheritance keys (a key literally containing ``.``
renders verbatim, e.g. ``version.workspace = true``). It is deliberately not a
general TOML library; a shape it does not model raises rather than emitting
ambiguous text.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Inline:
    """Force a mapping to render as a TOML inline table (``{ k = v, ... }``).

    Used where Cargo wants an inline table value rather than a ``[table]``
    section — e.g. a path dependency ``libhello = { path = "../libhello" }``.
    """

    data: Mapping[str, object]


def _render_scalar(value: object) -> str:
    """Render a leaf value (string, bool, int/float, string array, inline table)."""
    if isinstance(value, Inline):
        inner = ", ".join(
            f"{_render_key(k)} = {_render_scalar(v)}" for k, v in value.data.items()
        )
        return "{ " + inner + " }" if inner else "{}"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, Sequence):
        return "[" + ", ".join(_render_scalar(v) for v in value) + "]"
    raise TypeError(f"cannot serialize {type(value).__name__} to TOML: {value!r}")


def _render_key(key: str) -> str:
    """Render a key: bare when it is a plain/dotted identifier, else quoted.

    A key literally containing ``.`` is a Cargo dotted key (``version.workspace``)
    and renders verbatim; a bare-identifier key renders unquoted; anything else
    is quoted so an exotic key can never break the document.
    """
    if all(part and _is_bare(part) for part in key.split(".")):
        return key
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _is_bare(part: str) -> bool:
    return all(c.isalnum() or c in "-_" for c in part)


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
    """Append ``table``'s body then its sub-tables/arrays under ``prefix``."""
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
    """Serialize a document mapping to TOML text (trailing newline included).

    Top-level scalar keys render first, then each table as a ``[header]``
    section — the ordering TOML requires (bare keys precede subtable headers
    within their owning table).
    """
    lines: list[str] = []
    _render_table("", doc, lines)
    return "\n".join(lines) + "\n"
