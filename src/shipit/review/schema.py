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
The review caller passes the `is_review_shaped` `want` predicate so a large
UNRELATED JSON blob in noisy stdout is never selected over the real
``{summary, comments}`` review (which would read downstream as a clean,
finding-less pass); a `RecursionError` from deeply nested untrusted output is
caught and the over-deep object stepped past in one O(N) pass, so a pathological
blob neither crashes the round nor hides a valid review printed after it.

`finding_from_dict` is the ONE trust boundary from an unvalidated
``REVIEW_SCHEMA`` comment dict to a typed :class:`~shipit.finding.Finding` —
shared by the posting path (:mod:`shipit.review.post`) and the review-round
record (:mod:`shipit.review.roundrecord`), so both consumers coerce agent JSON
the same way and the record can never disagree with what was posted.

`validate_review` is the zero-dependency structural check of a parsed review
against :data:`REVIEW_SCHEMA` — the engine behind ``shipit review validate``
(#826). It exists so a reviewer AGENT can self-verify its output against the same
shape the funnel expects BEFORE handing it back, closing the loop where an agy
run returned unparseable/ill-typed JSON and burned the whole round. It returns a
list of human-readable problem strings (empty == valid) rather than raising, so
the CLI can print every problem at once; the FIRST-class check it enforces is the
one the issue calls out — ``severity`` must be one of the shared
:class:`~shipit.finding.Severity` enum values, never a free-form string. A
hand-written walk (not a ``jsonschema`` dependency) so the messages point at the
exact JSON path (``comments[2].severity``) and the package keeps its zero-dep
posture.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

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


#: The status enum from :data:`REVIEW_SCHEMA` — derived, never a second copy, so
#: the validator and the schema can never disagree on the allowed summary states.
_STATUS_VALUES: tuple[str, ...] = tuple(
    REVIEW_SCHEMA["properties"]["summary"]["properties"]["status"]["enum"]
)
#: The severity enum, from the shared domain ladder (the schema derives from the
#: same source). This is the check the issue (#826) calls out as first-class: an
#: agent's ``severity`` must be one of these tokens, never a free-form string.
_SEVERITY_VALUES: tuple[str, ...] = tuple(severity.value for severity in Severity)


def _is_str(value: object) -> bool:
    return isinstance(value, str)


def _is_int_not_bool(value: object) -> bool:
    # `bool` is an `int` subclass — a schema `integer` must reject `true`/`false`,
    # matching `finding_from_dict`'s `line` coercion (which drops a bool line).
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number_not_bool(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_object(
    value: object, path: str, required: tuple[str, ...], problems: list[str]
) -> dict | None:
    """Confirm ``value`` is an object with EXACTLY ``required`` keys.

    Appends a problem for a non-object, any missing required key, and any extra
    key (``REVIEW_SCHEMA`` is ``additionalProperties: False`` throughout, so an
    unknown key IS a schema violation — codex's strict ``--output-schema`` rejects
    it, and flagging it here lets an agent match that bar before handing back).
    Returns the dict when it is an object (so field checks can proceed even if a
    key is missing), else ``None``.
    """
    if not isinstance(value, dict):
        problems.append(f"{path}: expected an object, got {_typename(value)}")
        return None
    for key in required:
        if key not in value:
            problems.append(f"{path}: missing required key {key!r}")
    for key in value:
        if key not in required:
            problems.append(f"{path}: unexpected key {key!r} (not in the schema)")
    return value


def _check_field(
    obj: dict, key: str, path: str, predicate, expected: str, problems: list[str]
) -> None:
    """Type-check ``obj[key]`` with ``predicate`` when the key is present.

    A MISSING key is left to :func:`_check_object`'s required-key pass so a field
    is never reported twice; this only rejects a present value of the wrong type.
    """
    if key in obj and not predicate(obj[key]):
        problems.append(f"{path}.{key}: expected {expected}, got {_typename(obj[key])}")


def _typename(value: object) -> str:
    """A short JSON-flavored type name for a problem message (``null``, not ``NoneType``)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, float):
        return "number"
    if isinstance(value, int):
        return "integer"
    return type(value).__name__


def validate_review(payload: object) -> list[str]:
    """Check a parsed review against :data:`REVIEW_SCHEMA`; return problem strings.

    A pure, zero-dependency structural walk: returns an EMPTY list when ``payload``
    conforms, else one human-readable string per problem (JSON-path prefixed, e.g.
    ``comments[2].severity: …``) — every problem at once, so an agent fixes its
    output in one pass rather than whack-a-mole. Never raises and never mutates.

    Enforced (mirroring the schema, ``additionalProperties: False`` throughout):
    the ``{summary, comments}`` envelope; ``summary`` = ``status`` (one of
    :data:`_STATUS_VALUES`), ``overall_feedback`` (string), ``coverage`` (=
    ``reviewed`` list-of-strings + ``skipped`` list-of ``{file, reason}``); each
    ``comments[i]`` = ``file``/``text``/``category``/``evidence``/``fix`` strings,
    ``line`` integer-or-null, ``confidence`` number in ``[0, 1]``, and — the
    first-class check (#826) — ``severity`` one of :data:`_SEVERITY_VALUES`, never
    a free-form string. Unknown keys are flagged (codex's strict schema rejects
    them); this is the bar ``shipit review validate`` holds agents to.
    """
    problems: list[str] = []
    root = _check_object(payload, "review", ("summary", "comments"), problems)
    if root is None:
        return problems

    summary = root.get("summary")
    if "summary" in root:
        _validate_summary(summary, problems)

    comments = root.get("comments")
    if "comments" in root:
        if not isinstance(comments, list):
            problems.append(f"comments: expected an array, got {_typename(comments)}")
        else:
            for index, comment in enumerate(comments):
                _validate_comment(comment, f"comments[{index}]", problems)
    return problems


def _validate_summary(summary: object, problems: list[str]) -> None:
    """Validate the ``summary`` object (status/overall_feedback/coverage)."""
    obj = _check_object(
        summary, "summary", ("status", "overall_feedback", "coverage"), problems
    )
    if obj is None:
        return
    if "status" in obj and obj["status"] not in _STATUS_VALUES:
        problems.append(
            f"summary.status: {obj['status']!r} is not one of "
            f"{', '.join(_STATUS_VALUES)}"
        )
    _check_field(obj, "overall_feedback", "summary", _is_str, "a string", problems)
    if "coverage" in obj:
        _validate_coverage(obj["coverage"], problems)


def _validate_coverage(coverage: object, problems: list[str]) -> None:
    """Validate ``summary.coverage`` (reviewed list + skipped file/reason list)."""
    obj = _check_object(coverage, "summary.coverage", ("reviewed", "skipped"), problems)
    if obj is None:
        return
    reviewed = obj.get("reviewed")
    if "reviewed" in obj:
        if not isinstance(reviewed, list):
            problems.append(
                f"summary.coverage.reviewed: expected an array, got "
                f"{_typename(reviewed)}"
            )
        else:
            for index, item in enumerate(reviewed):
                if not _is_str(item):
                    problems.append(
                        f"summary.coverage.reviewed[{index}]: expected a string, "
                        f"got {_typename(item)}"
                    )
    skipped = obj.get("skipped")
    if "skipped" in obj:
        if not isinstance(skipped, list):
            problems.append(
                f"summary.coverage.skipped: expected an array, got {_typename(skipped)}"
            )
        else:
            for index, item in enumerate(skipped):
                path = f"summary.coverage.skipped[{index}]"
                entry = _check_object(item, path, ("file", "reason"), problems)
                if entry is None:
                    continue
                _check_field(entry, "file", path, _is_str, "a string", problems)
                _check_field(entry, "reason", path, _is_str, "a string", problems)


def _validate_comment(comment: object, path: str, problems: list[str]) -> None:
    """Validate one ``comments[i]`` finding — the severity-enum check lives here."""
    obj = _check_object(
        comment,
        path,
        (
            "file",
            "line",
            "text",
            "severity",
            "category",
            "confidence",
            "evidence",
            "fix",
        ),
        problems,
    )
    if obj is None:
        return
    _check_field(obj, "file", path, _is_str, "a string", problems)
    _check_field(obj, "text", path, _is_str, "a string", problems)
    _check_field(obj, "category", path, _is_str, "a string", problems)
    _check_field(obj, "evidence", path, _is_str, "a string", problems)
    _check_field(obj, "fix", path, _is_str, "a string", problems)
    if "line" in obj and not (obj["line"] is None or _is_int_not_bool(obj["line"])):
        problems.append(
            f"{path}.line: expected an integer or null, got {_typename(obj['line'])}"
        )
    # The first-class check (#826): severity must be one of the shared enum tokens.
    if "severity" in obj and obj["severity"] not in _SEVERITY_VALUES:
        problems.append(
            f"{path}.severity: {obj['severity']!r} is not one of "
            f"{', '.join(_SEVERITY_VALUES)}"
        )
    if "confidence" in obj:
        confidence = obj["confidence"]
        if not _is_number_not_bool(confidence):
            problems.append(
                f"{path}.confidence: expected a number, got {_typename(confidence)}"
            )
        elif not 0 <= confidence <= 1:
            problems.append(f"{path}.confidence: {confidence} is out of range [0, 1]")


def is_review_shaped(payload: dict) -> bool:
    """Whether ``payload`` has the review ENVELOPE shape — a ``summary`` object and
    a ``comments`` list (:data:`REVIEW_SCHEMA`'s two required top-level keys).

    The shape predicate the review parse path hands :func:`extract_json` as
    ``want`` so an UNRELATED JSON object in noisy stdout is never selected as the
    review. It is deliberately shallow: it gates SELECTION among embedded
    candidates, not full schema validation (:func:`finding_from_dict` is the
    per-finding trust boundary), so it only has to tell the real
    ``{summary, comments}`` envelope apart from a stray log/tool blob.
    """
    return isinstance(payload.get("summary"), dict) and isinstance(
        payload.get("comments"), list
    )


def _accepted(value: object, want: Callable[[dict], bool] | None) -> bool:
    """Whether ``value`` is an acceptable :func:`extract_json` result.

    A non-``dict`` is NEVER accepted — :func:`extract_json` is typed and
    documented to yield a JSON OBJECT, so a bare array/string/number from a fast
    path must fall through (and, if nothing else matches, raise ``ValueError`` →
    :class:`~shipit.review.backends.BackendError`) rather than reach a caller that
    assumes a mapping (the calibrator's ``_unwrap_output`` does ``parsed.get(…)``
    and would ``AttributeError`` on a list). A GENERIC caller (``want`` is
    ``None``) accepts any such dict; a caller that passes a shape predicate accepts
    ONLY a dict the predicate approves, so an off-shape object is never selected.
    """
    if not isinstance(value, dict):
        return False
    return want is None or want(value)


def extract_json(text: str, *, want: Callable[[dict], bool] | None = None) -> dict:
    """Parse a JSON object out of an agent's stdout, tolerating wrapping.

    Tries, in order: a direct parse of the stripped text; stripping ```json …```
    code fences; a BALANCED SCAN over every embedded object
    (:func:`_scan_embedded_objects`) — never the greedy first-brace-to-last-brace
    splice (RVW03-WS03). Raises :class:`ValueError` if none yield an accepted
    object.

    Every accepted result is a JSON OBJECT (``dict``) — the return type and the
    contract — so a fast path that parses to a bare array/string falls through
    rather than escaping to a caller that assumes a mapping.

    ``want`` is an optional SHAPE predicate. Without it (the generic caller — the
    calibrator envelope, which is NOT review-shaped) the fast paths return any
    valid JSON object and the scan returns the LARGEST embedded object. WITH it
    (the review caller passes :func:`is_review_shaped`) only a ``dict`` the
    predicate approves is accepted: the fast paths fall through when their parse is
    not a wanted dict, and the scan selects the largest *wanted* candidate rather
    than merely the largest. This closes a silent-clean-pass
    hole — noisy stdout can carry a large unrelated JSON blob (a log/tool object)
    around the real ``{summary, comments}`` review, and returning that blob (a
    dict with no ``comments``) would read downstream as a finding-less, i.e.
    CLEAN, pass. When no wanted candidate exists the parse fails LOUDLY
    (``ValueError`` → :class:`~shipit.review.backends.BackendError` → the #76
    salvage), never a silent clean.

    Deeply nested untrusted output can exhaust the recursion limit INSIDE the JSON
    decoder; ``RecursionError`` is caught alongside ``json.JSONDecodeError`` on the
    fast-path parses, and :func:`_scan_embedded_objects` STEPS PAST an over-deep
    object in one O(N) pass (:func:`_skip_balanced_object`) and keeps scanning — so
    a pathological blob neither crashes the round nor hides a valid review printed
    after it, and a per-brace O(N^2) rescan is never triggered.
    """
    text_clean = text.strip()
    try:
        parsed = json.loads(text_clean)
    except (json.JSONDecodeError, RecursionError):
        pass
    else:
        if _accepted(parsed, want):
            return parsed

    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text_clean, flags=re.MULTILINE)
    try:
        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, RecursionError):
        pass
    else:
        if _accepted(parsed, want):
            return parsed

    best: tuple[dict, int] | None = None
    for candidate, span in _scan_embedded_objects(text_clean):
        if not _accepted(candidate, want):
            continue
        if best is None or span > best[1]:
            best = (candidate, span)
    if best is not None:
        return best[0]

    raise ValueError(f"Could not parse valid JSON from output:\n{text}")


def has_complete_json_object(text: str) -> bool:
    """Whether ``text`` contains at least one COMPLETE embedded JSON object.

    The extractor's own balanced scan (:func:`_scan_embedded_objects`) as a
    predicate, for the parse-failure diagnosis in
    :func:`shipit.review.backends.base.parse_review_output` (issue #1006): a
    stdout that carries a complete object which the review parse still rejected
    holds a wrong-envelope verdict (#826) or an unrelated tool/log blob — the
    output terminated on its own, which a bare ``{`` (brace-bearing prose, a
    narrated command snippet, a truncated object) does not prove.
    """
    return bool(_scan_embedded_objects(text or ""))


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
        except RecursionError:
            # Deeply nested untrusted output exhausts the decoder's recursion
            # limit at this `{`. Step PAST the whole balanced over-deep object in
            # ONE O(N) pass and resume the scan after it, so a valid review printed
            # AFTER a pathological blob is still recovered (the `want` path's whole
            # point). Not a per-brace retry: `RecursionError` carries no `.pos`, so
            # re-decoding from each of the blob's N interior braces would be O(N^2)
            # — a hang on the very input this guards. If the over-deep object is
            # unterminated (truncated), nothing parseable can follow inside it, so
            # stop.
            end = _skip_balanced_object(text, index)
            if end is None:
                break
            index = text.find("{", end)
            continue
        if isinstance(candidate, dict):
            found.append((candidate, end - index))
        index = text.find("{", end)
    return found


def _skip_balanced_object(text: str, start: int) -> int | None:
    """The index just past the balanced ``{…}`` object beginning at ``text[start]``,
    or ``None`` if it is unterminated (truncated) — how :func:`_scan_embedded_objects`
    steps over an object too deeply nested for the JSON decoder to parse
    (:class:`RecursionError`) WITHOUT re-decoding it.

    A single O(N) forward pass tracking brace DEPTH, honoring JSON string literals
    (a ``{`` or ``}`` inside a string is text, not structure) and backslash
    escapes (so an escaped quote does not end a string), returning the index after
    the ``}`` that brings depth back to zero. Because the scan resumes AFTER that
    index, the passes over successive over-deep blobs never overlap — total skip
    work across a whole extraction stays O(N), never the O(N^2) a per-brace retry
    would cost.
    """
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return None
