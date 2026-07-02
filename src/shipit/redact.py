"""The central redactor (ADR-0028/0029): every log record is masked here.

Two rules, one seam:

- **Exact-value masking.** :mod:`shipit.secretsrc` registers every fetched
  secret value at fetch time (:func:`register_secret`); a registered value can
  then never appear in any rendered record, on any sink. This is the guarantee
  no off-the-shelf package can offer (ADR-0029 records the survey) — the app
  knows its own secrets, so it masks them exactly rather than guessing.
- **Pattern masking.** Compiled shapes for secrets that arrive from OUTSIDE the
  secretsrc boundary (a token pasted into an error message, a PEM block read
  off disk): GitHub-minted token prefixes and PEM-armored blocks.

Both are applied by :func:`redact_event`, the processor slotted into
``logsetup._PIPELINE`` — the ONE chain every sink shares — so everything
logged, file JSONL and stderr alike, passes through here before rendering.

The registry is process-lifetime module state, deliberately: secrets are
fetched once and must stay masked for the rest of the process, across any
number of ``configure_logging`` calls. :func:`clear_registered_secrets` is the
test seam.
"""

from __future__ import annotations

import re
import threading
from collections.abc import MutableMapping
from typing import Any

#: The placeholder a masked secret is replaced with (matches the existing
#: convention in :mod:`shipit.gh`).
MASK = "***"

#: Compiled shapes for secrets that never pass through secretsrc. GitHub token
#: prefixes (PAT / OAuth / user / installation / refresh, plus fine-grained
#: ``github_pat_``) and PEM-armored blocks (private keys, certs — the armor
#: lines and everything between them go, as one mask).
_PATTERNS = (
    re.compile(r"gh[posru]_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+"),
    re.compile(r"-----BEGIN [A-Z0-9 ]+-----.*?-----END [A-Z0-9 ]+-----", re.DOTALL),
)

#: Every secret value fetched this process — exact strings, masked verbatim.
#: An immutable tuple, pre-sorted longest-first at registration time: the hot
#: read path (:func:`redact_text`, every string field of every record) never
#: sorts and never iterates a mutating collection — it reads one immutable
#: snapshot. Writes are rare (once per fetched secret) and lock-guarded.
_REGISTRY: tuple[str, ...] = ()
_REGISTRY_LOCK = threading.Lock()


def register_secret(value: str | None) -> None:
    """Register a fetched secret value for exact masking in every log record.

    Called by :mod:`shipit.secretsrc` at fetch time — the one moment the
    application provably holds a secret. Empty / ``None`` / whitespace-only
    values are ignored (nothing to mask; registering ``""`` or ``" "`` would
    mangle every record).
    """
    global _REGISTRY
    if not value or not value.strip():
        return
    with _REGISTRY_LOCK:
        if value not in _REGISTRY:
            _REGISTRY = tuple(sorted({*_REGISTRY, value}, key=len, reverse=True))


def clear_registered_secrets() -> None:
    """Reset the registry — a test seam, never called in production."""
    global _REGISTRY
    with _REGISTRY_LOCK:
        _REGISTRY = ()


def redact_text(text: str) -> str:
    """Mask every registered value and every pattern match in ``text``.

    Registered values are replaced longest-first, so a secret that contains
    another registered secret as a substring is masked whole rather than
    leaving its distinctive remainder behind. The registry snapshot is
    immutable and pre-sorted, so this loop is safe against a concurrent
    :func:`register_secret` and does no per-call sorting.
    """
    for value in _REGISTRY:
        text = text.replace(value, MASK)
    for pattern in _PATTERNS:
        text = pattern.sub(MASK, text)
    return text


def redact_event(
    logger: object, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """The redaction processor (ADR-0028/0029) in ``logsetup._PIPELINE``.

    Runs after enrichment and before rendering, on every record, for every
    sink. String values (``event``/msg, extras, the flattened ``exception``)
    are masked in place. A non-scalar value (a bound object that a renderer
    would later stringify) is checked via BOTH representations a downstream
    renderer may use — ``repr`` (the file sink's ``_flatten_to_scalars``) and
    ``str`` (the human surface's ``f"{k}={v}"``): if masking changes either,
    the value degrades to the masked repr string — so a secret can never ride
    an object past the redactor into any renderer. Clean values keep their
    type untouched.
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
