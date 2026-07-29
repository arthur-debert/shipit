"""The review-backend output-parsing boundary and its error vocabulary."""

from __future__ import annotations

import logging

logger = logging.getLogger("shipit.review")

#: How much of the raw agent output to echo back in a parse-failure message.
_SNIPPET = 200
#: agy prints this when its ``--print`` timeout fires mid-response.
_TIMEOUT_MARKER = "timed out waiting for response"

#: The remediation for a size/latency failure. Attached ONLY to an explicit
#: timeout, never inferred from the output's shape.
_SIZE_HINT = "try a faster model or a smaller diff"


class BackendUnavailable(RuntimeError):
    """The backend's agent binary is not reachable; the message says how to remediate."""


class BackendError(RuntimeError):
    """A backend ran but produced output we couldn't turn into a review.

    ``raw`` carries the full agent stdout for salvage (the message keeps only a
    snippet). ``timed_out`` is structured rather than string-matched, and may be
    set at the raise site when the timeout signal was on stderr, not in ``raw``.
    """

    def __init__(
        self, *args: object, raw: str = "", timed_out: bool | None = None
    ) -> None:
        super().__init__(*args)
        self.raw = raw
        if timed_out is None:
            haystack = f"{' '.join(str(a) for a in args)}\n{raw}".lower()
            timed_out = _TIMEOUT_MARKER in haystack
        self.timed_out = timed_out


def _diagnose_parse_failure(raw: str, *, backend_name: str, timed_out: bool) -> str:
    """The reason an agent's stdout yielded no review, claimed only on evidence
    the raw carries — so only an explicit timeout earns the size/latency hint.
    """
    from ..schema import has_complete_json_object

    if timed_out:
        return (
            f"{backend_name} timed out before returning a complete review — "
            f"{_SIZE_HINT}"
        )
    if not raw.strip():
        return (
            f"{backend_name} returned no output at all — no review was produced; "
            "check that the agent is logged in and that its process was not killed"
        )
    if has_complete_json_object(raw):
        return (
            f"{backend_name} returned complete JSON that is not a review: no "
            "`{summary, comments}` envelope was found (a wrong-shaped verdict, or "
            "only unrelated tool/log JSON). Check that the response is the verdict "
            "itself, not a report about one; `shipit review validate` checks a "
            "verdict against the schema"
        )
    return (
        f"{backend_name} returned no review verdict — the output is prose or "
        "incomplete JSON with no `{summary, comments}` envelope; inspect the raw "
        "output to see what the agent returned instead"
    )


def parse_review_output(stdout: str, *, backend_name: str = "the agent") -> dict:
    """Parse an agent's stdout into a review dict, or raise :class:`BackendError`."""
    from ..schema import extract_json, is_review_shaped

    raw = stdout or ""
    try:
        # Select the real review among the objects in a noisy stdout: a larger
        # unrelated blob would carry no `comments` and settle as a clean pass.
        review = extract_json(stdout, want=is_review_shaped)
    except ValueError as exc:
        snippet = (
            f"{raw[:_SNIPPET]} … {raw[-_SNIPPET:]}" if len(raw) > 2 * _SNIPPET else raw
        )
        # Snippet on every surface, full raw only at DEBUG: the raw must not dump
        # to a terminal or CI job log, but the file sink keeps it.
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
        timed_out = _TIMEOUT_MARKER in raw.lower()
        hint = _diagnose_parse_failure(
            raw, backend_name=backend_name, timed_out=timed_out
        )
        raise BackendError(
            f"{hint}\nraw output: {snippet}", raw=raw, timed_out=timed_out
        ) from exc
    logger.debug(
        "review parsed for %s — agent returned %d chars; full raw stdout follows:\n%s",
        backend_name,
        len(raw),
        raw,
    )
    return review
