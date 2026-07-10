"""match — the deterministic same-claim matching primitive (ADR-0048, RVW03-WS06).

ONE definition of "the same claim", tuned in ONE place, shared by its two
consumers: the Ground-truth **scorer** (:mod:`shipit.review.scorer`, matching an
emitted finding against a fixture label) and semantic dedup of same-round
findings (#673, matching two emitted findings against each other). No LLM is
ever part of this module — a misjudging semantic matcher would be the RVW02
calibrator failure reproduced inside the ruler itself (ADR-0048); matching is
pure lexical mechanics, deterministic across runs and free to re-run forever.

The rule, stated once (ADR-0048): two claims are THE SAME when they name the
same **file**, a **line within the label's range** (with no slack — the range
itself is the tolerance; a label with no range is file-scoped), and their
normalized **claim tokens overlap** at or above the lexical threshold — where a
label's banked phrasing **aliases** count as additional claim texts (best
overlap wins, so one adjudicated rewording is enough to match forever after).
A **near-miss** is right file + overlapping lines but claim below the
threshold, OR right claim but line just outside the range — surfaced (never
silently dropped) so the scorer's adjudication report can offer it for banking
as an alias or a range correction.

Normalization is deliberately dumb and inspectable: lowercase, split on
non-alphanumerics (so ``applyLayout``/``apply_layout`` both yield tokens, and
code identifiers survive as their words), drop pure stopwords and single
characters. Similarity is the **overlap coefficient**
(``|A∩B| / min(|A|,|B|)``), not Jaccard: a fixture claim is one dense sentence
while an emitted finding's text is often a paragraph, and Jaccard would punish
the length asymmetry rather than measure agreement.
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
    """The three deterministic outcomes of matching a claim against a label.

    ``MATCH`` scores; ``NEAR_MISS`` is surfaced for Adjudication (an alias or
    range correction may be banked, ADR-0048); ``NO_MATCH`` is silence — the
    claim tells the label's story not at all.
    """

    MATCH = "match"
    NEAR_MISS = "near-miss"
    NO_MATCH = "no-match"


#: The lexical threshold: overlap coefficient at or above this is a MATCH
#: (given file + line agree). Tuned on the RVW02-WS05 wording variants (the
#: app#391 offscreen-pan claim matches its historical rewording at ~0.7).
CLAIM_THRESHOLD = 0.5

#: Below the threshold but at/above this floor still *suggests* the same claim —
#: combined with right file + right lines it makes a NEAR_MISS instead of
#: silence. Below the floor only exact location overlap can make a near-miss.
NEAR_MISS_FLOOR = 0.2

#: How far outside a label's line range a claim-passing finding may sit and
#: still surface as a NEAR_MISS (line drift between the pinned head and what a
#: reviewer reports is real; a MATCH gets zero slack — ADR-0048's rule is
#: "line within the label's range").
NEAR_MISS_LINE_SLACK = 10

#: Tokens carrying no claim identity. Deliberately small and generic — claim
#: identity should ride on the defect's own words, not on connective tissue.
_STOPWORDS = frozenset(
    """
    a an and are as at be but by for from has have if in into is it its no not
    of on or so that the their then this to was when which will with would
    """.split()
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def normalize_claim(text: str) -> frozenset[str]:
    """A claim's identity as a normalized token set. PURE, deterministic.

    Lowercase, split on every non-alphanumeric run (``0×0-frame`` →
    ``0``…``frame``; snake/kebab identifiers decompose into their words;
    camelCase does NOT decompose — ``applyLayout`` → ``applylayout`` — which is
    fine because both sides of a comparison normalize identically), then drop
    stopwords and single characters. Order and multiplicity are identity-free:
    matching measures *which* defect words appear, not how often.
    """
    return frozenset(
        token
        for token in _TOKEN_RE.findall(text.lower())
        if len(token) > 1 and token not in _STOPWORDS
    )


def claim_overlap(a: str, b: str) -> float:
    """Overlap coefficient of two claims' normalized token sets, 0.0–1.0.

    ``|A∩B| / min(|A|,|B|)`` — 1.0 when the shorter claim's tokens all appear
    in the longer (a dense fixture claim inside a paragraph-length finding
    text), 0.0 when nothing overlaps or either side normalizes to empty.
    """
    ta, tb = normalize_claim(a), normalize_claim(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def best_overlap(text: str, texts: tuple[str, ...] | list[str]) -> float:
    """The best :func:`claim_overlap` of ``text`` against any of ``texts``.

    The alias rule (ADR-0048): a label's claim and its banked phrasing aliases
    are all admissible phrasings of one defect — best agreement wins.
    """
    return max((claim_overlap(text, other) for other in texts), default=0.0)


@dataclass(frozen=True)
class Claim:
    """One located claim: the matching primitive's input unit.

    ``file`` is a repo-relative path; ``line`` is where the claim points
    (``None`` = file-scoped); ``text`` states the defect. Both an emitted
    Finding and a fixture label project onto this shape — which is exactly why
    the primitive is reusable for finding-vs-finding dedup (#673).
    """

    file: str
    line: int | None
    text: str


def _in_range(line: int | None, lines: tuple[int, int] | None, slack: int = 0) -> bool:
    """Is ``line`` within ``lines`` (inclusive), widened by ``slack``?

    ``lines`` None = file-scoped label: every line (and no line at all)
    qualifies. ``line`` None against a ranged label only qualifies when slack
    is being applied — a location-less claim can never hard-MATCH a ranged
    label, but it may still near-miss on claim strength.
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
    """Match one emitted claim against one label. PURE, deterministic.

    The label side arrives decomposed (``file``, inclusive ``lines`` range or
    ``None`` for file-scoped, ``texts`` = claim + aliases) so the fixture layer
    stays free to evolve its record shape without touching the rule.

    MATCH: same file AND line within the range (no slack) AND best overlap ≥
    ``threshold``. NEAR_MISS (ADR-0048's adjudication feeders): same file AND
    either (line in range, overlap in [floor, threshold)) — a phrasing the
    lexicon does not know yet — or (overlap ≥ threshold, line within
    :data:`NEAR_MISS_LINE_SLACK` of the range) — right claim, drifted location.
    Anything else: NO_MATCH. A different file is ALWAYS no-match: file identity
    is the one non-negotiable coordinate.
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
    """Are two emitted claims the same claim? The #673 dedup seam. PURE.

    The symmetric projection of the label rule for finding-vs-finding
    comparison: same file, lines within ``line_slack`` of each other (a missing
    line on either side falls back to file scope — two file-scoped claims in
    one file compare on text alone), token overlap ≥ ``threshold``.
    """
    if a.file != b.file:
        return False
    if a.line is not None and b.line is not None and abs(a.line - b.line) > line_slack:
        return False
    return claim_overlap(a.text, b.text) >= threshold
