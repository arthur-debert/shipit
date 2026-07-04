"""`shipit pr push-gate` — the pre-push classification tripwire (#423).

An exit-code engine verb wired into the synced pre-push hook (lefthook): it
blocks a push — exit 1 with the same CLASSIFY message `pr next`/`pr status`
report — while the current branch's PR has unclassified findings in its LATEST
review round, and passes (exit 0) in every other state: no PR for the branch,
no review round yet, an empty round, a fully classified round, or the
round-cap stop (at the cap there is no further round for a verdict to decide —
the same exception the engine's gate makes).

This is the fail-FAST seam, not the arbiter: `--no-verify` and non-Tree clones
slip past any git hook, which is exactly why the `pr next`/`pr status` gate
(:mod:`shipit.prstate.state`) is the authoritative one — this verb just moves
the same refusal to act-time so the shepherd learns about a missed
classification at the push, not a round-trip later. It trips at most once per
round: verdicts are durable (the dev-cycle event log), so once the round is
classified the gate passes for good.

Hook posture: the verb FAILS OPEN (exit 0, WARNING logged) on any inability to
evaluate — no gh auth, network down, outside a checkout — because a broken
read path must never block git; the authoritative seam still holds the loop.
Deliberately NOT wrapped in the CLI error shell: the shell's uniform
``error:`` + exit 1 is the exact opposite of this posture (the hook canon —
hook verbs own their exit contract).
"""

from __future__ import annotations

import logging
import sys

import click

from ...gh import resolve_pr
from ...identity import Repo
from ...prstate.breakers import build_rounds, evaluate_breakers, unclassified_findings
from ...prstate.fetch import gather
from ...prstate.reviewers_config import load_roster
from ...prstate.state import classify_action
from .._context import ambient_identity
from .._errors import KNOWN_ERRORS

logger = logging.getLogger("shipit.prstate")


@click.command(name="push-gate")
def cmd() -> None:
    """Block the push while the PR's latest round has unclassified findings.

    The pre-push tripwire (#423): resolves the current branch's PR, exits 1
    with the CLASSIFY message while the latest review round has findings with
    no recorded verdict, exits 0 in every other state (no PR, no round, no
    findings, fully classified, round-cap reached) — and fails OPEN when the
    PR state cannot be read at all, so a gh outage never blocks git.
    """
    raise SystemExit(run())


def run(*, repo: Repo | None = None) -> int:
    """Resolve → gather → check the latest round's verdicts. Exit-code only.

    ``repo`` is injectable for tests; the CLI path resolves the ambient one.
    Returns 1 ONLY on the real trip (unclassified findings in the latest
    round, below the round cap), with the shared
    :func:`~shipit.prstate.state.classify_action` message on stderr — the
    identical wording the engine's gate reports, one prose, two seams.
    Everything else returns 0: the pass states trivially, and any
    infrastructure failure (the known runtime error set) fails OPEN with a
    WARNING, per the hook canon.
    """
    try:
        target = resolve_pr(None, *ambient_identity(repo))
        if target is None:
            return 0  # no PR for this branch — nothing to gate
        ctx = gather(target, load_roster())
        if evaluate_breakers(ctx).breaker == "round-cap":
            return 0  # the mechanical stop owns the cap; no verdict can matter
        rounds = build_rounds(ctx)
        if not rounds:
            return 0  # no review round yet
        unclassified = unclassified_findings(rounds[-1], ctx.verdicts)
    except KNOWN_ERRORS:
        # Fail OPEN: the tripwire is fail-fast UX; the `pr next`/`pr status`
        # gate is the arbiter. A push must never be blocked by a broken read.
        logger.warning(
            "push-gate could not evaluate the PR state; failing open",
            exc_info=True,
        )
        return 0
    if not unclassified:
        return 0
    print(
        f"push blocked (pr#{target.number}): "
        + classify_action(target.number, len(unclassified), len(ctx.open_threads()))
        + "\n(the `pr next`/`pr status` gate enforces this either way; "
        "classify, then push again)",
        file=sys.stderr,
    )
    return 1
