"""``harness/eval/extractors`` — objective metrics read from a run's transcript.

Pure over the parsed JSONL events, except the live :func:`exit_hygiene`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from ... import execrun, git
from .. import breakglass

#: Repeats of one (tool, args) fingerprint within ONE turn above this are a stuck loop.
_REPEAT_THRESHOLD = 2

#: ``message.usage.iterations`` above this in one turn is the runaway stuck-loop signal.
_ITERATION_THRESHOLD = 8

_BYPASS_MARKERS = ("--no-verify", "--no-hooks")

#: The break-glass assignment. The value capture stops at whitespace, quotes,
#: braces, and backslashes so a grep over JSON-serialized input keeps no syntax.
_BREAK_GLASS_RE = re.compile(rf"{re.escape(breakglass.ENV)}\s*=\s*([^\s\"'\\{{}}]+)")


def extract(transcript: Path) -> dict[str, Any]:
    events = list(iter_events(transcript))
    vector = tool_call_vector(events)
    return {
        "tool_call_count": sum(vector.values()),
        "tool_call_vector": vector,
        "turn_count": turn_count(events),
        "stuck_loop": stuck_loop(events),
        "no_verify_count": no_verify_count(events),
        "break_glass_count": break_glass_count(events),
        "error_count": error_count(events),
        "retry_count": retry_count(events),
        "token_usage": token_usage(events),
    }


def iter_events(transcript: Path) -> Iterator[dict]:
    """Yield each transcript event; blank/malformed lines and a missing file are skipped."""
    try:
        with transcript.open(encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    yield event
    except OSError:
        return


def tool_call_vector(events: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Call counts keyed by tool name; an unnamed ``tool_use`` is keyed ``""``."""
    vector: dict[str, int] = {}
    for block in _tool_use_blocks(events):
        name = str(block.get("name") or "")
        vector[name] = vector.get(name, 0) + 1
    return vector


def tool_call_count(events: Iterable[Mapping[str, Any]]) -> int:
    return sum(tool_call_vector(events).values())


def turn_count(events: Iterable[Mapping[str, Any]]) -> int:
    """Distinct assistant messages; streamed parts sharing a ``message.id`` count once."""
    seen_ids: set[str] = set()
    count = 0
    for event in events:
        message = event.get("message")
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        msg_id = message.get("id")
        if isinstance(msg_id, str):
            if msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
        count += 1
    return count


def stuck_loop(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """``detected`` ORs two signals: repeated identical calls within one turn, and
    runaway in-turn iterations. The counts behind each are returned alongside.

    Repeats are counted PER TURN — a call legitimately repeated once per turn must
    not flag.
    """
    events = list(events)
    max_repeated = 0
    for blocks in _turn_tool_use_blocks(events):
        counts: dict[tuple[str, str], int] = {}
        for block in blocks:
            fp = _fingerprint(block)
            counts[fp] = counts.get(fp, 0) + 1
        max_repeated = max(max_repeated, max(counts.values(), default=0))
    max_iterations = max(_turn_iteration_counts(events), default=0)
    return {
        "detected": max_repeated > _REPEAT_THRESHOLD
        or max_iterations > _ITERATION_THRESHOLD,
        "max_repeated_calls": max_repeated,
        "max_turn_iterations": max_iterations,
    }


def retry_count(events: Iterable[Mapping[str, Any]]) -> int:
    previous: tuple[str, str] | None = None
    retries = 0
    for block in _tool_use_blocks(events):
        fp = _fingerprint(block)
        if fp == previous:
            retries += 1
        previous = fp
    return retries


def no_verify_count(events: Iterable[Mapping[str, Any]]) -> int:
    """Tool calls carrying any :data:`_BYPASS_MARKERS` token; one per call."""
    count = 0
    for block in _tool_use_blocks(events):
        text = _input_text(block)
        if any(marker in text for marker in _BYPASS_MARKERS):
            count += 1
    return count


def break_glass_count(events: Iterable[Mapping[str, Any]]) -> int:
    """Tool calls that ARM the break-glass escape; a disarming assignment does not count."""
    count = 0
    for block in _tool_use_blocks(events):
        for value in _BREAK_GLASS_RE.findall(_input_text(block)):
            if breakglass.is_armed(value.strip("\"'")):
                count += 1
                break  # one armed use per call, not per assignment occurrence.
    return count


def error_count(events: Iterable[Mapping[str, Any]]) -> int:
    count = 0
    for block in _content_blocks(events):
        if block.get("type") == "tool_result" and block.get("is_error"):
            count += 1
    return count


def token_usage(events: Iterable[Mapping[str, Any]]) -> dict[str, int] | None:
    """Summed assistant-message ``usage``, or ``None`` when no turn logged any.

    Consumed once per ``message.id`` so streamed parts of one turn do not double-count.
    """
    fields = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }
    seen = False
    seen_ids: set[str] = set()
    for event in events:
        message = event.get("message")
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        usage = message.get("usage")
        if not isinstance(usage, Mapping):
            continue
        msg_id = message.get("id")
        if isinstance(msg_id, str):
            if msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
        seen = True
        fields["input_tokens"] += _int(usage.get("input_tokens"))
        fields["output_tokens"] += _int(usage.get("output_tokens"))
        fields["cache_read_tokens"] += _int(usage.get("cache_read_input_tokens"))
        fields["cache_creation_tokens"] += _int(
            usage.get("cache_creation_input_tokens")
        )
    if not seen:
        return None
    fields["total_tokens"] = fields["input_tokens"] + fields["output_tokens"]
    return fields


def _no_stray_pids() -> list[int]:
    """The default stray-PID source: none — the injectable seam for a PID tracker."""
    return []


def exit_hygiene(
    repo_root: str | Path,
    *,
    list_stray_pids: Callable[[], Sequence[int]] = _no_stray_pids,
) -> dict[str, Any]:
    """Clean worktree + no stray PIDs; a git failure degrades to ``worktree_clean=None``."""
    try:
        dirty = git.status_porcelain(cwd=str(repo_root))
    except execrun.ExecError:
        worktree_clean: bool | None = None
        dirty_file_count: int | None = None
    else:
        worktree_clean = not dirty
        dirty_file_count = len(dirty)
    stray = list(list_stray_pids())
    return {
        "worktree_clean": worktree_clean,
        "dirty_file_count": dirty_file_count,
        "stray_pid_count": len(stray),
    }


def _content_blocks(events: Iterable[Mapping[str, Any]]) -> Iterator[Mapping[str, Any]]:
    for event in events:
        message = event.get("message")
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, Mapping):
                yield block


def _tool_use_blocks(
    events: Iterable[Mapping[str, Any]],
) -> Iterator[Mapping[str, Any]]:
    for block in _content_blocks(events):
        if block.get("type") == "tool_use":
            yield block


def _turn_tool_use_blocks(
    events: Iterable[Mapping[str, Any]],
) -> Iterator[list[Mapping[str, Any]]]:
    """Per assistant turn, that turn's ``tool_use`` blocks; dedup matches :func:`turn_count`."""
    seen_ids: set[str] = set()
    for event in events:
        message = event.get("message")
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        msg_id = message.get("id")
        if isinstance(msg_id, str):
            if msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)
        content = message.get("content")
        if not isinstance(content, list):
            yield []
            continue
        yield [
            block
            for block in content
            if isinstance(block, Mapping) and block.get("type") == "tool_use"
        ]


def _fingerprint(block: Mapping[str, Any]) -> tuple[str, str]:
    name = str(block.get("name") or "")
    inp = block.get("input")
    try:
        args = json.dumps(inp, sort_keys=True, default=str)
    except (TypeError, ValueError):
        args = repr(inp)
    return (name, args)


def _input_text(block: Mapping[str, Any]) -> str:
    inp = block.get("input")
    if inp is None:
        return ""
    if isinstance(inp, str):
        return inp
    try:
        return json.dumps(inp, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(inp)


def _turn_iteration_counts(events: Iterable[Mapping[str, Any]]) -> Iterator[int]:
    for event in events:
        message = event.get("message")
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        usage = message.get("usage")
        if not isinstance(usage, Mapping):
            continue
        iterations = usage.get("iterations")
        if isinstance(iterations, list):
            yield len(iterations)


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
