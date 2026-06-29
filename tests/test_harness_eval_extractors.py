"""Objective extractors: a fixture transcript -> the expected metric values.

WS01's one metric is the tool-call count — the number of `tool_use` blocks across
the run's assistant messages. Tested against the external behavior (events in →
count out), never the parser internals.
"""

from __future__ import annotations

import json

from shipit.harness.eval.extractors import extract, tool_call_count


def _assistant(*blocks):
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": list(blocks)},
    }


def _tool_use(name):
    return {"type": "tool_use", "id": f"toolu_{name}", "name": name, "input": {}}


def test_tool_call_count_sums_tool_use_blocks_across_messages():
    events = [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        _assistant({"type": "text", "text": "let me look"}, _tool_use("Read")),
        _assistant(_tool_use("Bash"), _tool_use("Edit")),
    ]
    assert tool_call_count(events) == 3


def test_tool_call_count_is_zero_for_a_toolless_run():
    events = [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        _assistant({"type": "text", "text": "done, no tools"}),
    ]
    assert tool_call_count(events) == 0


def test_tool_call_count_ignores_events_without_list_content():
    # Attachments, summaries, string-content turns contribute nothing and never raise.
    events = [
        {"type": "summary", "summary": "x"},
        {"attachment": {"type": "deferred_tools_delta"}},
        {"message": {"role": "user", "content": "string content"}},
        {"message": "not-a-mapping"},
        _assistant(_tool_use("Read")),
    ]
    assert tool_call_count(events) == 1


def test_extract_reads_a_jsonl_transcript_file(tmp_path):
    transcript = tmp_path / "agent-x.jsonl"
    lines = [
        json.dumps({"type": "user", "message": {"role": "user", "content": "go"}}),
        "",  # blank line — tolerated
        "{ not json",  # malformed line — skipped, not fatal
        json.dumps(_assistant(_tool_use("Bash"), _tool_use("Read"))),
    ]
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert extract(transcript) == {"tool_call_count": 2}


def test_extract_on_missing_file_yields_zero(tmp_path):
    assert extract(tmp_path / "nope.jsonl") == {"tool_call_count": 0}
