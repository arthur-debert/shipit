"""Objective extractors — metrics read deterministically from a run's transcript.

WS01 carries the ONE walking-skeleton metric, the **tool-call count**, behind a
clean seam: :func:`extract` is the orchestrator the hook calls, and the per-metric
work lives in pure functions over already-parsed transcript events. WS02 grows the
composable set (stuck-loop fingerprints, `--no-verify` / workaround greps,
break-glass count, step/turn count, token totals) by adding more pure functions
here and merging their results into :func:`extract`'s dict — no caller changes
(docs/prd/har02-run-eval.md, module #2).

The transcript is JSONL: one JSON object per line, each an event. Reading/parsing
it is the boundary (:func:`iter_events`, tolerant of blank or malformed lines);
the metric functions are pure over the parsed events so they are unit-testable
from fixtures, never from a live transcript.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any


def extract(transcript: Path) -> dict[str, Any]:
    """The objective metrics for a run's transcript. WS01: tool-call count only.

    Reads the transcript once and hands the parsed events to each pure metric.
    The returned dict is merged verbatim into the eval record's ``eval.*`` fields.
    """
    events = list(iter_events(transcript))
    return {"tool_call_count": tool_call_count(events)}


def iter_events(transcript: Path) -> Iterator[dict]:
    """Yield each transcript event (one parsed JSON object per line).

    Tolerant by design — blank lines and any line that is not a JSON object are
    skipped rather than raising, so a partially-written or truncated transcript
    still yields the events it can. A missing file yields nothing.
    """
    try:
        text = transcript.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def tool_call_count(events: Iterable[Mapping[str, Any]]) -> int:
    """Count `tool_use` blocks across the run's assistant messages.

    Each tool the agent invoked is one ``{"type": "tool_use", …}`` content block
    on an assistant message; summing them is the run's tool-call total. Events
    without a list ``message.content`` (user turns, attachments, summaries)
    contribute nothing.
    """
    count = 0
    for event in events:
        message = event.get("message")
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "tool_use":
                count += 1
    return count
