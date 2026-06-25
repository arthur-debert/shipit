"""Thread-state completeness — the release#515 / release#455 regressions.

The REST `/pulls/{n}/comments` fetch surfaced only a subset of inline comments
(3 of 6 on release#502) and an exact-string author filter
(`copilot-pull-request-reviewer[bot]` vs the comment-author rendering
`Copilot`) silently returned empty. The snapshot is now built from GraphQL
`reviewThreads` only, so these tests pin:

  - every inline comment on the PR surfaces in the context (no REST subset),
  - both Copilot login variants match through the adapter,
  - threads from ANY author (second bots, humans) count in the open-thread
    accounting and block the done-signal.

The `multi_bot_threads` fixture mirrors the #502 shape: 6 inline comments
across 5 threads from Copilot (both login variants), CodeRabbit, and a human.
"""

from __future__ import annotations

from dataclasses import replace

from shipit.prstate.model import ReviewLifecycle
from shipit.prstate.reviewers import CopilotAdapter
from shipit.prstate.state import TaskState, evaluate

COPILOT = CopilotAdapter()


def all_comments(ctx):
    return [c for t in ctx.threads for c in t.comments]


def test_snapshot_surfaces_all_inline_comments(context):
    # The REST-missed regression: every known inline comment is in the
    # snapshot, regardless of author or thread resolution state.
    ctx = context("multi_bot_threads")
    assert sorted(c.comment_id for c in all_comments(ctx)) == [101, 102, 103, 104, 105, 106]


def test_copilot_login_variants_both_match(context):
    # Review login is `copilot-pull-request-reviewer[bot]`; comment authors
    # render as `Copilot` AND `copilot-pull-request-reviewer` — the adapter's
    # tolerant match must catch all of them (release#455).
    ctx = context("multi_bot_threads")
    assert COPILOT.matches("Copilot")
    assert COPILOT.matches("copilot-pull-request-reviewer[bot]")
    assert COPILOT.detect(ctx) is ReviewLifecycle.DONE_COMMENTS
    assert {t.thread_id for t in COPILOT.authored_threads(ctx)} == {
        "PRT_copilot_1",
        "PRT_copilot_2",
        "PRT_copilot_3",
    }
    # Open = authored minus the resolved one.
    assert {t.thread_id for t in COPILOT.open_threads(ctx)} == {
        "PRT_copilot_1",
        "PRT_copilot_2",
    }


def test_second_bot_and_human_threads_count_in_open_threads(context):
    # Thread accounting is reviewer-agnostic: CodeRabbit (not in the adapter
    # registry) and human threads count toward open_threads.
    ctx = context("multi_bot_threads")
    open_ids = {t.thread_id for t in ctx.open_threads()}
    assert "PRT_coderabbit_1" in open_ids
    assert "PRT_human_1" in open_ids
    assert len(open_ids) == 4


def test_unresolved_second_bot_thread_blocks_done(context):
    # Copilot (the only required reviewer) is done, but the PR must NOT read
    # as reviewed/ready while ANY thread is unresolved.
    ctx = context("multi_bot_threads")
    status = evaluate(ctx)
    assert status.state is TaskState.ADDRESSING
    assert status.open_threads == 4


def test_resolving_every_thread_is_the_done_signal(context):
    # The inverse: with ALL threads resolved (same PR otherwise), the snapshot
    # reads READY — "0 unresolved threads" is the trustworthy done-signal.
    ctx = context("multi_bot_threads")
    ctx.threads = [replace(t, is_resolved=True) for t in ctx.threads]
    status = evaluate(ctx)
    assert status.state is TaskState.READY
    assert status.open_threads == 0
