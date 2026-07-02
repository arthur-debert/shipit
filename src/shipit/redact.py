"""redact — the central redactor: exact-value registry + pattern rules.

ADR-0028/0029: redaction is central and fail-safe. Two layers:

- **Registered values** — the secrets layer registers every value it fetches
  (:func:`register`); a registered value is masked EXACTLY wherever it appears.
  (Wiring ``secretsrc`` to call :func:`register` at fetch time is LOG01-WS02's
  slice; the registry seam lives here so the Exec runner and the logging chain
  share one redactor.)
- **Pattern rules** — GitHub-minted token shapes and PEM private-key blocks,
  catching inherited tokens nobody registered.

Everything the Exec runner (:mod:`shipit.execrun`) logs or attaches to a raised
error passes through :func:`redact`; the JSONL logging chain (LOG01) attaches
the same function as a processor so masking behavior can never diverge between
sinks and errors.
"""

from __future__ import annotations

import re

#: The placeholder a masked secret is replaced with.
MASK = "***"

#: Compiled pattern rules, applied to every text passed through :func:`redact`:
#: GitHub token shapes (PAT / OAuth / user / server / refresh, plus fine-grained
#: ``github_pat_``) and PEM private-key/certificate blocks (BEGIN…END, any label).
_PATTERNS = (
    re.compile(r"gh[posru]_[A-Za-z0-9]+|github_pat_[A-Za-z0-9_]+"),
    re.compile(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.DOTALL),
)

#: A registered value shorter than this is IGNORED: masking one- or two-char
#: fragments would shred ordinary text (fail-safe means masking secrets, not
#: destroying the record's legibility). Real secrets are never this short.
_MIN_VALUE_LEN = 4

#: The exact secret values registered so far (process-lifetime; secrets are
#: fetched once per process and never un-become secret).
_registered: set[str] = set()


def register(value: str | None) -> None:
    """Register an exact secret ``value`` to be masked by every :func:`redact` call.

    ``None``, empty, and too-short values are ignored (see :data:`_MIN_VALUE_LEN`)
    so a degenerate registration can never blank out the record.
    """
    if value and len(value) >= _MIN_VALUE_LEN:
        _registered.add(value)


def redact(text: str) -> str:
    """Mask every registered value and every pattern match in ``text``.

    Registered values are replaced longest-first so a value that contains
    another registered value is masked whole, never left half-recognizable.
    """
    for value in sorted(_registered, key=len, reverse=True):
        text = text.replace(value, MASK)
    for pattern in _PATTERNS:
        text = pattern.sub(MASK, text)
    return text
