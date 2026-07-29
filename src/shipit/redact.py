"""The central redactor: every log record is masked here, on every sink.

See docs/adr/0029-agents-first-jsonl-logging.md.
"""

from __future__ import annotations

import re
import threading
from collections.abc import MutableMapping
from typing import Any

MASK = "***"

#: A new pattern must name the shipit code path that handles that credential kind.
_PATTERNS = (
    re.compile(r"gh[posru]_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+"),
    re.compile(r"-----BEGIN [A-Z0-9 ]+-----.*?-----END [A-Z0-9 ]+-----", re.DOTALL),
    re.compile(r"dp\.(?:st|pt)\.[A-Za-z0-9._-]+"),
)

_REGISTRY: tuple[str, ...] = ()
_REGISTRY_LOCK = threading.Lock()


def register_secret(value: str | None) -> None:
    """Register a fetched secret value for exact masking; empty or whitespace-only values are ignored, since masking those would mangle every record."""
    global _REGISTRY
    if not value or not value.strip():
        return
    with _REGISTRY_LOCK:
        if value not in _REGISTRY:
            _REGISTRY = tuple(sorted({*_REGISTRY, value}, key=len, reverse=True))


def clear_registered_secrets() -> None:
    global _REGISTRY
    with _REGISTRY_LOCK:
        _REGISTRY = ()


def redact_text(text: str) -> str:
    """Mask every registered value and every pattern match in ``text``; registered values are replaced longest-first, so a secret containing another is masked whole."""
    for value in _REGISTRY:
        text = text.replace(value, MASK)
    for pattern in _PATTERNS:
        text = pattern.sub(MASK, text)
    return text


def redact_event(
    logger: object, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """The redaction processor in ``logsetup._PIPELINE``: masks every record, every sink.

    A non-scalar value is checked through BOTH representations a renderer may use
    (``repr`` and ``str``); if masking changes either it degrades to the masked repr.
    """
    for key, value in event_dict.items():
        if isinstance(value, str):
            event_dict[key] = redact_text(value)
        elif value is not None and not isinstance(value, (int, float, bool)):
            rendered = repr(value)
            masked = redact_text(rendered)
            stringified = str(value)
            if masked != rendered or redact_text(stringified) != stringified:
                event_dict[key] = masked
    return event_dict
