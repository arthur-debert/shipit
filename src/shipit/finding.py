"""The review Finding domain — the Severity ladder, dispositions, and both wire formats.

Pure, no I/O. See docs/adr/0044-findings-arrive-classified.md.
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
    """The 4-tier ladder every Finding carries; the major/minor boundary is the merge-block test."""

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

DEFAULT_SEVERITY = Severity.MAJOR


class Disposition(Enum):
    """What the calibrator decided to do with a judged Finding; everything but ``post`` is routed out yet retained."""

    POST = "post"
    DROP_UNVERIFIED = "drop-unverified"
    NIT_SUPPRESSED = "nit-suppressed"
    OUT_OF_SCOPE = "out-of-scope"


@dataclass(frozen=True)
class Finding:
    """One reviewer-reported issue, pre-classified; ``severity`` routes, ``category``/``confidence`` are informational only."""

    severity: Severity
    text: str
    file: str = ""
    line: int | None = None
    category: str = ""
    confidence: float | None = None
    evidence: str = ""
    fix: str = ""


@dataclass(frozen=True)
class JudgedFinding:
    """A Finding paired with its routing; it counts as posted only when canonical and disposed ``post``."""

    finding: Finding
    disposition: Disposition
    duplicate_of: int | None = None
    run_id: str | None = None

    @property
    def posted(self) -> bool:
        return self.disposition is Disposition.POST and self.duplicate_of is None


@dataclass(frozen=True)
class Marker:
    """The tuple recovered from a machine marker; ``severity`` is None when none parsed."""

    severity: Severity | None
    category: str = ""
    confidence: float | None = None


def parse_severity(value: object) -> Severity | None:
    """Tolerantly map a raw value to a :class:`Severity` (case-insensitive ladder tokens), else None."""
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
    policy: Severity | None = None,
) -> Severity:
    """The severity precedence chain: ``override`` beats marker, adapter mapping, ``policy``, then :data:`DEFAULT_SEVERITY`."""
    if override is not None:
        return override
    return marker or adapter or policy or DEFAULT_SEVERITY


def order_findings(findings: Iterable[Finding]) -> list[Finding]:
    """Sort findings highest severity first (critical → nit), stable within a tier."""
    return sorted(findings, key=lambda f: f.severity.rank)


CONVENTIONAL_PREFIXES: dict[Severity, str] = {
    Severity.CRITICAL: "issue (critical, blocking):",
    Severity.MAJOR: "issue (blocking):",
    Severity.MINOR: "suggestion (non-blocking):",
    Severity.NIT: "nitpick:",
}

FIX_LABEL = "Suggested fix:"


_MARKER_TAG = "shipit:finding"

_MARKER_RE = re.compile(rf"<!--\s*{_MARKER_TAG}\b(.*?)-->", re.DOTALL)

_ATTR_RE = re.compile(r'([A-Za-z_]+)=(?:"([^"]*)"|(\S+))')

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
    """Render the machine marker; absent category/confidence are omitted rather than fabricated."""
    parts = [f"severity={finding.severity.value}"]
    if finding.category:
        parts.append(f'category="{_escape(finding.category)}"')
    if finding.confidence is not None:
        parts.append(f"confidence={_format_confidence(finding.confidence)}")
    return f"<!-- {_MARKER_TAG} {' '.join(parts)} -->"


def parse_marker(body: str) -> Marker | None:
    """Recover the machine tuple from the FIRST marker in a body; None when there is none, ``severity=None`` when it is unreadable."""
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
    """Render the two-layer comment body: the invisible marker, then the Conventional Comments human layer."""
    prefix = CONVENTIONAL_PREFIXES[finding.severity]
    body = f"{render_marker(finding)}\n{prefix} {finding.text}"
    if finding.evidence:
        body += f"\n\n```\n{finding.evidence}\n```"
    if finding.fix:
        body += f"\n\n{FIX_LABEL} {finding.fix}"
    return body


_PREFIXES_BY_LENGTH = sorted(CONVENTIONAL_PREFIXES.values(), key=len, reverse=True)

_EVIDENCE_RE = re.compile(r"\n\n```\n(.*?)\n```", re.DOTALL)


def parse_comment(body: str, *, file: str = "", line: int | None = None) -> Finding:
    """Recover a :class:`Finding` from a posted body; ``file``/``line`` are thread properties the caller supplies, and a rendered body round-trips losslessly."""
    marker = parse_marker(body)
    text = _MARKER_RE.sub("", body, count=1).strip()

    for prefix in _PREFIXES_BY_LENGTH:
        if text.startswith(prefix):
            text = text[len(prefix) :].lstrip()
            break

    fix = ""
    fix_at = text.rfind(f"\n\n{FIX_LABEL} ")
    if fix_at != -1:
        fix = text[fix_at + len(FIX_LABEL) + 3 :].strip()
        text = text[:fix_at]

    evidence = ""
    evidence_matches = list(_EVIDENCE_RE.finditer(text))
    if evidence_matches:
        last = evidence_matches[-1]
        evidence = last.group(1)
        text = text[: last.start()] + text[last.end() :]

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
