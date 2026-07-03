"""Resolve the target PR for a `shipit pr` subcommand — the SHARED helper.

Every `pr` verb takes an optional PR number; omitted, it means "the PR for the
current branch". This module is the single place that turns that into the typed
:class:`~shipit.pr.PrId` target (ADR-0030), so every verb (`status`, `review`,
`next`, `ready`) reuses the exact same resolution and error semantics rather
than each re-implementing the branch lookup.

The resolver MINTS the PrId at the verb boundary: the repo half comes from the
root context (the verb passes :meth:`RootContext.require_repo`'s ambient
identity — resolved once per invocation, offline per ADR-0024), the number half
is the explicit argument or the current branch's PR. From here down the target
travels typed — the pr-family services take the PrId, and none of them
re-derives the repo per fetch.

The branch lookup boundary is the gh adapter (`shipit.gh.pr_number_probe`) —
`gh pr view --json number` for the current branch. Three outcomes, kept
DISTINCT so callers never have to swallow
errors to find them (the WS04 review caught this conflation):

  * an explicit / resolved PR number  -> minted into a ``PrId``
  * the branch genuinely has no PR    -> ``None`` (a normal state, not an error)
  * a real gh/auth failure            -> raises ``execrun.ExecError``

The no-PR case is teased apart from a genuine failure by ``gh``'s own signal:
on a branch with no PR, ``gh pr view`` exits non-zero with empty stdout and the
stderr line ``no pull requests found for branch "<name>"``. That maps to
``None``; every other failure (missing gh, auth, transient API error) becomes
an ``ExecError`` so a verb can surface it as a clean stderr + non-zero exit per
the PRD. The lookup runs as a PROBE (``check=False`` through the Exec runner):
a nonzero exit is this call's normal answer on every PR-less branch, so it
records at DEBUG, never as a spurious ERROR (ADR-0028). A read-only verb maps
``None`` to ``no_pr``; a mutating verb treats both ``None`` and ``ExecError``
as fatal — but each decides, because the cases arrive distinct.
"""

from __future__ import annotations

import json

from ... import execrun, gh
from ...identity import Repo
from ...pr import PrId
from ...prstate.errors import PrStateError


def resolve_pr(number: int | None, repo: Repo) -> PrId | None:
    """The typed PR target: the given number, or the current branch's PR
    (``None`` if there is none) — minted into a :class:`~shipit.pr.PrId`.

    ``repo`` is the identity half of the target — the verb's ambient repo from
    the root context (or an explicit override), never re-derived here. An
    explicit ``number`` is minted directly. Otherwise asks ``gh`` for the
    current branch's PR number; returns ``None`` when the branch genuinely has
    no PR. Raises :class:`shipit.execrun.ExecError` when ``gh`` itself fails
    (missing gh, auth, transient API error) — never collapses that into
    ``None``.
    """
    if number is not None:
        return PrId(repo=repo, number=number)
    result = gh.pr_number_probe()
    if result.rc != 0:
        # "no PR for this branch" is a normal state, not a failure: gh exits
        # non-zero with the adapter's NO_PR_MARKER (per-tool knowledge written
        # down once, in the adapter). Anything else is a real gh/auth failure —
        # surface the failed Exec as its transport error (pre-redacted by the
        # ExecError constructor), never collapse it into None.
        if gh.NO_PR_MARKER in result.stderr.lower():
            return None
        raise execrun.ExecError(
            result.argv,
            rc=result.rc,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_ms=result.duration_ms,
        )
    out = result.stdout
    if not out.strip():
        # Defensive: a non-erroring empty body also means no PR.
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise PrStateError(f"unparseable `gh pr view` output: {exc}") from exc
    resolved = data.get("number")
    if resolved is None:
        return None
    # Pass the raw wire value straight into PrId — its construction IS the
    # validation (exact-int, positive, ADR-0030), the same discipline the
    # sibling wire boundary `pr.core_from_node` applies. A silent `int(resolved)`
    # coercion here would defeat that invariant, accepting a `"99"`/`7.0`/`True`
    # from unexpected `gh` output and minting the wrong PR target. Re-raise with
    # the wire context so a malformed number dies at this read, like the JSON
    # decode above.
    try:
        return PrId(repo=repo, number=resolved)
    except ValueError as exc:
        raise PrStateError(f"malformed `gh pr view` number: {exc}") from exc
