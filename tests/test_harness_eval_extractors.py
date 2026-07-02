"""Objective extractors: a fixture transcript -> the expected metric values.

Every case asserts EXTERNAL behavior — events (or a fixture JSONL transcript) in,
the expected metric out — never the parser's internals. The four PRD fixtures
(clean / stuck / no-verify / break-glass) live as builders here, and each extractor
gets its own table-driven case.
"""

from __future__ import annotations

import json

import pytest
from shipit.harness.eval import extractors
from shipit.execrun import ExecError
from shipit.harness.eval.extractors import (
    break_glass_count,
    error_count,
    exit_hygiene,
    extract,
    no_verify_count,
    retry_count,
    stuck_loop,
    token_usage,
    tool_call_count,
    tool_call_vector,
    turn_count,
)

# --------------------------------------------------------------------------- #
# Event builders (the fixture vocabulary)
# --------------------------------------------------------------------------- #


def _assistant(*blocks, usage=None, msg_id=None):
    message = {"role": "assistant", "content": list(blocks)}
    if usage is not None:
        message["usage"] = usage
    if msg_id is not None:
        message["id"] = msg_id
    return {"type": "assistant", "message": message}


def _user(*blocks):
    return {"type": "user", "message": {"role": "user", "content": list(blocks)}}


def _tool_use(name, **inp):
    return {"type": "tool_use", "id": f"toolu_{name}", "name": name, "input": inp}


def _tool_result(*, is_error=False):
    return {"type": "tool_result", "tool_use_id": "toolu_x", "is_error": is_error}


def _bash(command):
    return _tool_use("Bash", command=command)


# --------------------------------------------------------------------------- #
# tool-call vector / count
# --------------------------------------------------------------------------- #


def test_tool_call_vector_counts_per_tool():
    events = [
        _user("hi"),
        _assistant({"type": "text", "text": "look"}, _tool_use("Read", file="a")),
        _assistant(_tool_use("Bash", command="ls"), _tool_use("Read", file="b")),
    ]
    assert tool_call_vector(events) == {"Read": 2, "Bash": 1}
    assert tool_call_count(events) == 3


def test_tool_call_count_is_zero_for_a_toolless_run():
    events = [_user("hi"), _assistant({"type": "text", "text": "no tools"})]
    assert tool_call_vector(events) == {}
    assert tool_call_count(events) == 0


def test_tool_call_vector_ignores_events_without_list_content():
    events = [
        {"type": "summary", "summary": "x"},
        {"attachment": {"type": "deferred_tools_delta"}},
        {"message": {"role": "user", "content": "string content"}},
        {"message": "not-a-mapping"},
        _assistant(_tool_use("Read", file="a")),
    ]
    assert tool_call_vector(events) == {"Read": 1}


# --------------------------------------------------------------------------- #
# turn count
# --------------------------------------------------------------------------- #


def test_turn_count_counts_assistant_messages():
    events = [_user("go"), _assistant(_tool_use("Read")), _user("more"), _assistant()]
    assert turn_count(events) == 2


def test_turn_count_dedupes_parts_sharing_a_message_id():
    # One response delivered in two parts (same id) is one turn.
    events = [
        _assistant({"type": "text", "text": "a"}, msg_id="m1"),
        _assistant(_tool_use("Bash"), msg_id="m1"),
        _assistant(_tool_use("Read"), msg_id="m2"),
    ]
    assert turn_count(events) == 2


# --------------------------------------------------------------------------- #
# stuck-loop fingerprints
# --------------------------------------------------------------------------- #


def test_stuck_loop_clean_run_is_not_flagged():
    events = [
        _assistant(_bash("ls")),
        _assistant(_bash("pytest")),
        _assistant(_tool_use("Read", file="a")),
    ]
    result = stuck_loop(events)
    assert result["detected"] is False
    assert result["max_repeated_calls"] == 1
    assert result["max_turn_iterations"] == 0


def test_stuck_loop_flags_same_tool_args_repeated_more_than_twice():
    # The exact same call three times WITHIN ONE TURN (>2) trips the repeated-call
    # fingerprint — the signal is per-turn (one assistant message's content list).
    events = [_assistant(_bash("pytest -q"), _bash("pytest -q"), _bash("pytest -q"))]
    result = stuck_loop(events)
    assert result["max_repeated_calls"] == 3
    assert result["detected"] is True


def test_stuck_loop_is_per_turn_not_across_the_run():
    # The same call once per turn across many turns is NORMAL (e.g. `Bash pytest`
    # every turn). The repeated-call signal resets per turn, so this is not stuck.
    events = [_assistant(_bash("pytest -q")) for _ in range(5)]
    result = stuck_loop(events)
    assert result["max_repeated_calls"] == 1
    assert result["detected"] is False


def test_stuck_loop_does_not_flag_same_tool_with_different_args():
    # One turn with the same call twice + a third different call → max repeat 2,
    # at the >2 threshold but not over it, so not stuck.
    events = [_assistant(_bash("ls"), _bash("ls"), _bash("pwd"))]
    result = stuck_loop(events)
    assert result["max_repeated_calls"] == 2
    assert result["detected"] is False


def test_stuck_loop_flags_runaway_turn_iterations():
    # A single turn whose usage.iterations ran >8 trips the runaway signal.
    events = [_assistant(_bash("x"), usage={"iterations": [{}] * 9})]
    result = stuck_loop(events)
    assert result["max_turn_iterations"] == 9
    assert result["detected"] is True


def test_stuck_loop_eight_iterations_is_not_yet_flagged():
    events = [_assistant(_bash("x"), usage={"iterations": [{}] * 8})]
    assert stuck_loop(events)["detected"] is False


# --------------------------------------------------------------------------- #
# retry count
# --------------------------------------------------------------------------- #


def test_retry_count_counts_back_to_back_identical_calls():
    events = [
        _assistant(_bash("pytest")),
        _assistant(_bash("pytest")),  # retry of the previous
        _assistant(_bash("pytest")),  # retry again
        _assistant(_bash("ls")),  # different — resets
    ]
    assert retry_count(events) == 2


def test_retry_count_ignores_repeats_that_are_not_adjacent():
    events = [
        _assistant(_bash("ls")),
        _assistant(_bash("pwd")),
        _assistant(_bash("ls")),
    ]
    assert retry_count(events) == 0


# --------------------------------------------------------------------------- #
# check-bypass / break-glass greps
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "command,expected",
    [
        ("git commit -m x", 0),
        ("git commit --no-verify -m x", 1),
        ("git push --no-verify", 1),
        ("lefthook run --no-hooks", 1),
    ],
)
def test_no_verify_count_detects_check_bypass(command, expected):
    assert no_verify_count([_assistant(_bash(command))]) == expected


def test_no_verify_count_one_per_call_even_with_multiple_markers():
    assert (
        no_verify_count([_assistant(_bash("git commit --no-verify --no-hooks"))]) == 1
    )


@pytest.mark.parametrize(
    "command,expected",
    [
        ("SHIPIT_BREAK_GLASS=1 shipit install --push", 1),
        ("SHIPIT_BREAK_GLASS=true git commit", 1),
        ("SHIPIT_BREAK_GLASS=0 git commit", 0),  # disarmed
        ("SHIPIT_BREAK_GLASS=false git commit", 0),
        ("SHIPIT_BREAK_GLASS=FALSE git commit", 0),  # case-insensitive falsey
        ("git commit -m normal", 0),
        # Value at the END of the command: the input serializes to
        # `{"command": "SHIPIT_BREAK_GLASS=0"}`, so the capture must STOP at the
        # closing quote/brace (`0`, not `0"}`) — else a disarmed use miscounts as armed.
        ("SHIPIT_BREAK_GLASS=0", 0),
        ("SHIPIT_BREAK_GLASS=1", 1),
    ],
)
def test_break_glass_count_counts_armed_uses_only(command, expected):
    assert break_glass_count([_assistant(_bash(command))]) == expected


def test_break_glass_count_one_per_call():
    cmd = "SHIPIT_BREAK_GLASS=1 a && SHIPIT_BREAK_GLASS=1 b"
    assert break_glass_count([_assistant(_bash(cmd))]) == 1


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #


def test_error_count_counts_errored_tool_results():
    events = [
        _assistant(_bash("bad")),
        _user(_tool_result(is_error=True)),
        _assistant(_bash("ok")),
        _user(_tool_result(is_error=False)),
    ]
    assert error_count(events) == 1


# --------------------------------------------------------------------------- #
# token totals
# --------------------------------------------------------------------------- #


def test_token_usage_sums_across_turns():
    events = [
        _assistant(
            _bash("x"),
            usage={
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 5,
                "cache_creation_input_tokens": 7,
            },
        ),
        _assistant(
            _bash("y"),
            usage={"input_tokens": 50, "output_tokens": 10},
        ),
    ]
    assert token_usage(events) == {
        "input_tokens": 150,
        "output_tokens": 30,
        "cache_read_tokens": 5,
        "cache_creation_tokens": 7,
        "total_tokens": 180,
    }


def test_token_usage_is_none_when_nothing_logged():
    assert token_usage([_assistant(_bash("x"))]) is None


def test_token_usage_dedupes_streamed_parts_sharing_a_message_id():
    # Two streamed parts of ONE response share a message id and each carry the same
    # usage block; usage is consumed once per id (not summed per event), mirroring
    # turn_count — otherwise a single turn's tokens would double-count.
    usage = {"input_tokens": 100, "output_tokens": 20}
    events = [
        _assistant({"type": "text", "text": "a"}, usage=usage, msg_id="m1"),
        _assistant(_bash("x"), usage=usage, msg_id="m1"),
    ]
    assert token_usage(events) == {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "total_tokens": 120,
    }


def test_token_usage_ignores_non_assistant_usage():
    # A usage block on a non-assistant message is not summed (role-guarded), so a
    # transcript carrying only that logs no usage at all.
    events = [
        {"type": "user", "message": {"role": "user", "usage": {"input_tokens": 99}}}
    ]
    assert token_usage(events) is None


# --------------------------------------------------------------------------- #
# extract() over a real JSONL transcript file (the four PRD fixtures)
# --------------------------------------------------------------------------- #


def _write_transcript(tmp_path, events, name="agent-x.jsonl"):
    transcript = tmp_path / name
    transcript.write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )
    return transcript


def test_extract_clean_run(tmp_path):
    events = [_user("go"), _assistant(_bash("ls"), _tool_use("Read", file="a"))]
    metrics = extract(_write_transcript(tmp_path, events))
    assert metrics["tool_call_count"] == 2
    assert metrics["tool_call_vector"] == {"Bash": 1, "Read": 1}
    assert metrics["turn_count"] == 1
    assert metrics["stuck_loop"]["detected"] is False
    assert metrics["no_verify_count"] == 0
    assert metrics["break_glass_count"] == 0
    assert metrics["error_count"] == 0
    assert metrics["retry_count"] == 0
    assert metrics["token_usage"] is None


def test_extract_stuck_run(tmp_path):
    # Four identical calls WITHIN ONE TURN — the per-turn repeated-call signal —
    # which also reads as three back-to-back retries in the flat call sequence.
    events = [_assistant(*[_bash("pytest -q") for _ in range(4)])]
    metrics = extract(_write_transcript(tmp_path, events))
    assert metrics["stuck_loop"]["detected"] is True
    assert metrics["stuck_loop"]["max_repeated_calls"] == 4
    assert metrics["retry_count"] == 3


def test_extract_no_verify_run(tmp_path):
    events = [_assistant(_bash("git commit --no-verify -m wip"))]
    metrics = extract(_write_transcript(tmp_path, events))
    assert metrics["no_verify_count"] == 1
    assert metrics["break_glass_count"] == 0


def test_extract_break_glass_run(tmp_path):
    events = [_assistant(_bash("SHIPIT_BREAK_GLASS=1 shipit install --push"))]
    metrics = extract(_write_transcript(tmp_path, events))
    assert metrics["break_glass_count"] == 1


def test_extract_tolerates_blank_and_malformed_lines(tmp_path):
    transcript = tmp_path / "agent-x.jsonl"
    lines = [
        json.dumps(_user("go")),
        "",  # blank — tolerated
        "{ not json",  # malformed — skipped
        json.dumps(_assistant(_bash("ls"), _tool_use("Read", file="a"))),
    ]
    transcript.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert extract(transcript)["tool_call_count"] == 2


def test_extract_on_missing_file_yields_empty_metrics(tmp_path):
    metrics = extract(tmp_path / "nope.jsonl")
    assert metrics["tool_call_count"] == 0
    assert metrics["tool_call_vector"] == {}
    assert metrics["token_usage"] is None


# --------------------------------------------------------------------------- #
# exit-hygiene (the one live check — git via the gh boundary, PID seam injected)
# --------------------------------------------------------------------------- #


def test_exit_hygiene_clean_worktree(monkeypatch):
    monkeypatch.setattr(extractors.gh, "git_status_porcelain", lambda *, cwd: "")
    result = exit_hygiene("/repo")
    assert result == {
        "worktree_clean": True,
        "dirty_file_count": 0,
        "stray_pid_count": 0,
    }


def test_exit_hygiene_dirty_worktree(monkeypatch):
    porcelain = " M src/a.py\n?? scratch.txt\nUU conflicted.py\n"
    monkeypatch.setattr(extractors.gh, "git_status_porcelain", lambda *, cwd: porcelain)
    result = exit_hygiene("/repo")
    assert result["worktree_clean"] is False
    assert result["dirty_file_count"] == 3


def test_exit_hygiene_counts_injected_stray_pids(monkeypatch):
    monkeypatch.setattr(extractors.gh, "git_status_porcelain", lambda *, cwd: "")
    result = exit_hygiene("/repo", list_stray_pids=lambda: [101, 202])
    assert result["stray_pid_count"] == 2


def test_exit_hygiene_degrades_on_git_failure(monkeypatch):
    def _boom(*, cwd):
        raise ExecError(["gh"], rc=1, stderr="not a git repo")

    monkeypatch.setattr(extractors.gh, "git_status_porcelain", _boom)
    result = exit_hygiene("/repo")
    assert result["worktree_clean"] is None
    assert result["dirty_file_count"] is None
    assert result["stray_pid_count"] == 0
