"""Resolve the target PR for a `shipit pr` subcommand — the SHARED helper.

Every `pr` verb takes an optional PR number; omitted, it means "the PR for the
current branch". This module is the single place that turns that into a number,
so WS05/WS06's verbs (`review`, `next`, `ready`) reuse the exact same resolution
and error semantics rather than each re-implementing the branch lookup.

The boundary is `shipit.prstate.ghapi` — `gh pr view --json number` for the
current branch. A `gh`/auth failure raises `ghapi.GhError`; the caller decides
whether that is fatal (a verb that mutates must error) or benign (a read-only
status line treats it as "no PR"). We do NOT swallow the error here: collapsing
a real gh/auth failure into a silent "no PR" would mask the cause behind a
misleading message — the verb owns that policy.
"""

from __future__ import annotations

import json

from ...prstate import ghapi


def resolve_pr(pr: int | None) -> int | None:
    """The given PR number, or the current branch's open PR (``None`` if none).

    ``pr`` passed through untouched when explicit. Otherwise asks ``gh`` for the
    current branch's PR number; returns ``None`` when the branch genuinely has
    no PR. Raises :class:`ghapi.GhError` when ``gh`` itself fails (missing gh,
    auth, transient API error) — never collapses that into ``None``.
    """
    if pr is not None:
        return pr
    try:
        data = json.loads(ghapi._gh(["pr", "view", "--json", "number"]))
    except json.JSONDecodeError as exc:
        raise ghapi.GhError(f"unparseable `gh pr view` output: {exc}") from exc
    number = data.get("number")
    return int(number) if number is not None else None
