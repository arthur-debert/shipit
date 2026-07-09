"""finding — the review Finding domain: Severity ladder, dispositions, wire formats.

The **deep, pure module** (no I/O) for review findings (ADR-0044, RVW02). One
reviewer-reported issue on a PR is a :class:`Finding` — a located claim carrying
a :class:`Severity`, a category, and a confidence — and this module is the ONE
place its vocabulary and wire formats live. The review pipeline and the PR state
engine both consume it; neither defines its own copy.

What it owns:

- **The 4-tier Severity ladder** — ``critical | major | minor | nit`` — with its
  ordering (:func:`order_findings`, :attr:`Severity.rank`) and the merge-block
  test (:attr:`Severity.blocks_merge`: major-or-worse means a competent reviewer
  would hold the merge). This REPLACES the retired ERROR/WARNING/INFO triple.
- **The disposition vocabulary** — :class:`Disposition`:
  ``post | drop-unverified | nit-suppressed | out-of-scope`` (pre-existing
  issues are the archetypal out-of-scope routing).
- **Both wire renderings** of a posted finding (:func:`render_comment`):
  the Conventional Comments human layer (``issue (critical, blocking):`` /
  ``issue (blocking):`` / ``suggestion (non-blocking):`` / ``nitpick:``) and the
  invisible machine marker — an HTML comment carrying the EXACT
  severity/category/confidence tuple — so severity survives GitHub with the
  threads as the engine's only finding store.
- **The parser** (:func:`parse_comment`, :func:`parse_marker`) that recovers a
  Finding from a posted comment body alone, and the **severity precedence
  chain** (:func:`resolve_severity`): machine marker → reviewer-adapter mapping
  → ``major`` default, beaten only by a write-once Severity override. The
  ``major`` default is the fail-safe: an unparseable finding forces a review
  round rather than slipping past the Breaker.

Category and confidence ride along **informational-only** — nothing routes on
them; Severity is the engine's sole routing key.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "CONVENTIONAL_PREFIXES",
    "DEFAULT_SEVERITY",
    "FIX_LABEL",
    "Disposition",
    "Finding",
    "Marker",
    "Severity",
    "order_findings",
    "parse_comment",
    "parse_marker",
    "parse_severity",
    "render_comment",
    "render_marker",
    "resolve_severity",
]


class Severity(Enum):
    """The 4-tier ladder every Finding carries — one ladder across all reviewer kinds.

    The major/minor boundary is the **merge-block test**: major-or-worse means a
    competent reviewer would hold the merge for it. ``critical`` = merge would be
    actively harmful (security, data loss, crash, broken build); ``major`` = a
    concrete correctness/behavioral defect worth blocking on; ``minor`` = worth
    doing, not worth holding the merge; ``nit`` = wording, naming, or style with
    no correctness, behavioral, or security impact.
    """

    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    NIT = "nit"

    @property
    def rank(self) -> int:
        """Ordering key, 0 = most severe (``critical``) … 3 = least (``nit``)."""
        return _RANKS[self]

    @property
    def blocks_merge(self) -> bool:
        """The merge-block test: True for major-or-worse — only these mint rounds."""
        return self in (Severity.CRITICAL, Severity.MAJOR)


_RANKS: dict[Severity, int] = {s: i for i, s in enumerate(Severity)}

#: The fail-safe severity: an unparseable finding defaults to ``major`` so a
#: parsing failure forces a review round instead of slipping past the Breaker.
DEFAULT_SEVERITY = Severity.MAJOR


class Disposition(Enum):
    """What the calibrator decided to do with a judged Finding.

    ``post`` reaches the PR; the rest are routed out but RETAINED (never erased —
    the future Opportunity harvest reads them): ``drop-unverified`` failed
    adversarial verification, ``nit-suppressed`` is a new nit after round 1,
    ``out-of-scope`` is beyond the PR's diff (pre-existing issues the archetype).
    """

    POST = "post"
    DROP_UNVERIFIED = "drop-unverified"
    NIT_SUPPRESSED = "nit-suppressed"
    OUT_OF_SCOPE = "out-of-scope"


@dataclass(frozen=True)
class Finding:
    """One reviewer-reported issue: a located claim, arriving PRE-classified.

    ``severity`` is the engine's sole routing key; ``category`` (the dimension
    that found it — e.g. ``correctness``) and ``confidence`` (0.0–1.0) are
    informational-only. ``file``/``line`` locate the claim, ``text`` states it,
    ``evidence`` is the quoted code backing it, ``fix`` the suggested remedy.
    """

    severity: Severity
    text: str
    file: str = ""
    line: int | None = None
    category: str = ""
    confidence: float | None = None
    evidence: str = ""
    fix: str = ""


@dataclass(frozen=True)
class Marker:
    """The tuple recovered from a machine marker. ``severity`` is None when the
    marker carried no parseable severity (the caller falls through the
    :func:`resolve_severity` chain)."""

    severity: Severity | None
    category: str = ""
    confidence: float | None = None


def parse_severity(value: object) -> Severity | None:
    """Tolerantly map a raw value to a :class:`Severity`, else None.

    Accepts the ladder tokens case-insensitively (``"Major"`` → ``MAJOR``).
    Anything else — including the retired ERROR/WARNING/INFO triple — is None:
    the caller's precedence chain, not this parser, owns the ``major`` default.
    """
    if isinstance(value, Severity):
        return value
    if not isinstance(value, str):
        return None
    try:
        return Severity(value.strip().lower())
    except ValueError:
        return None


def resolve_severity(
    marker: Severity | None = None,
    adapter: Severity | None = None,
    override: Severity | None = None,
) -> Severity:
    """The severity precedence chain (ADR-0044).

    Machine marker → reviewer-adapter mapping → :data:`DEFAULT_SEVERITY`
    (``major``, the fail-safe); a write-once Severity ``override`` beats all
    three.
    """
    if override is not None:
        return override
    return marker or adapter or DEFAULT_SEVERITY


def order_findings(findings: Iterable[Finding]) -> list[Finding]:
    """Sort findings highest severity first (critical → nit), stable within a tier."""
    return sorted(findings, key=lambda f: f.severity.rank)


# --- Wire rendering 1: the Conventional Comments human layer -----------------

#: Severity → the Conventional Comments label that opens the human layer. The
#: blocking/non-blocking decoration makes severity legible at a glance; the
#: retired ``Agent: <name> [SEVERITY]`` prefix is NOT part of this format.
CONVENTIONAL_PREFIXES: dict[Severity, str] = {
    Severity.CRITICAL: "issue (critical, blocking):",
    Severity.MAJOR: "issue (blocking):",
    Severity.MINOR: "suggestion (non-blocking):",
    Severity.NIT: "nitpick:",
}

#: The paragraph label that opens the optional fix-suggestion section. Public so
#: the review body's unanchored fold renders the same label without duplicating
#: the literal — this module OWNS the wire vocabulary.
FIX_LABEL = "Suggested fix:"


# --- Wire rendering 2: the invisible machine marker ---------------------------

#: The marker tag — ``<!-- shipit:finding severity=… … -->``. HTML comments are
#: invisible on GitHub, so the exact tuple rides every posted comment unseen.
_MARKER_TAG = "shipit:finding"

_MARKER_RE = re.compile(rf"<!--\s*{_MARKER_TAG}\b(.*?)-->", re.DOTALL)

#: key=value pairs inside a marker: the value is either double-quoted (may be
#: empty, holds escaped free-form text) or a bare non-space token.
_ATTR_RE = re.compile(r'([A-Za-z_]+)=(?:"([^"]*)"|(\S+))')

# Free-form marker values (category) are escaped so the marker stays a valid,
# single HTML comment: ``&`` first, then ``"`` (the value delimiter) and ``--``
# (illegal inside an HTML comment). Unescape reverses in the opposite order, so
# the round trip is lossless.
_ESCAPES = (("&", "&amp;"), ('"', "&quot;"), ("--", "&#45;&#45;"))


def _escape(value: str) -> str:
    for raw, escaped in _ESCAPES:
        value = value.replace(raw, escaped)
    return value


def _unescape(value: str) -> str:
    for raw, escaped in reversed(_ESCAPES):
        value = value.replace(escaped, raw)
    return value


def _format_confidence(confidence: float) -> str:
    """A stable, compact decimal for the marker (``0.8``, not ``0.8000000001``)."""
    return f"{confidence:g}"


def render_marker(finding: Finding) -> str:
    """Render the machine marker carrying the exact severity/category/confidence
    tuple. Category and confidence are omitted when absent — informational-only
    fields never fabricate values."""
    parts = [f"severity={finding.severity.value}"]
    if finding.category:
        parts.append(f'category="{_escape(finding.category)}"')
    if finding.confidence is not None:
        parts.append(f"confidence={_format_confidence(finding.confidence)}")
    return f"<!-- {_MARKER_TAG} {' '.join(parts)} -->"


def parse_marker(body: str) -> Marker | None:
    """Recover the machine tuple from a comment body, else None.

    Returns None when no ``shipit:finding`` marker is present at all. A marker
    that IS present but malformed still returns a :class:`Marker` with whatever
    parsed — an unreadable severity comes back as ``severity=None`` so the
    caller's :func:`resolve_severity` chain lands on the ``major`` fail-safe.
    Only the FIRST marker in the body counts.
    """
    match = _MARKER_RE.search(body)
    if match is None:
        return None
    attrs: dict[str, str] = {}
    for key, quoted, bare in _ATTR_RE.findall(match.group(1)):
        attrs.setdefault(key, quoted if bare == "" else bare)
    confidence: float | None = None
    raw_confidence = attrs.get("confidence")
    if raw_confidence is not None:
        try:
            confidence = float(raw_confidence)
        except ValueError:
            confidence = None
    return Marker(
        severity=parse_severity(attrs.get("severity")),
        category=_unescape(attrs.get("category", "")),
        confidence=confidence,
    )


def render_comment(finding: Finding) -> str:
    """Render the full two-layer comment body for a posted finding.

    Line 1 is the invisible machine marker; then the Conventional Comments human
    layer — the tier's label + the claim text — followed by the quoted evidence
    as a fenced block and the ``Suggested fix:`` paragraph when present.
    :func:`parse_comment` reverses this layout.
    """
    prefix = CONVENTIONAL_PREFIXES[finding.severity]
    body = f"{render_marker(finding)}\n{prefix} {finding.text}"
    if finding.evidence:
        body += f"\n\n```\n{finding.evidence}\n```"
    if finding.fix:
        body += f"\n\n{FIX_LABEL} {finding.fix}"
    return body


# Longest prefix first, so "issue (critical, blocking):" wins over a hypothetical
# shorter overlap when matching the human layer.
_PREFIXES_BY_LENGTH = sorted(CONVENTIONAL_PREFIXES.values(), key=len, reverse=True)

_EVIDENCE_RE = re.compile(r"\n\n```\n(.*?)\n```", re.DOTALL)


def parse_comment(body: str, *, file: str = "", line: int | None = None) -> Finding:
    """Recover a :class:`Finding` from a posted comment body alone.

    The body carries severity/category/confidence (the machine marker) and the
    claim text / evidence / fix (the human layer); ``file``/``line`` are thread
    properties GitHub holds, so the caller passes them in. Severity follows the
    fail-safe chain: the marker's value, else :data:`DEFAULT_SEVERITY` (an
    unparseable finding forces a round). A body :func:`render_comment` produced
    round-trips losslessly, provided the claim text does not itself contain the
    layout delimiters (a blank-line-fenced code block or a ``Suggested fix:``
    paragraph).
    """
    marker = parse_marker(body)
    text = _MARKER_RE.sub("", body).strip()

    for prefix in _PREFIXES_BY_LENGTH:
        if text.startswith(prefix):
            text = text[len(prefix) :].lstrip()
            break

    fix = ""
    fix_at = text.find(f"\n\n{FIX_LABEL} ")
    if fix_at != -1:
        fix = text[fix_at + len(FIX_LABEL) + 3 :].strip()
        text = text[:fix_at]

    evidence = ""
    evidence_match = _EVIDENCE_RE.search(text)
    if evidence_match:
        evidence = evidence_match.group(1)
        text = text[: evidence_match.start()] + text[evidence_match.end() :]

    finding = Finding(
        severity=resolve_severity(marker.severity if marker else None),
        text=text.strip(),
        file=file,
        line=line,
        category=marker.category if marker else "",
        confidence=marker.confidence if marker else None,
        evidence=evidence,
        fix=fix,
    )
    return finding
