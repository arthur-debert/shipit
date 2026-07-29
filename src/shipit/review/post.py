"""Map a review JSON to one GitHub grouped-review payload and post it.

An unanchored inline comment would 422 the WHOLE review, so such findings fold
into the summary body instead.
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

#: The minted installation token is never passed to a record.
logger = logging.getLogger("shipit.review")

_STATUS_TO_EVENT = {
    "APPROVED": "APPROVE",
    "REQUEST_CHANGES": "REQUEST_CHANGES",
    "COMMENT": "COMMENT",
}


def commentable_lines(diff: str) -> dict[str, set[int]]:
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
            target = raw[4:].strip()
            if target == "/dev/null":
                path = None
            else:
                path = target[2:] if target.startswith(("a/", "b/")) else target
                result.setdefault(path, set())
            in_hunk = False
            continue
        if raw.startswith("@@"):
            new_line = _parse_hunk_new_start(raw)
            in_hunk = path is not None
            continue
        if not in_hunk or path is None:
            continue
        if raw.startswith("\\"):
            continue
        if raw.startswith("-"):
            continue
        result[path].add(new_line)
        new_line += 1

    return result


def _parse_hunk_new_start(header: str) -> int:
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
    """Empty when the attestation carries nothing, and total over malformed
    input: an agent may emit any shape here."""
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
    """Findings order highest severity first, ``event`` overrides the summary's
    status, and ``commit_id`` pins the head sha so moved lines are not rejected."""
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
        "commit_id": str(ctx.head_sha),
        "event": resolved_event,
        "body": body,
    }
    if comments:
        payload["comments"] = comments
    return payload


def _resolve_repo(ctx: ReviewView) -> str:
    if ctx.repo:
        return ctx.repo
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
    """POSTs unless ``dry_run``, authenticated as the backend's GitHub App when
    ``as_app``, so GitHub attributes the review to the bot; a dry run mints no token."""
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
        # The ExecError is pre-redacted, so no token can ride this record.
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
