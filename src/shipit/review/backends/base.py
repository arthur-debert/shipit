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
  * :func:`diagnose_parse_failure` — say WHICH non-delivery this was (timed out /
    silent / narrated-instead-of-answered / truncated), so the remediation matches
    the actual fault instead of always blaming diff size (issue #1006);
  * :class:`BackendUnavailable` — the agent binary is not on PATH, or the reviewer
    is configured with a model the backend declares unusable for a review Run
    (both preflight refusals; issue #1006);
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

#: The remediation for a SIZE/LATENCY failure — a review that was cut off before it
#: finished. Attached ONLY to a timeout or a started-then-truncated body, never to a
#: response that never began emitting a verdict (issue #1006: this advice was given
#: for a 4-file docs diff, where speed was never the problem, and sent the operator
#: chasing diff size while the real fault was an unusable model).
_SIZE_HINT = "try a faster model or a smaller diff"


class BackendUnavailable(RuntimeError):
    """The backend cannot review as configured — a PREFLIGHT refusal, raised before
    any Tree is provisioned or any model bills, with a message that names the fix.

    Two causes share this surface because they share a remedy shape ("change
    something, then re-run"): the agent binary is not reachable (install / start /
    upgrade the agent), or the reviewer is configured with a model this backend
    DECLARES unusable for a review Run (issue #1006 — edit the roster's ``model``).
    The service maps it to a ``failed`` funnel outcome carrying the message, so a
    misconfigured reviewer says exactly what is wrong instead of degrading into a
    generic "no parseable JSON" after the fact."""


class BackendError(RuntimeError):
    """A backend ran but produced output we couldn't turn into a review.

    Raised when ``extract_json`` can't parse the agent's stdout (truncated /
    non-JSON output — commonly an agent timeout). Subclasses ``RuntimeError``
    so ``_LocalReviewAdapter.request`` already normalizes it to ``PrStateError``
    (clean error + exit 1, never a raw traceback).

    Carries the FULL raw agent stdout on ``raw`` (the message itself keeps only a
    head/tail snippet, the PR-surface budget). The service layer reads ``raw`` to
    SALVAGE content-but-unparseable output as a top-level review comment (#76) —
    so the agent's prose isn't dropped just because its JSON was truncated.

    Carries a STRUCTURED ``timed_out`` flag (not a string match): the service
    layer splits the degraded funnel outcome ``timed_out`` vs ``empty`` on this
    attribute alone, so a timeout settles ``timed_out`` even when the human-facing
    message paraphrases the timeout instead of echoing :data:`_TIMEOUT_MARKER`
    verbatim. ``timed_out`` may be set explicitly at the raise site (the robust
    path — e.g. a nonzero child whose timeout signal is in *stderr*, not the
    salvageable stdout ``raw``); when left ``None`` it is auto-derived from the
    message + ``raw`` so a marker-bearing output is still classed as a timeout."""

    def __init__(
        self, *args: object, raw: str = "", timed_out: bool | None = None
    ) -> None:
        super().__init__(*args)
        #: The full raw agent stdout (empty when there was nothing to salvage).
        self.raw = raw
        if timed_out is None:
            haystack = f"{' '.join(str(a) for a in args)}\n{raw}".lower()
            timed_out = _TIMEOUT_MARKER in haystack
        #: True when this failure is a TIMEOUT (-> funnel ``timed_out``), False
        #: when it is a generic unparseable/empty non-delivery (-> ``empty``).
        self.timed_out = timed_out


def diagnose_parse_failure(raw: str, *, backend_name: str, timed_out: bool) -> str:
    """The SPECIFIC reason an agent's stdout yielded no review — the failure's own
    diagnosis, not one catch-all guess (issue #1006).

    The five non-delivery modes are genuinely different faults with different fixes,
    and conflating them is what made a dead reviewer read as a slow one for two days:

      * **timed out** — the backend's own timeout marker is present: the response
        was cut off mid-flight, so size/latency IS the lever (:data:`_SIZE_HINT`);
      * **silent** — nothing on stdout at all: not a size problem; the run produced
        no response whatsoever (a killed child, a failed login);
      * **narrated** — no verdict was ever begun and nothing parsed: the agent
        answered in English instead of emitting the verdict. This is the #1006
        signature (an agent that goes agentic in ``--print`` narrates its
        tool-hunting and never answers) and is emphatically NOT a size or latency
        fault — a faster model or a smaller diff cannot fix a model that does not
        answer at all, so that advice is deliberately WITHHELD here and the real
        levers (the reviewer's configured model; whether the review task reached
        it) are named instead;
      * **off-shape** — a COMPLETE JSON object was emitted but it is not the
        ``{summary, comments}`` envelope: a wrong-shaped verdict (#826) or an
        unrelated tool/log blob. The body terminated, so size/latency is NOT the
        lever either — the fix is the reviewer's output contract;
      * **truncated** — the envelope was begun and the output stopped mid-body: a
        genuine cut-off, where size/latency advice is honest.

    Which of the last three applies is decided by the extractor
    (:func:`shipit.review.schema.classify_json_attempt`), which knows what a
    verdict ATTEMPT looks like — NOT by the presence of a ``{``, since narration,
    command snippets and tool JSON all carry braces while delivering no verdict.

    Pure — a string in, a hint out; the caller owns the raising and logging.
    """
    from ..schema import classify_json_attempt

    if timed_out:
        return (
            f"{backend_name} timed out before returning a complete review — "
            f"{_SIZE_HINT}"
        )
    if not raw.strip():
        return (
            f"{backend_name} returned NO output at all — no review was produced. "
            "This is not a diff-size or latency problem: check that the agent is "
            "logged in and that its process was not killed."
        )
    attempt = classify_json_attempt(raw)
    if attempt == "none":
        return (
            f"{backend_name} NARRATED instead of reviewing: it returned prose and "
            "never emitted the required JSON verdict (no JSON object was started). "
            "This is NOT a size or latency problem — a faster model or a smaller "
            "diff will not fix a model that does not answer at all. A model that "
            "goes agentic in headless `--print` mode does exactly this: it "
            "describes what it would do instead of answering. Check the reviewer's "
            "configured model, and that the review task reached the agent."
        )
    if attempt == "off_shape":
        return (
            f"{backend_name} returned COMPLETE JSON that is not a review: no "
            "`{summary, comments}` envelope was found (a wrong-shaped verdict, or "
            "only unrelated tool/log JSON). This is NOT a size or latency problem "
            "— the output terminated, it just does not match the contract. Check "
            "that the reviewer was given the review schema and that its response "
            "is the verdict itself, not a report about one; "
            "`shipit review validate` checks a verdict against the schema."
        )
    return (
        f"{backend_name} returned JSON that could not be parsed — the verdict was "
        f"started but stops mid-body (truncated); {_SIZE_HINT}"
    )


def parse_review_output(stdout: str, *, backend_name: str = "the agent") -> dict:
    """Parse an agent's stdout into a review dict, or raise :class:`BackendError`.

    Wraps :func:`shipit.review.schema.extract_json` (which still raises a
    bare ``ValueError`` on unparseable input) at the backend boundary, turning
    that into an actionable :class:`BackendError`: it includes a head/tail
    snippet of the raw output for debugging and a hint DIAGNOSED from the raw
    itself (:func:`diagnose_parse_failure`) — timed out vs silent vs narrated-
    instead-of-answered vs truncated — so the remediation fits the actual fault
    and only a genuine cut-off is blamed on size/latency (issue #1006).

    ``backend_name`` names the calling backend (e.g. ``"codex"`` / ``"agy"``) so
    the hint blames the RIGHT backend — this function is shared by every backend,
    so a hardcoded name would mislabel a different backend's failure.
    """
    # Local import: schema is a sibling, but keeping it here avoids any chance
    # of an import-order issue and matches the lazy style used elsewhere.
    from ..schema import extract_json, is_review_shaped

    raw = stdout or ""
    try:
        # `want=is_review_shaped`: among the objects in a noisy stdout, select the
        # real `{summary, comments}` review — never a larger unrelated JSON blob,
        # which (no `comments`) would settle downstream as a silent clean pass.
        review = extract_json(stdout, want=is_review_shaped)
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
        timed_out = _TIMEOUT_MARKER in raw.lower()
        hint = diagnose_parse_failure(
            raw, backend_name=backend_name, timed_out=timed_out
        )
        # Attach the full raw so the service can SALVAGE it (#76); the message keeps
        # only the snippet (the PR-surface / terminal budget). The STRUCTURED
        # ``timed_out`` flag (not a string match) is what the service splits the
        # funnel outcome on.
        raise BackendError(
            f"{hint}\nraw output: {snippet}", raw=raw, timed_out=timed_out
        ) from exc
    # Parse OK. Log the full raw at DEBUG — the always-on audit trail (#75) of what
    # the agent actually emitted, durable in the file sink for every run.
    logger.debug(
        "review parsed for %s — agent returned %d chars; full raw stdout follows:\n%s",
        backend_name,
        len(raw),
        raw,
    )
    return review
