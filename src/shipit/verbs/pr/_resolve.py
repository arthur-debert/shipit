"""Resolve the target PR for a `shipit pr` subcommand — the SHARED helper.

Every `pr` verb takes an optional PR number; omitted, it means "the PR for the
current branch". This module is the single place that turns that into a number,
so WS05/WS06's verbs (`review`, `next`, `ready`) reuse the exact same resolution
and error semantics rather than each re-implementing the branch lookup.

The boundary is `shipit.prstate.ghapi` — `gh pr view --json number` for the
current branch. Three outcomes, kept DISTINCT so callers never have to swallow
errors to find them (the WS04 review caught this conflation):

  * an explicit / resolved PR number  -> returned as an ``int``
  * the branch genuinely has no PR    -> ``None`` (a normal state, not an error)
  * a real gh/auth failure            -> raises ``ghapi.GhError``

The no-PR case is teased apart from a genuine failure by ``gh``'s own signal:
on a branch with no PR, ``gh pr view`` exits non-zero with empty stdout and the
stderr line ``no pull requests found for branch "<name>"`` (carried into the
``GhError`` message). That maps to ``None``; every other failure (missing gh,
auth, transient API error) keeps its ``GhError`` so a verb can surface it as a
clean stderr + non-zero exit per the PRD. A read-only verb maps ``None`` to
``no_pr``; a mutating verb treats both ``None`` and ``GhError`` as fatal — but
each decides, because the cases arrive distinct.
"""

from __future__ import annotations

import json

from ...prstate import ghapi

# gh's wording when the current branch has no associated PR. This is the
# normal "no PR yet" state, NOT a gh/auth failure — so it resolves to None
# rather than propagating as a GhError.
_NO_PR_MARKER = "no pull requests found for branch"


def resolve_pr(pr: int | None) -> int | None:
    """The given PR number, or the current branch's PR (``None`` if there is none).

    ``pr`` passed through untouched when explicit. Otherwise asks ``gh`` for the
    current branch's PR number; returns ``None`` when the branch genuinely has
    no PR. Raises :class:`ghapi.GhError` when ``gh`` itself fails (missing gh,
    auth, transient API error) — never collapses that into ``None``.
    """
    if pr is not None:
        return pr
    try:
        out = ghapi._gh(["pr", "view", "--json", "number"])
    except ghapi.GhError as exc:
        # "no PR for this branch" is a normal state, not a failure: gh exits
        # non-zero with this marker. Anything else is a real gh/auth failure.
        if _NO_PR_MARKER in str(exc):
            return None
        raise
    if not out.strip():
        # Defensive: a non-erroring empty body also means no PR.
        return None
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ghapi.GhError(f"unparseable `gh pr view` output: {exc}") from exc
    number = data.get("number")
    return int(number) if number is not None else None
