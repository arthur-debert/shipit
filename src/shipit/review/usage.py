"""Token-usage capture from what each backend's CLI reports in its own streams."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: Where a usage figure came from, so a consumer need not sniff nulls.
SOURCE_CLAUDE_ENVELOPE = "claude-envelope"
SOURCE_CODEX_STDERR = "codex-stderr"
SOURCE_UNREPORTED = "unreported"

#: Matches codex's stderr usage line, same-line or next-line.
_CODEX_TOKENS_LINE = re.compile(r"tokens used:?\s*\n?\s*([\d,]+)", re.IGNORECASE)


@dataclass(frozen=True)
class TokenUsage:
    """One launch's token cost; ``total_tokens`` is ``None`` only for :data:`SOURCE_UNREPORTED`."""

    total_tokens: int | None
    source: str = SOURCE_UNREPORTED
    input_tokens: int | None = None
    output_tokens: int | None = None

    @property
    def reported(self) -> bool:
        return self.total_tokens is not None

    def as_record(self) -> dict[str, Any]:
        """The ``round.runs[].usage`` record shape."""
        return {
            "total_tokens": self.total_tokens,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "source": self.source,
        }


#: Explicitly-unknown usage — the honest "we do not know", distinct from zero.
UNREPORTED = TokenUsage(total_tokens=None)


def from_claude_envelope(envelope: Mapping[str, Any]) -> TokenUsage:
    """Usage from a claude result envelope; any shape drift degrades to :data:`UNREPORTED`."""
    usage = envelope.get("usage")
    if not isinstance(usage, Mapping):
        return UNREPORTED
    input_tokens = _int_or_none(usage.get("input_tokens"))
    output_tokens = _int_or_none(usage.get("output_tokens"))
    if input_tokens is None or output_tokens is None:
        return UNREPORTED
    cache_total = 0
    for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        if key not in usage:
            continue
        value = _int_or_none(usage[key])
        if value is None:
            return UNREPORTED
        cache_total += value
    return TokenUsage(
        total_tokens=input_tokens + output_tokens + cache_total,
        source=SOURCE_CLAUDE_ENVELOPE,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def from_codex_stderr(stderr: str | None) -> TokenUsage:
    """Usage from ``codex exec``'s stderr log; anything unparseable is :data:`UNREPORTED`."""
    match = _CODEX_TOKENS_LINE.search(stderr or "")
    if match is None:
        return UNREPORTED
    try:
        total_tokens = int(match.group(1).replace(",", ""))
    except ValueError:
        return UNREPORTED
    return TokenUsage(
        total_tokens=total_tokens,
        source=SOURCE_CODEX_STDERR,
    )


def _int_or_none(value: object) -> int | None:
    """``value`` as a non-negative token count, else ``None`` (bools excluded)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None
