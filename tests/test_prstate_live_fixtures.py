"""Engine behaviour over REAL captured gh payloads (release#337).

These fixtures were recorded live from arthur-debert/release#342 — a throwaway
probe PR driven through actual Copilot + Gemini reviews — not hand-written. They
pin the engine against the real shapes GitHub returns: bot login variants
(`copilot-pull-request-reviewer`, `gemini-code-assist`), the empty
`reviewRequests` even when Copilot is engaged, GraphQL thread node ids, and the
resolved-thread transition to READY.
"""

from __future__ import annotations

from shipit.prstate.reviewers import by_name
from shipit.prstate.state import TaskState, evaluate

# These payloads were captured before CodeRabbit was added as a second required
# reviewer (release#622), so they carry only Copilot + Gemini. Drive the engine
# with the required SET that was in effect then — just Copilot — which is itself
# the data-driven config the parallel-required design rests on: the same engine,
# a different required set, no code change.
_COPILOT_ONLY = [by_name("copilot")]


def test_live_addressing_real_payload(context):
    status = evaluate(context("live_addressing_pr342"), required=_COPILOT_ONLY)
    assert status.state is TaskState.ADDRESSING
    # Both bots reviewed and left a comment; real login variants matched.
    assert status.reviewers == {
        "copilot": "done_comments",
        "coderabbit": "not_requested",
        "gemini": "done_comments",
        "codex": "not_requested",
        "agy": "not_requested",
    }
    assert status.open_threads == 2
    assert status.cycles == 1
    # The REAL Copilot findings carry no severity anywhere, so they resolve
    # through Copilot's `minor` unclassified policy (#743): a Copilot-only
    # unclassified round fires the no-major+ stop — both threads still resolve
    # before Ready (state stays ADDRESSING above), but no further round is
    # minted. Pre-#743 these findings rode the `major` fail-safe, this breaker
    # could never fire on a Copilot-commenting PR, and loops rode to the cap.
    assert status.breaker == "no-major-finding"


def test_live_ready_real_payload(context):
    # Same PR after replying + resolving both threads — drives straight to
    # READY: every finding's severity resolves off the chain (ADR-0044), so
    # resolved threads + green checks + a clean merge state ARE the
    # done-signal; no recorded verdict exists or is needed.
    ctx = context("live_ready_pr342")
    status = evaluate(ctx, required=_COPILOT_ONLY)
    assert status.state is TaskState.READY
    assert status.open_threads == 0
    assert status.checks.value == "green"
    assert status.mergeable == "MERGEABLE"
