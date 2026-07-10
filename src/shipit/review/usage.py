"""usage — token-usage capture from CLI backend output (RVW03-WS04, #667).

The measurement-integrity seam: token usage is captured at LAUNCH-RESULT level,
from what each backend's CLI actually reports in its own output streams — with
NO dependence on the transcript/``run_id`` join (the funnel's run ids are minted
uuids that never match a transcript stem, so that join is broken by
construction; the eval report no longer attempts it).

What each CLI reports is a PROBED fact (2026-07-10, the same do-not-guess
convention as the adapter argv):

- ``claude`` ``-p --output-format json`` (2.1.206): the result envelope carries
  a structured ``usage`` block (``input_tokens`` / ``output_tokens`` /
  ``cache_read_input_tokens`` / ``cache_creation_input_tokens``) →
  :func:`from_claude_envelope`.
- ``codex exec`` (0.139.0): stdout is the bare final message; usage is reported
  on **stderr** as a human log line — ``tokens used`` followed by a
  comma-grouped figure (``tokens used\\n11,943``) → :func:`from_codex_stderr`.
- ``agy --print`` (1.1.1): the answer text only — NOTHING on stderr, no usage
  flag in its help. agy usage is therefore EXPLICITLY UNKNOWN
  (:data:`UNREPORTED`), never a fabricated number.

The honesty rule the record shape carries: a backend whose CLI reports usage
produces non-null numbers; one that does not records explicitly-unknown
(``total_tokens: None`` + ``source: "unreported"``) — the eval report
distinguishes the two (a latency-only cell is MARKED as such, never rendered as
zero cost).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: ``source`` tokens — WHERE a usage figure came from, so a record consumer can
#: tell a measured number from an explicitly-unknown one without sniffing nulls.
SOURCE_CLAUDE_ENVELOPE = "claude-envelope"
SOURCE_CODEX_STDERR = "codex-stderr"
SOURCE_UNREPORTED = "unreported"

#: codex 0.139 reports usage on stderr as a human log line: ``tokens used``
#: (optionally with a colon) followed — possibly on the NEXT line — by a
#: comma-grouped figure. Probed 2026-07-10; the match is tolerant of both the
#: same-line (``tokens used: 11,943``) and next-line (``tokens used\n11,943``)
#: renderings so a minor CLI formatting change does not silently zero the data.
_CODEX_TOKENS_LINE = re.compile(r"tokens used:?\s*\n?\s*([\d,]+)", re.IGNORECASE)


@dataclass(frozen=True)
class TokenUsage:
    """One launch's token cost, as its CLI reported it — or explicitly unknown.

    ``total_tokens`` is ``None`` ONLY for :data:`SOURCE_UNREPORTED` (the CLI
    reports no usage) — never a silent parse miss dressed as a measurement.
    ``input_tokens`` / ``output_tokens`` are carried when the source is
    structured enough to split them (the claude envelope); a coarse source
    (codex's single stderr figure) leaves them ``None`` with a real total.
    """

    total_tokens: int | None
    source: str = SOURCE_UNREPORTED
    input_tokens: int | None = None
    output_tokens: int | None = None

    @property
    def reported(self) -> bool:
        """True when this is a real measurement (a non-null total)."""
        return self.total_tokens is not None

    def as_record(self) -> dict[str, Any]:
        """The ``round.runs[].usage`` record shape (RVW03-WS04)."""
        return {
            "total_tokens": self.total_tokens,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "source": self.source,
        }


#: The explicitly-unknown usage: the backend's CLI reports none (agy), or the
#: reporting stream was absent. Records as ``total_tokens: None`` +
#: ``source: "unreported"`` — the honest "we do not know", distinct from zero.
UNREPORTED = TokenUsage(total_tokens=None)


def from_claude_envelope(envelope: Mapping[str, Any]) -> TokenUsage:
    """Usage from a ``claude -p --output-format json`` result envelope.

    The envelope's ``usage`` block (probed on 2.1.206) carries
    ``input_tokens`` / ``output_tokens`` plus the cache counters; the total is
    ``input + output + cache_read + cache_creation`` — every token the run
    consumed, the same fold the transcript extractor's convention uses for the
    prompt side. A malformed/absent block degrades to :data:`UNREPORTED`
    (never a guessed number). A counter that is PRESENT but corrupt (a bool, a
    negative, a non-int) is a shape-drift signal for the whole block, not an
    absent field: it poisons the measurement to :data:`UNREPORTED` rather than
    folding in as a partial (undercounted) total — an absent counter is simply
    left out of the sum.
    """
    usage = envelope.get("usage")
    if not isinstance(usage, Mapping):
        return UNREPORTED
    counts: dict[str, int] = {}
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ):
        if key not in usage:
            continue  # absent → not summed (legitimately omitted, e.g. no cache)
        value = _int_or_none(usage[key])
        if value is None:
            return UNREPORTED  # present but corrupt — the block is untrustworthy
        counts[key] = value
    input_tokens = counts.get("input_tokens")
    output_tokens = counts.get("output_tokens")
    if input_tokens is None and output_tokens is None:
        return UNREPORTED
    total = sum(counts.values())
    return TokenUsage(
        total_tokens=total,
        source=SOURCE_CLAUDE_ENVELOPE,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def from_codex_stderr(stderr: str) -> TokenUsage:
    """Usage from ``codex exec``'s stderr log (probed on codex 0.139.0).

    codex prints the run's total as a human log line on STDERR — ``tokens
    used`` then a comma-grouped figure (see :data:`_CODEX_TOKENS_LINE`);
    stdout carries only the final message, so this is the ONE stream the
    figure lives in. No match degrades to :data:`UNREPORTED` — a CLI
    formatting drift reads as "unknown", never as zero. A match whose digits
    do not form a valid int (a commas-only capture like ``,,,`` that strips to
    empty, or a figure past CPython's integer-string-conversion limit) degrades
    the same way rather than raising out of this untrusted-stderr parse.
    """
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
