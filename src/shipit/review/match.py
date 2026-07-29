"""The deterministic same-claim matching primitive; no LLM is ever part of it.

See docs/adr/0048-ground-truth-fixture-deterministic-scorer.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "CLAIM_THRESHOLD",
    "NEAR_MISS_FLOOR",
    "NEAR_MISS_LINE_SLACK",
    "Claim",
    "MatchVerdict",
    "best_overlap",
    "claim_overlap",
    "match_claim",
    "normalize_claim",
    "same_claim",
]


class MatchVerdict(Enum):
    """The outcomes of matching a claim against a label; ``NEAR_MISS`` feeds adjudication."""

    MATCH = "match"
    NEAR_MISS = "near-miss"
    NO_MATCH = "no-match"


#: Overlap coefficient at or above this is a MATCH, given file + line agree.
CLAIM_THRESHOLD = 0.5

#: Below the threshold but at/above this floor makes a NEAR_MISS, not silence.
NEAR_MISS_FLOOR = 0.2

#: How far outside a label's range a claim-passing finding may sit and still
#: near-miss; a MATCH gets zero slack.
NEAR_MISS_LINE_SLACK = 10

#: Tokens carrying no claim identity. Deliberately small and generic.
_STOPWORDS = frozenset(
    """
    a an and are as at be but by for from has have if in into is it its no not
    of on or so that the their then this to was when which will with would
    """.split()
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def normalize_claim(text: str) -> frozenset[str]:
    """A claim's identity as a normalized token set; pure and deterministic."""
    return frozenset(
        token
        for token in _TOKEN_RE.findall(text.lower())
        if len(token) > 1 and token not in _STOPWORDS
    )


def claim_overlap(a: str, b: str) -> float:
    """Overlap coefficient ``|A∩B| / min(|A|,|B|)`` of two claims' token sets, 0.0–1.0."""
    ta, tb = normalize_claim(a), normalize_claim(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def best_overlap(text: str, texts: tuple[str, ...] | list[str]) -> float:
    """The best :func:`claim_overlap` of ``text`` against any of ``texts``."""
    return max((claim_overlap(text, other) for other in texts), default=0.0)


@dataclass(frozen=True)
class Claim:
    """One located claim: a repo-relative ``file``, a ``line`` (``None`` = file-scoped), a ``text``."""

    file: str
    line: int | None
    text: str


def _in_range(line: int | None, lines: tuple[int, int] | None, slack: int = 0) -> bool:
    """Is ``line`` within ``lines`` (inclusive), widened by ``slack``?

    A location-less claim can never hard-match a ranged label, but may near-miss.
    """
    if lines is None:
        return True
    if line is None:
        return slack > 0
    lo, hi = lines
    return lo - slack <= line <= hi + slack


def match_claim(
    claim: Claim,
    *,
    file: str,
    lines: tuple[int, int] | None,
    texts: tuple[str, ...] | list[str],
    threshold: float = CLAIM_THRESHOLD,
) -> MatchVerdict:
    """Match one emitted claim against one label, decomposed into ``file``/``lines``/``texts``.

    A different file is always NO_MATCH: file identity is non-negotiable.
    """
    if claim.file != file:
        return MatchVerdict.NO_MATCH
    overlap = best_overlap(claim.text, texts)
    if _in_range(claim.line, lines):
        if overlap >= threshold:
            return MatchVerdict.MATCH
        if overlap >= NEAR_MISS_FLOOR:
            return MatchVerdict.NEAR_MISS
        return MatchVerdict.NO_MATCH
    if overlap >= threshold and _in_range(claim.line, lines, NEAR_MISS_LINE_SLACK):
        return MatchVerdict.NEAR_MISS
    return MatchVerdict.NO_MATCH


def same_claim(
    a: Claim,
    b: Claim,
    *,
    line_slack: int = NEAR_MISS_LINE_SLACK,
    threshold: float = CLAIM_THRESHOLD,
) -> bool:
    """Are two emitted claims the same claim? The symmetric finding-vs-finding rule.

    A missing line on either side falls back to file scope.
    """
    if a.file != b.file:
        return False
    if a.line is not None and b.line is not None and abs(a.line - b.line) > line_slack:
        return False
    return claim_overlap(a.text, b.text) >= threshold
