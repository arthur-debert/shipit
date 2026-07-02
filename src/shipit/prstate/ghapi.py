"""The single boundary to GitHub: shell out to `gh`, parse JSON with the stdlib.

Why `gh` rather than a Python client: `gh` is already provisioned in every
environment the engine runs in (local, Cloud), handles auth + pagination, and
— crucially — speaks GraphQL, where the PR review-thread and resolution data
live. Keeping the boundary here means the rest of the package is pure data
transformation and unit-tests without the network.

Execution routes through the one Exec runner (ADR-0028): every `gh` call is an
Exec via :func:`shipit.execrun.run` with a stated timeout, one structured
record per Exec, and central redaction (:mod:`shipit.redact`) on everything
logged or raised. A failed invocation — nonzero exit, timeout, or a missing
`gh` binary — raises the single transport error
:class:`shipit.execrun.ExecError`; this boundary defines no transport error of
its own (the legacy ``GhError`` is deleted, no alias). The one SEMANTIC error
the engine raises is :class:`shipit.prstate.errors.PrStateError`.
"""

from __future__ import annotations

import json
import logging

from .. import execrun
from .errors import PrStateError

#: The engine's logger (shared name with the rest of ``shipit.prstate``). The
#: TRANSPORT record for every call here is the Exec runner's (one record per
#: Exec, ADR-0028); this boundary only records the one failure the runner can't
#: see — a GraphQL response that completed but carries semantic errors.
logger = logging.getLogger("shipit.prstate")

#: The stated per-Exec timeout (ADR-0028: every Exec carries one). Every call
#: through this boundary talks to GitHub, so each gets the runner's generous
#: network default.
_GH_TIMEOUT: float = execrun.DEFAULT_TIMEOUT


def _gh(args: list[str], *, input_text: str | None = None) -> str:
    """``gh <args>`` through the Exec runner, returning stdout.

    Raises :class:`shipit.execrun.ExecError` on any failure — nonzero exit,
    timeout expiry, or a missing ``gh`` binary (normalized by the runner; no
    ``shutil.which`` pre-check needed).
    """
    return execrun.run(["gh", *args], input=input_text, timeout=_GH_TIMEOUT).stdout


def _gh_probe(args: list[str], *, input_text: str | None = None) -> execrun.ExecResult:
    """``gh <args>`` with ``check=False`` — for probes where a nonzero exit is
    a NORMAL answer (the no-PR read on every ``pr`` verb without an explicit
    number). The Exec records at DEBUG, not ERROR (ADR-0028); the caller
    branches on the result's ``rc``/``stderr``.
    """
    return execrun.run(
        ["gh", *args], input=input_text, check=False, timeout=_GH_TIMEOUT
    )


def rest(
    path: str,
    *,
    paginate: bool = False,
    method: str | None = None,
    fields: dict[str, str] | None = None,
) -> object:
    """Call `gh api <path>` and return parsed JSON (None on empty output)."""
    args = ["api"]
    if method:
        args += ["-X", method]
    if paginate:
        args.append("--paginate")
    for key, value in (fields or {}).items():
        args += ["-f", f"{key}={value}"]
    args.append(path)
    out = _gh(args)
    if not out.strip():
        return None
    if paginate:
        return _merge_paginated(out)
    return json.loads(out)


def _merge_paginated(out: str) -> list:
    """`gh api --paginate` concatenates one JSON array per page; flatten them."""
    merged: list = []
    decoder = json.JSONDecoder()
    text = out.strip()
    idx = 0
    while idx < len(text):
        obj, end = decoder.raw_decode(text, idx)
        merged.extend(obj if isinstance(obj, list) else [obj])
        idx = end
        while idx < len(text) and text[idx] in " \n\r\t":
            idx += 1
    return merged


def graphql(query: str, **variables: object) -> dict:
    """Run one of the ENGINE's own GraphQL queries/mutations; return `data`.

    PURPOSE-BUILT for the engine's cursor/pagination + review-thread queries —
    NOT a general-purpose GraphQL boundary. Two deliberate behaviours make it
    correct for every call the engine makes but surprising to a general caller:

      * a variable whose value is ``None`` is OMITTED entirely (not sent as
        null) — so a first-page ``after: $cursor`` defaults to GraphQL null;
        you cannot pass an *explicit* null through this helper; and
      * a str value is forced as a string via ``-f`` (only int/bool type-infer
        via ``-F``) — required for ``ID!`` variables, which must not be coerced
        to a number.

    A future caller needing explicit-null variables, or float/enum/list
    variables, must not reach for this — build that call against ``gh api
    graphql`` directly. Raises :class:`PrStateError` if the response carries
    errors (a semantic failure: the Exec succeeded, the answer is unusable).
    """
    args = ["api", "graphql", "-f", f"query={query}"]
    for key, value in variables.items():
        # Omit None entirely: an unprovided nullable GraphQL variable defaults
        # to null, which is what a first-page `after: $cursor` wants. Passing
        # it through would send the literal string "None".
        if value is None:
            continue
        # -F type-infers ints/bools; -f forces a string (needed for ID! vars).
        flag = "-F" if isinstance(value, (int, bool)) else "-f"
        args += [flag, f"{key}={value}"]
    payload = json.loads(_gh(args))
    if payload.get("errors"):
        error = PrStateError(f"graphql errors: {payload['errors']}")
        # A propagating semantic failure the Exec record cannot carry (the gh
        # call exited 0): record it at ERROR with the exception attached
        # (glassbox spray) before it leaves this boundary.
        logger.error(
            "gh graphql call returned errors (Exec succeeded, answer unusable)",
            exc_info=error,
        )
        raise error
    return payload["data"]


def pr_edit_reviewer(pr: int, reviewer: str, *, remove: bool = False) -> None:
    """Add (or remove) a reviewer on a PR via ``gh pr edit``.

    ``gh pr edit --add-reviewer`` resolves the reviewer handle to its real
    node id and mutates through GraphQL. That path is load-bearing for bot
    reviewers: the REST ``requested_reviewers`` POST silently no-ops for them
    (returns 200 but leaves ``requested_reviewers`` empty) — never swap this
    for the REST call.
    """
    owner, name = repo_slug()
    flag = "--remove-reviewer" if remove else "--add-reviewer"
    _gh(["pr", "edit", str(pr), "--repo", f"{owner}/{name}", flag, reviewer])


def pr_ready(pr: int, *, undo: bool = False) -> None:
    """Flip a PR's draft flag via ``gh pr ready`` (``--undo`` for ready→draft).

    ``gh pr ready`` is idempotent: flipping a PR that is already in the target
    state prints a notice and exits 0, so callers don't need to pre-check the
    flag to stay safe — they pre-check only to *say* something more useful.
    """
    owner, name = repo_slug()
    args = ["pr", "ready", str(pr), "--repo", f"{owner}/{name}"]
    if undo:
        args.append("--undo")
    _gh(args)


def pr_review_reply(pr: int, comment_id: int, body: str) -> None:
    """Post a threaded reply to an existing PR review comment.

    Wraps ``POST /repos/{owner}/{name}/pulls/{pr}/comments/{comment_id}/replies``
    — the dedicated reply endpoint that threads the new comment under the
    target rather than starting a fresh top-level review comment. ``comment_id``
    is the numeric REST id (the same handle ``pr resolve-thread`` takes); ``body``
    is the reply text. This is the push-back path: reply with rationale, then
    resolve the thread.
    """
    owner, name = repo_slug()
    rest(
        f"repos/{owner}/{name}/pulls/{pr}/comments/{comment_id}/replies",
        method="POST",
        fields={"body": body},
    )


def repo_slug() -> tuple[str, str]:
    """Return (owner, name) for the current repo."""
    data = json.loads(_gh(["repo", "view", "--json", "owner,name"]))
    return data["owner"]["login"], data["name"]


def pr_meta(pr: int) -> dict:
    """PR-level metadata the engine needs in one call.

    Deliberately does NOT fetch ``reviewRequests``: ``gh pr view --json``
    silently omits Bot-typed requested reviewers (a requested Copilot reads as
    ``[]``), so the engine sources requested reviewers from GraphQL instead
    (`fetch._threads_and_review_requests`).
    """
    out = _gh(
        [
            "pr",
            "view",
            str(pr),
            "--json",
            "number,headRefOid,baseRefName,isDraft,mergeable,mergeStateStatus,statusCheckRollup",
        ]
    )
    return json.loads(out)
