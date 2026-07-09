"""post — map a review JSON to a GitHub "create review" payload and post it.

Take the structured review a backend produced (the
:data:`shipit.review.schema.REVIEW_SCHEMA` shape) and turn it into a single
GitHub *grouped review* — one ``POST /repos/{owner}/{repo}/pulls/{n}/reviews``
carrying a summary body plus inline comments anchored to changed lines.

Two functions, kept separable so the payload build is unit-testable without any
network:

* :func:`build_review_payload` — pure data transform (review + ReviewView →
  GitHub payload dict). No I/O.
* :func:`post_review` — builds the payload and (unless ``dry_run``) POSTs it via
  the :mod:`shipit.gh` boundary, AS the agent's GitHub App when ``as_app``.

Two GitHub constraints shape the mapping:

* **Self-review** — GitHub returns 422 if you ``APPROVE`` / ``REQUEST_CHANGES``
  your OWN PR. The local-review path posts AS the agent's bot (a different
  identity), so APPROVE/REQUEST_CHANGES is allowed; a caller can still pass
  ``event="COMMENT"`` to force a comment-only review.
* **Diff-line anchoring** — an inline comment whose ``(file, line)`` is not part
  of the PR diff makes GitHub 422 the WHOLE review. We parse the diff once
  (:func:`commentable_lines`) and fold any unanchored finding into the review
  body instead of emitting it as an inline comment.

Each inline comment body is the :mod:`shipit.finding` two-layer rendering — the
invisible machine marker carrying the exact severity/category/confidence tuple
plus the Conventional Comments human layer — so the PR state engine can recover
each finding's severity from the comment body alone (ADR-0044). The retired
``Agent: <name> [SEVERITY]`` prefix is gone; findings are ordered highest
severity first.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping

from .. import execrun, gh
from ..agent.backend import Backend
from ..finding import (
    CONVENTIONAL_PREFIXES,
    FIX_LABEL,
    order_findings,
    render_comment,
)
from . import ghauth
from .diff import ReviewView
from .schema import finding_from_dict

#: The review-post logger — a child of the package ``shipit`` logger. The post
#: (target, identity, outcome) is recorded at DEBUG/INFO; the minted installation
#: token is NEVER passed to a record — only the boolean ``as_app`` fact is.
logger = logging.getLogger("shipit.review")

# Map the review summary.status enum → GitHub review `event`.
_STATUS_TO_EVENT = {
    "APPROVED": "APPROVE",
    "REQUEST_CHANGES": "REQUEST_CHANGES",
    "COMMENT": "COMMENT",
}


def commentable_lines(diff: str) -> dict[str, set[int]]:
    """Parse a unified diff → ``{path: set of RIGHT-side (new-file) line numbers}``.

    GitHub only accepts an inline review comment on a line that is part of the
    diff on the side you anchor to. We anchor every comment to the RIGHT (new)
    side, so the commentable positions are the new-file line numbers of the
    *added* and *context* lines in each hunk. Removed (``-``) lines exist only on
    the LEFT side and are excluded.
    """
    result: dict[str, set[int]] = {}
    path: str | None = None
    new_line = 0
    in_hunk = False

    for raw in diff.splitlines():
        if raw.startswith("diff --git"):
            path = None
            in_hunk = False
            continue
        if raw.startswith("+++ "):
            # "+++ b/path" (or "+++ /dev/null" for a deletion). Strip the b/.
            target = raw[4:].strip()
            if target == "/dev/null":
                path = None
            else:
                path = target[2:] if target.startswith(("a/", "b/")) else target
                result.setdefault(path, set())
            in_hunk = False
            continue
        if raw.startswith("@@"):
            # @@ -old_start,old_count +new_start,new_count @@
            new_line = _parse_hunk_new_start(raw)
            in_hunk = path is not None
            continue
        if not in_hunk or path is None:
            continue
        if raw.startswith("\\"):
            # "\ No newline at end of file" — not a content line.
            continue
        if raw.startswith("-"):
            # removed line: LEFT side only, no RIGHT-side number consumed.
            continue
        # added ("+") or context (" " / empty) line: both occupy a RIGHT-side
        # new-file line number, and GitHub accepts a comment on either.
        result[path].add(new_line)
        new_line += 1

    return result


def _parse_hunk_new_start(header: str) -> int:
    """Extract ``new_start`` from a ``@@ -a,b +c,d @@`` hunk header (defaults 1)."""
    for token in header.split():
        if token.startswith("+"):
            spec = token[1:]
            start = spec.split(",", 1)[0]
            try:
                return int(start)
            except ValueError:
                return 1
    return 1


def _coverage_section(coverage: object) -> str:
    """Render the summary's coverage attestation as a human-facing body section:
    what was reviewed, what was skipped and why — so silence means "clean," not
    "skipped". Empty when the attestation carries nothing (the salvage and
    dry-run paths build summaries without one).

    TOTAL over malformed input: the agy path has no native schema enforcement and
    ``extract_json`` does no validation, so an agent may emit any shape here. A
    non-dict ``coverage``, a non-list ``reviewed``/``skipped``, or a non-dict
    ``skipped`` entry is ignored rather than crashing the whole review post."""
    if not isinstance(coverage, dict):
        return ""
    raw_reviewed = coverage.get("reviewed")
    reviewed = (
        [str(entry) for entry in raw_reviewed] if isinstance(raw_reviewed, list) else []
    )
    raw_skipped = coverage.get("skipped")
    skipped = (
        [entry for entry in raw_skipped if isinstance(entry, dict)]
        if isinstance(raw_skipped, list)
        else []
    )
    if not reviewed and not skipped:
        return ""
    lines = ["### Coverage"]
    if reviewed:
        lines.append("Reviewed: " + ", ".join(f"`{entry}`" for entry in reviewed))
    for entry in skipped:
        file = entry.get("file", "?")
        reason = entry.get("reason", "")
        lines.append(f"Skipped: `{file}` — {reason}")
    return "\n".join(lines)


def build_review_payload(
    review: dict,
    ctx: ReviewView,
    *,
    agent_name: str,
    event: str | None = None,
) -> dict:
    """Map a review JSON (``REVIEW_SCHEMA`` shape) to a GitHub create-review payload.

    Returns the body for ``POST /repos/{owner}/{repo}/pulls/{n}/reviews``:

    * ``commit_id`` is pinned to ``ctx.head_sha`` — anchoring to the head sha
      avoids GitHub rejecting comments whose lines moved since an earlier sha.
    * ``event`` is derived from ``review["summary"]["status"]`` UNLESS ``event``
      is passed explicitly, in which case the override wins.
    * ``body`` is a ``Agent: <name>`` header line followed by the summary's
      ``overall_feedback`` and its coverage attestation (when carried). Any
      finding NOT anchored to a changed diff line is appended here under a
      "Findings not anchored to changed lines" section rather than emitted as an
      inline comment (an unanchored inline comment would 422 the entire review).
    * ``comments[]`` holds one ``{path, line, side: "RIGHT", body}`` entry per
      finding whose ``(file, line)`` IS a commentable RIGHT-side diff position.
      Each body is the :mod:`shipit.finding` two-layer rendering (machine marker
      + Conventional Comments layer); findings are ordered highest severity
      first in both the inline list and the unanchored fold.
    """
    summary = review.get("summary") or {}
    status = summary.get("status", "COMMENT")
    overall_feedback = summary.get("overall_feedback", "")

    resolved_event = (
        event if event is not None else _STATUS_TO_EVENT.get(status, "COMMENT")
    )

    anchorable = commentable_lines(ctx.diff)

    findings = order_findings(
        finding_from_dict(raw)
        for raw in review.get("comments") or []
        if isinstance(raw, Mapping)
    )

    comments: list[dict] = []
    unanchored: list[str] = []
    for finding in findings:
        is_anchored = finding.line is not None and finding.line in anchorable.get(
            finding.file, set()
        )
        if is_anchored:
            comments.append(
                {
                    "path": finding.file,
                    "line": finding.line,
                    "side": "RIGHT",
                    "body": render_comment(finding),
                }
            )
        else:
            snippet = f"\n\n```\n{finding.evidence}\n```" if finding.evidence else ""
            fix = f"\n\n{FIX_LABEL} {finding.fix}" if finding.fix else ""
            location = (
                f"{finding.file}:{finding.line}"
                if finding.line is not None
                else finding.file
            )
            prefix = CONVENTIONAL_PREFIXES[finding.severity]
            unanchored.append(f"- `{location}` {prefix} {finding.text}{snippet}{fix}")

    body = f"Agent: {agent_name}\n\n{overall_feedback}".rstrip()
    coverage = _coverage_section(summary.get("coverage"))
    if coverage:
        body += f"\n\n{coverage}"
    if unanchored:
        body += "\n\n### Findings not anchored to changed lines:\n" + "\n".join(
            unanchored
        )

    payload: dict = {
        # The wire payload carries the string form of the typed head `Sha` (COR02).
        "commit_id": str(ctx.head_sha),
        "event": resolved_event,
        "body": body,
    }
    if comments:
        payload["comments"] = comments
    return payload


def _resolve_repo(ctx: ReviewView) -> str:
    """The ``OWNER/NAME`` slug to POST to: ``ctx.repo`` if set, else inferred via
    ``gh repo view``. Raises a clear error if it can't be determined."""
    if ctx.repo:
        return ctx.repo
    # The typed adapter read (PROC03): `gh.current_repo()` returns the
    # :class:`~shipit.identity.Repo`; this wire-facing seam hands the REST path
    # builders the slug string. Its `ValueError` is the empty/unusable
    # `gh repo view` answer — normalized like the transport failure.
    try:
        return gh.current_repo().slug
    except execrun.ExecError as exc:
        raise RuntimeError(
            "Could not determine the repository to post the review to: ctx.repo is "
            f"unset and `gh repo view` failed ({exc}). Pass --repo OWNER/NAME."
        ) from exc
    except ValueError as exc:
        raise RuntimeError(
            "Could not determine the repository to post the review to (unusable "
            f"`gh repo view` result: {exc}). Pass --repo OWNER/NAME."
        ) from exc


def post_review(
    review: dict,
    ctx: ReviewView,
    *,
    backend: Backend,
    event: str | None = None,
    dry_run: bool = False,
    as_app: bool = False,
) -> dict:
    """Build the grouped-review payload and (unless ``dry_run``) POST it.

    With ``dry_run=True``: prints the payload as pretty JSON and returns it,
    WITHOUT calling ``gh`` and minting NO token — safe to run anywhere. When
    ``as_app`` is also set, it notes the review would be authored by the
    backend's funnel login (``adr-<agent>-review[bot]``, off the registry).

    With ``as_app=True`` (and not dry-run): authenticates AS the backend's GitHub
    App installation — mints a 1-hour installation token via
    :mod:`shipit.review.ghauth` (Doppler-sourced PEM → in-memory RS256 JWT →
    installation token) and passes it to ``gh.rest(..., token=…)`` so GitHub
    attributes the review to the bot instead of the user's own ``gh`` login.
    With ``as_app=False`` posts as the user via the normal ``gh`` auth.

    Raises ``RuntimeError`` on a ``gh`` / auth failure with an actionable message.
    """
    agent_name = backend.funnel_agent or backend.name
    payload = build_review_payload(review, ctx, agent_name=agent_name, event=event)

    if dry_run:
        logger.info(
            "review post dry-run for pr#%s on %s (event=%s, as_app=%s) — not posting",
            ctx.number,
            ctx.repo,
            payload.get("event"),
            as_app,
            extra={"pr": ctx.number, "repo": ctx.repo},
        )
        print(json.dumps(payload, indent=2))
        if as_app:
            print(f"(dry-run: would post as {backend.funnel_login})")
        return payload

    repo = _resolve_repo(ctx)

    token: str | None = None
    if as_app:
        # Mint a 1-hour installation token to author the review AS the bot. The
        # token value never reaches a log record — only the fact that we are
        # authenticating as the app is recorded.
        logger.debug(
            "review post authenticating as the %r GitHub App for %s",
            agent_name,
            repo,
        )
        try:
            token = ghauth.installation_token(backend, repo)
        except ghauth.ReviewAuthError as exc:
            raise RuntimeError(
                f"Could not authenticate as the {agent_name!r} GitHub App to post "
                f"to {repo}#{ctx.number}: {exc}"
            ) from exc

    path = f"/repos/{repo}/pulls/{ctx.number}/reviews"
    logger.info(
        "review posting to pr#%s on %s (event=%s, as_app=%s)",
        ctx.number,
        repo,
        payload.get("event"),
        as_app,
        extra={"pr": ctx.number, "repo": repo},
    )
    try:
        response = gh.rest(path, method="POST", body=payload, token=token)
    except execrun.ExecError as exc:
        # A propagating failure (glassbox spray): the post is the review's whole
        # point, so its failure records at ERROR with the exception attached —
        # the ExecError is pre-redacted, so the token can never ride this record.
        logger.error(
            "review post to pr#%s on %s failed",
            ctx.number,
            repo,
            exc_info=True,
            extra={"pr": ctx.number, "repo": repo},
        )
        raise RuntimeError(
            f"Failed to post review to {repo}#{ctx.number}: {exc}"
        ) from exc
    logger.info(
        "review posted to pr#%s on %s",
        ctx.number,
        repo,
        extra={"pr": ctx.number, "repo": repo},
    )
    return response if isinstance(response, dict) else {"response": response}
