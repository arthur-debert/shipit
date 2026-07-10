"""schema — the single-repo review JSON schema + a tolerant JSON extractor.

`REVIEW_SCHEMA` is the JSON-schema the codex backend enforces natively
(`--output-schema`) and the agy backend describes in-prose.

Each finding carries the shared 4-tier :class:`~shipit.finding.Severity` ladder
(``critical | major | minor | nit`` — the enum values come FROM the domain
module, never a second copy) plus an informational-only ``category`` and
``confidence``, its quoted ``evidence``, and a suggested ``fix``. The review
summary carries a **coverage attestation** — what was reviewed and what was
skipped with reasons — so silence means "clean," not "skipped." The attestation
is human-facing, not engine data.

`extract_json` is the three-fallback parse: direct, fenced (```json …```), then a
BALANCED SCAN over every embedded ``{…}`` object (RVW03-WS03) — agents wrap their
JSON output inconsistently, so the parser is deliberately forgiving. The scan
replaced a greedy ``{.*}`` regex whose first-brace-to-last-brace capture broke on
brace-bearing wrapper prose (a stray braced log line silently cost the round a
whole dimension pass) and could splice a WRONG object out of two adjacent ones.

`finding_from_dict` is the ONE trust boundary from an unvalidated
``REVIEW_SCHEMA`` comment dict to a typed :class:`~shipit.finding.Finding` —
shared by the posting path (:mod:`shipit.review.post`) and the review-round
record (:mod:`shipit.review.roundrecord`), so both consumers coerce agent JSON
the same way and the record can never disagree with what was posted.
"""

from __future__ import annotations

import json
import re

from ..finding import Finding, Severity, parse_severity, resolve_severity

REVIEW_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["APPROVED", "REQUEST_CHANGES", "COMMENT"],
                },
                "overall_feedback": {"type": "string"},
                "coverage": {
                    "type": "object",
                    "properties": {
                        "reviewed": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "skipped": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "file": {"type": "string"},
                                    "reason": {"type": "string"},
                                },
                                "required": ["file", "reason"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["reviewed", "skipped"],
                    "additionalProperties": False,
                },
            },
            "required": ["status", "overall_feedback", "coverage"],
            "additionalProperties": False,
        },
        "comments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    # Nullable: a file-level finding has no specific line, and the
                    # posting path folds a null-line finding in unanchored. `line`
                    # STAYS in `required` — codex's strict `--output-schema` needs
                    # every property required; optionality rides the null type.
                    "line": {"type": ["integer", "null"]},
                    "text": {"type": "string"},
                    "severity": {
                        "type": "string",
                        "enum": [severity.value for severity in Severity],
                    },
                    "category": {"type": "string"},
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                    "evidence": {"type": "string"},
                    "fix": {"type": "string"},
                },
                "required": [
                    "file",
                    "line",
                    "text",
                    "severity",
                    "category",
                    "confidence",
                    "evidence",
                    "fix",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "comments"],
    "additionalProperties": False,
}


def _str_field(value: object) -> str:
    """Coerce an untrusted JSON field to a ``str``, else ``""``.

    The agy path has no native schema enforcement, so a comment field the schema
    types as a string may arrive as any JSON shape. A non-string here must not
    ride into a domain :class:`Finding` where it would crash a consumer —
    a dict ``category`` breaks :func:`~shipit.finding.render_marker`'s ``_escape``
    (no ``.replace``), an unhashable ``file`` breaks the posting path's
    ``anchorable`` lookup. This is the trust boundary: every string field of the
    Finding it returns is honestly a string.
    """
    return value if isinstance(value, str) else ""


def finding_from_dict(raw: dict) -> Finding:
    """Map one ``REVIEW_SCHEMA`` comment dict to a domain :class:`Finding`.

    The **trust boundary** from unvalidated agent JSON to a typed Finding: every
    field is coerced to the domain type or a fail-safe, so a malformed comment
    (the schema-unenforced agy path) can NEVER crash a downstream consumer — the
    posting path and the review-round record both route through here, ONE rule
    set. Severity follows the fail-safe chain
    (:func:`shipit.finding.resolve_severity`): an absent or unparseable severity
    lands on ``major`` — it forces a round rather than slipping past the Breaker.
    String fields fall back to ``""``, a non-int ``line`` to ``None``, a
    non-number ``confidence`` to ``None`` — and a ``bool`` (an ``int`` subclass)
    is rejected as neither, so ``line: true`` never becomes line 1.
    """
    line = raw.get("line")
    confidence = raw.get("confidence")
    return Finding(
        # The agent's structured `severity` is adapter-layer input (a reviewer
        # stating severity in its output), NOT a machine marker recovered from a
        # posted body — pass the `adapter=` slot so the precedence chain reads it
        # in the right place (ADR-0044: marker → adapter → major default).
        severity=resolve_severity(adapter=parse_severity(raw.get("severity"))),
        text=_str_field(raw.get("text")),
        file=_str_field(raw.get("file")),
        # `bool` is a subclass of `int`, so exclude it explicitly — `line: true`
        # must NOT coerce to line 1 and anchor a comment to the wrong location.
        line=line if isinstance(line, int) and not isinstance(line, bool) else None,
        category=_str_field(raw.get("category")),
        # JSON Schema `type: number` admits an int (`1`); coerce so a Finding's
        # confidence is honestly a float and never a bare int downstream. Exclude
        # `bool` (an int subclass) so `confidence: true` is rejected, not 1.0.
        confidence=(
            float(confidence)
            if isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
            else None
        ),
        evidence=_str_field(raw.get("evidence")),
        fix=_str_field(raw.get("fix")),
    )


def extract_json(text: str) -> dict:
    """Parse a JSON object out of an agent's stdout, tolerating wrapping.

    Tries, in order: a direct parse of the stripped text; stripping ```json …```
    code fences; a BALANCED SCAN over every embedded object
    (:func:`_scan_embedded_objects`) that returns the largest one — never the
    greedy first-brace-to-last-brace splice (RVW03-WS03). Raises
    :class:`ValueError` if none yield valid JSON.
    """
    text_clean = text.strip()
    try:
        return json.loads(text_clean)
    except json.JSONDecodeError:
        pass

    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text_clean, flags=re.MULTILINE)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    best = None
    for candidate, span in _scan_embedded_objects(text_clean):
        if best is None or span > best[1]:
            best = (candidate, span)
    if best is not None:
        return best[0]

    raise ValueError(f"Could not parse valid JSON from output:\n{text}")


def _scan_embedded_objects(text: str) -> list[tuple[dict, int]]:
    """Every complete JSON OBJECT embedded in ``text``, as ``(object, source
    length)`` pairs — the balanced-scan fallback behind :func:`extract_json`.

    At each ``{`` the real JSON decoder (:meth:`json.JSONDecoder.raw_decode`)
    tries to parse ONE complete value starting there — so brace balance,
    strings, and escapes follow the actual JSON grammar (a ``}`` inside a
    string literal never closes an object) and a returned candidate is a
    complete, well-formed object by construction: a mis-spliced object (half of
    one candidate glued to half of another, the greedy-regex failure mode) is
    impossible. A ``{`` that starts no valid JSON (brace-bearing prose, an
    unquoted-key log line, a truncated object) is stepped past — the scan
    resumes at the first ``{`` AFTER the decode-failure position, because
    everything up to it was consumed as the valid prefix of the BROKEN object:
    an interior object of a truncated review must never surface as a standalone
    candidate (a timed-out review whose complete inner ``summary`` parsed would
    silently read as a clean, comment-less round — exactly what the upstream
    timeout-salvage path exists to catch, so truncated-only output still raises
    there). A ``{`` that DOES parse is consumed whole, so an object's own
    nested braces are never re-scanned as candidates.

    The caller picks the LARGEST candidate: wrapper prose around a review
    commonly carries small braced fragments (log lines, inline examples), and
    the findings object dwarfs them — while first-match would hand back
    whichever fragment happened to come first.
    """
    decoder = json.JSONDecoder()
    found: list[tuple[dict, int]] = []
    index = text.find("{")
    while index != -1:
        try:
            candidate, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError as exc:
            # Skip the broken object's consumed prefix, not just one brace:
            # its interior objects are fragments of IT, never candidates.
            index = text.find("{", max(exc.pos, index + 1))
            continue
        if isinstance(candidate, dict):
            found.append((candidate, end - index))
        index = text.find("{", end)
    return found
