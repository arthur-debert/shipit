from __future__ import annotations

from dataclasses import replace

from shipit.prstate.model import ReviewLifecycle
from shipit.prstate.reviewers import CopilotAdapter
from shipit.prstate.state import TaskState, evaluate

COPILOT = CopilotAdapter()


def all_comments(ctx):
    return [c for t in ctx.threads for c in t.comments]


def test_snapshot_surfaces_all_inline_comments(context):
    ctx = context("multi_bot_threads")
    assert sorted(c.comment_id for c in all_comments(ctx)) == [
        101,
        102,
        103,
        104,
        105,
        106,
    ]


def test_copilot_login_variants_both_match(context):
    ctx = context("multi_bot_threads")
    assert COPILOT.matches("Copilot")
    assert COPILOT.matches("copilot-pull-request-reviewer[bot]")
    assert COPILOT.detect(ctx) is ReviewLifecycle.DONE_COMMENTS
    assert {t.thread_id for t in COPILOT.authored_threads(ctx)} == {
        "PRT_copilot_1",
        "PRT_copilot_2",
        "PRT_copilot_3",
    }
    assert {t.thread_id for t in COPILOT.open_threads(ctx)} == {
        "PRT_copilot_1",
        "PRT_copilot_2",
    }


def test_second_bot_and_human_threads_count_in_open_threads(context):
    ctx = context("multi_bot_threads")
    open_ids = {t.thread_id for t in ctx.open_threads()}
    assert "PRT_coderabbit_1" in open_ids
    assert "PRT_human_1" in open_ids
    assert len(open_ids) == 4


def test_unresolved_second_bot_thread_blocks_done(context):
    ctx = context("multi_bot_threads")
    status = evaluate(ctx)
    assert status.state is TaskState.ADDRESSING
    assert status.open_threads == 4


def test_resolving_every_thread_is_the_done_signal(context):
    ctx = context("multi_bot_threads")
    ctx.threads = [replace(t, is_resolved=True) for t in ctx.threads]
    status = evaluate(ctx)
    assert status.state is TaskState.READY
    assert status.open_threads == 0
