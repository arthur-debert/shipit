"""base — the review-backend output-parsing boundary + error vocabulary.

Since TRE05-WS04b the funnel no longer owns per-backend CLI wrappers: the codex /
agy launch is driven through the shared spawn ``BackendAdapter`` reviewer posture
(:mod:`shipit.spawn.backends`), and the producer (:mod:`shipit.review.producer`)
captures the agent's stdout and feeds it through :func:`parse_review_output` here.
So this module is now just the parse boundary and the error vocabulary the service
layer maps to funnel outcomes:

  * :func:`parse_review_output` — turn an agent's raw stdout into a review dict,
    or raise :class:`BackendError` (carrying the full raw for the #76 salvage and,
    when the agy timeout marker is present, flagging the timeout);
  * :class:`BackendUnavailable` — the agent binary is not on PATH (preflight);
  * :class:`BackendError` — the agent ran but produced no usable review;
  * :data:`_TIMEOUT_MARKER` — the agy ``--print`` timeout signature.
"""

from __future__ import annotations

import logging

#: The review path's logger — a child of the package ``shipit`` logger, so a record
#: here reaches the OBS01 per-repo file sink (DEBUG-verbose). This is the ONE site
#: that sees the agent's full raw stdout on EVERY local-agent run (every backend's
#: ``run`` parses through :func:`parse_review_output`), so the durable raw-output
#: audit trail (#75) is logged here rather than at the per-backend call sites.
logger = logging.getLogger("shipit.review")

#: How much of the raw agent output to echo back in a parse-failure message —
#: enough to see the head and tail (where a truncation marker lives) without
#: dumping a whole review into the terminal.
_SNIPPET = 200
#: agy prints this when its ``--print`` timeout fires mid-response; the output
#: is then a TRUNCATED JSON object followed by the marker, which ``extract_json``
#: can't parse. Detecting it lets the error say "timed out" explicitly.
_TIMEOUT_MARKER = "timed out waiting for response"


class BackendUnavailable(RuntimeError):
    """The backend's agent binary is not reachable — message tells the user how
    to remediate (install / start the agent). Raised by ``preflight``."""


class BackendError(RuntimeError):
    """A backend ran but produced output we couldn't turn into a review.

    Raised when ``extract_json`` can't parse the agent's stdout (truncated /
    non-JSON output — commonly an agent timeout). Subclasses ``RuntimeError``
    so ``_LocalReviewAdapter.request`` already normalizes it to ``GhError``
    (clean error + exit 1, never a raw traceback).

    Carries the FULL raw agent stdout on ``raw`` (the message itself keeps only a
    head/tail snippet, the PR-surface budget). The service layer reads ``raw`` to
    SALVAGE content-but-unparseable output as a top-level review comment (#76) —
    so the agent's prose isn't dropped just because its JSON was truncated."""

    def __init__(self, *args: object, raw: str = "") -> None:
        super().__init__(*args)
        #: The full raw agent stdout (empty when there was nothing to salvage).
        self.raw = raw


def parse_review_output(stdout: str, *, backend_name: str = "the agent") -> dict:
    """Parse an agent's stdout into a review dict, or raise :class:`BackendError`.

    Wraps :func:`shipit.review.schema.extract_json` (which still raises a
    bare ``ValueError`` on unparseable input) at the backend boundary, turning
    that into an actionable :class:`BackendError`: it includes a head/tail
    snippet of the raw output for debugging and, when the agent's timeout marker
    is present, says so explicitly so the user knows to use a faster model or a
    smaller diff.

    ``backend_name`` names the calling backend (e.g. ``"codex"`` / ``"agy"``) so
    the timeout hint blames the RIGHT backend — this function is shared by every
    backend, so a hardcoded name would mislabel a different backend's timeout.
    """
    # Local import: schema is a sibling, but keeping it here avoids any chance
    # of an import-order issue and matches the lazy style used elsewhere.
    from ..schema import extract_json

    raw = stdout or ""
    try:
        review = extract_json(stdout)
    except ValueError as exc:
        snippet = (
            f"{raw[:_SNIPPET]} … {raw[-_SNIPPET:]}" if len(raw) > 2 * _SNIPPET else raw
        )
        # Parse FAILED (#75). The user-facing surfaces (console handler WARNING+, the
        # CI handler) get only the short SNIPPET — the full raw must not dump to a
        # terminal / CI job log. The FULL raw — the durable 'why' a truncation/invalid
        # -JSON failure needs — goes to DEBUG only, which the always-DEBUG OBS01 file
        # sink still captures. So: snippet on every surface, full raw in the file sink.
        logger.warning(
            "review parse failed for %s — agent returned UNPARSEABLE output "
            "(%d chars); snippet: %s",
            backend_name,
            len(raw),
            snippet,
        )
        logger.debug(
            "review parse failed for %s — full raw stdout follows:\n%s",
            backend_name,
            raw,
        )
        if _TIMEOUT_MARKER in raw.lower():
            hint = (
                f"{backend_name} timed out before returning a complete review — "
                "try a faster model or a smaller diff"
            )
        else:
            hint = (
                "the agent returned no parseable JSON (it may have timed out or "
                "been truncated) — try a faster model or a smaller diff"
            )
        # Attach the full raw so the service can SALVAGE it (#76); the message keeps
        # only the snippet (the PR-surface / terminal budget).
        raise BackendError(f"{hint}\nraw output: {snippet}", raw=raw) from exc
    # Parse OK. Log the full raw at DEBUG — the always-on audit trail (#75) of what
    # the agent actually emitted, durable in the file sink for every run.
    logger.debug(
        "review parsed for %s — agent returned %d chars; full raw stdout follows:\n%s",
        backend_name,
        len(raw),
        raw,
    )
    return review
