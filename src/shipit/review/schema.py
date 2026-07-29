"""The review JSON schema, a tolerant extractor for it, and the trust boundary
from an agent's unvalidated comment dict to a typed :class:`~shipit.finding.Finding`.
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
    return value if isinstance(value, str) else ""


def finding_from_dict(raw: dict) -> Finding:
    """The trust boundary: every field coerces to a domain type or a fail-safe."""
    line = raw.get("line")
    confidence = raw.get("confidence")
    return Finding(
        severity=resolve_severity(adapter=parse_severity(raw.get("severity"))),
        text=_str_field(raw.get("text")),
        file=_str_field(raw.get("file")),
        # `bool` is an `int` subclass: `line: true` must not anchor at line 1.
        line=line if isinstance(line, int) and not isinstance(line, bool) else None,
        category=_str_field(raw.get("category")),
        confidence=(
            float(confidence)
            if isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
            else None
        ),
        evidence=_str_field(raw.get("evidence")),
        fix=_str_field(raw.get("fix")),
    )


_STATUS_VALUES: tuple[str, ...] = tuple(
    REVIEW_SCHEMA["properties"]["summary"]["properties"]["status"]["enum"]
)
_SEVERITY_VALUES: tuple[str, ...] = tuple(severity.value for severity in Severity)


def _is_str(value: object) -> bool:
    return isinstance(value, str)


def _is_int_not_bool(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number_not_bool(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_object(
    value: object, path: str, required: tuple[str, ...], problems: list[str]
) -> dict | None:
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
    if key in obj and not predicate(obj[key]):
        problems.append(f"{path}.{key}: expected {expected}, got {_typename(obj[key])}")


def _typename(value: object) -> str:
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
    return isinstance(payload.get("summary"), dict) and isinstance(
        payload.get("comments"), list
    )


def _accepted(value: object, want: Callable[[dict], bool] | None) -> bool:
    if not isinstance(value, dict):
        return False
    return want is None or want(value)


def extract_json(text: str, *, want: Callable[[dict], bool] | None = None) -> dict:
    """Direct, then de-fenced, then the largest embedded object ``want``
    accepts; raises when nothing is."""
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
    return bool(_scan_embedded_objects(text or ""))


def _scan_embedded_objects(text: str) -> list[tuple[dict, int]]:
    """As ``(object, source length)``; the real decoder parses each, so nested
    braces are never rescanned as candidates."""
    decoder = json.JSONDecoder()
    found: list[tuple[dict, int]] = []
    index = text.find("{")
    while index != -1:
        try:
            candidate, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError as exc:
            index = text.find("{", max(exc.pos, index + 1))
            continue
        except RecursionError:
            # One O(N) pass: a per-brace retry would be O(N^2), since
            # `RecursionError` carries no `.pos`.
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
    """``None`` when the object is unterminated; one O(N) pass, no re-decoding."""
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
