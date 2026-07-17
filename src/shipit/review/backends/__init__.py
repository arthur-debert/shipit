"""backends — the review-output parse boundary + error vocabulary.

Since TRE05-WS04b the funnel has NO per-backend CLI wrappers of its own. The codex
/ agy launch is driven through the shared spawn ``BackendAdapter`` reviewer posture
(:mod:`shipit.spawn.backends`) by :mod:`shipit.review.producer`, which captures the
agent's stdout. This package is now only the thin parse boundary that turns that
stdout into a review dict — :func:`~shipit.review.backends.base.parse_review_output`
— plus the error vocabulary (:class:`BackendError` / :class:`BackendUnavailable`,
and :func:`~shipit.review.backends.base.diagnose_parse_failure`, which names WHICH
non-delivery a parse failure was) the service layer maps to funnel check-run
outcomes. The old ``get_backend`` registry
and the ``Backend`` ABC are retired (the front-loaded ``codex`` / ``agy`` ``run()``
path is gone — ADR-0020 §Reviewer-path reconciliation, REPLACE).
"""

from __future__ import annotations

from .base import (
    BackendError,
    BackendUnavailable,
    diagnose_parse_failure,
    parse_review_output,
)

__all__ = [
    "BackendError",
    "BackendUnavailable",
    "diagnose_parse_failure",
    "parse_review_output",
]
