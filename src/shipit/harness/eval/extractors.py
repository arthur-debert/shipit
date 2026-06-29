"""Objective extractors — metrics read deterministically from a run's transcript.

:func:`extract` is the orchestrator the hook calls; the per-metric work lives in
**composable pure functions** over already-parsed transcript events (the PRD's
module #2: "per-metric extractors live *inside* this module, not as separate
modules"). WS01 carried the one walking-skeleton metric; WS02 grows the full
transcript-cheap set:

  - **tool-call vector** — per-tool counts (:func:`tool_call_vector`), the scalar
    :func:`tool_call_count` generalized;
  - **turn count** — agent steps (:func:`turn_count`);
  - **stuck-loop fingerprints** — same tool+args hash repeated, or a runaway
    in-turn iteration count (:func:`stuck_loop`);
  - **check-bypass / break-glass** greps — `--no-verify` family
    (:func:`no_verify_count`) and HAR01's `SHIPIT_BREAK_GLASS` escape
    (:func:`break_glass_count`);
  - **error / retry** counts (:func:`error_count`, :func:`retry_count`);
  - **token totals** if the transcript logged them (:func:`token_usage`, ``None``
    when nothing is logged).

These are PURE over the parsed events, so each is unit-testable from a fixture
transcript (events in → metric out) and never reads the parser's internals. The
ONE exception is :func:`exit_hygiene` — a cheap live process/fs check gated to the
coordinator run's end (clean worktree + no stray PIDs); its git read goes through
the :mod:`shipit.gh` boundary and its PID source is an injectable seam, so it stays
thin and patchable. The hook wires it in only for the coordinator (it is not part
of :func:`extract`, which stays a pure function of the transcript).

The transcript is JSONL: one JSON object per line, each an event. Reading/parsing
it is the boundary (:func:`iter_events`, tolerant of blank or malformed lines).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from ... import gh
from .. import breakglass

#: A single (tool, args) fingerprint occurring MORE than this many times WITHIN ONE
#: TURN is the repeated-call stuck-loop signal (PRD: "same tool+args hash >2× in a turn").
_REPEAT_THRESHOLD = 2

#: A single model turn running MORE than this many internal agentic iterations
#: (``message.usage.iterations``) is the runaway stuck-loop signal (PRD: ">8 iterations").
_ITERATION_THRESHOLD = 8

#: Commit/push check-bypass markers grepped from tool-call inputs. ``--no-verify``
#: skips git's pre-commit/pre-push hooks; ``--no-hooks`` is the lefthook/husky form.
_BYPASS_MARKERS = ("--no-verify", "--no-hooks")

#: HAR01's break-glass escape (`SHIPIT_BREAK_GLASS=<truthy>` in a command). The
#: value capture is a single shell token that STOPS at whitespace, quotes, braces,
#: and backslashes — so a grep over the JSON-serialized tool input does not swallow
#: the surrounding JSON syntax (e.g. `{"command": "SHIPIT_BREAK_GLASS=0"}` captures
#: ``0``, not ``0"}``). Whether a captured value arms or disarms the escape is
#: decided by :mod:`shipit.harness.breakglass`, shared with the pretooluse hook.
_BREAK_GLASS_RE = re.compile(rf"{re.escape(breakglass.ENV)}\s*=\s*([^\s\"'\\{{}}]+)")


def extract(transcript: Path) -> dict[str, Any]:
    """The objective metrics for a run's transcript (PURE over the on-disk events).

    Reads the transcript once and hands the parsed events to each pure metric. The
    returned dict is what the record builder folds into the eval record; the hook
    adds the live :func:`exit_hygiene` block separately for the coordinator run.
    """
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
    """Yield each transcript event (one parsed JSON object per line).

    Parses lazily, one line at a time — it reads and decodes a line only when the
    consumer pulls it, so a caller that needs a single pass (or an early exit) never
    forces the whole file. :func:`extract`, the hook's caller, deliberately does NOT
    stream: it materializes the events with ``list(...)`` because the per-turn metrics
    (turn grouping, stuck-loop, token dedup) each walk the events again, so a single
    materialized pass is cheaper than re-reading the file per metric. Transcripts are
    transcript-cheap (the hook's "few ms" budget is about avoiding a model call, not
    about never holding the events in memory). Tolerant by design — blank lines and
    any line that is not a JSON object are skipped rather than raising, so a
    partially-written or truncated transcript still yields the events it can. A
    missing file yields nothing.
    """
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


# --------------------------------------------------------------------------- #
# Tool-call metrics
# --------------------------------------------------------------------------- #


def tool_call_vector(events: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Per-tool call counts — the tool-call vector keyed by tool name.

    Each ``{"type": "tool_use", "name": …}`` content block on an assistant message
    is one call; this groups the run's calls by tool so a reader can see whether an
    agent used the tools its role expects. An unnamed tool_use is keyed ``""``.
    """
    vector: dict[str, int] = {}
    for block in _tool_use_blocks(events):
        name = str(block.get("name") or "")
        vector[name] = vector.get(name, 0) + 1
    return vector


def tool_call_count(events: Iterable[Mapping[str, Any]]) -> int:
    """Total `tool_use` blocks across the run — the tool-call vector summed."""
    return sum(tool_call_vector(events).values())


def turn_count(events: Iterable[Mapping[str, Any]]) -> int:
    """The run's agent-turn (step) count — distinct assistant messages.

    One assistant message is one step the agent took. Streamed events that share a
    ``message.id`` (a single response delivered in parts) count once; events with
    no id fall back to counting per assistant event.
    """
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


# --------------------------------------------------------------------------- #
# Stuck-loop fingerprints
# --------------------------------------------------------------------------- #


def stuck_loop(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Stuck-loop fingerprints for the run (PRD module #2).

    Two independent signals, OR'd into ``detected``:

      - **repeated identical calls** — the max number of times any single
        ``(tool, args-hash)`` fingerprint occurs WITHIN ONE TURN; ``> 2`` is the
        "same tool+args hash >2× in a turn" signal (an agent re-issuing the exact
        same call inside a single step). Counting per-turn (not across the whole
        run) is deliberate: a call legitimately repeated once per turn — e.g.
        ``Bash pytest`` every turn — is NORMAL and must not flag.
      - **runaway iterations** — the max ``message.usage.iterations`` length across
        the run's turns; ``> 8`` is a single model turn that spun internally.

    Returns the booleans' inputs too (``max_repeated_calls`` / ``max_turn_iterations``)
    so the record carries *why* a run was flagged, not just that it was.
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
    """Back-to-back identical tool calls — the run's retry count.

    Counts positions in the ordered tool-call sequence where a call repeats the
    immediately preceding ``(tool, args)`` fingerprint (the agent re-running the
    exact same thing). A subset of, and finer-grained than, the stuck-loop repeat
    signal — it isolates *consecutive* retries from calls merely repeated apart.
    """
    previous: tuple[str, str] | None = None
    retries = 0
    for block in _tool_use_blocks(events):
        fp = _fingerprint(block)
        if fp == previous:
            retries += 1
        previous = fp
    return retries


# --------------------------------------------------------------------------- #
# Check-bypass / break-glass greps
# --------------------------------------------------------------------------- #


def no_verify_count(events: Iterable[Mapping[str, Any]]) -> int:
    """Tool calls that bypass the commit/push checks (`--no-verify` family).

    Counts tool_use blocks whose serialized input contains any
    :data:`_BYPASS_MARKERS` token — one per call regardless of how many markers it
    carries — so a run that sidestepped the pre-commit/pre-push hooks is visible
    from the record (PRD user story 3).
    """
    count = 0
    for block in _tool_use_blocks(events):
        text = _input_text(block)
        if any(marker in text for marker in _BYPASS_MARKERS):
            count += 1
    return count


def break_glass_count(events: Iterable[Mapping[str, Any]]) -> int:
    """HAR01 break-glass uses in the run — `SHIPIT_BREAK_GLASS=<truthy>` in a command.

    Counts tool calls that ARM the escape (a truthy assignment); a disarming
    ``=0`` / ``=false`` / … assignment does not count, matching the pretooluse
    hook's own falsey set. Break-glass frequency is the HAR01-tightening signal
    (CONTEXT.md "break-glass"; PRD user story 5).
    """
    count = 0
    for block in _tool_use_blocks(events):
        for value in _BREAK_GLASS_RE.findall(_input_text(block)):
            # Strip any stray surrounding quotes, then defer the armed/disarmed
            # decision to the shared break-glass semantics (same falsey set as the
            # pretooluse hook, including the empty string).
            if breakglass.is_armed(value.strip("\"'")):
                count += 1
                break  # one armed use per call, not per assignment occurrence.
    return count


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


def error_count(events: Iterable[Mapping[str, Any]]) -> int:
    """Errored tool results in the run — ``tool_result`` blocks flagged ``is_error``.

    A tool whose execution failed comes back as a ``{"type": "tool_result",
    "is_error": true, …}`` block on the following user message; summing them is the
    run's error count.
    """
    count = 0
    for block in _content_blocks(events):
        if block.get("type") == "tool_result" and block.get("is_error"):
            count += 1
    return count


# --------------------------------------------------------------------------- #
# Token totals
# --------------------------------------------------------------------------- #


def token_usage(events: Iterable[Mapping[str, Any]]) -> dict[str, int] | None:
    """Summed token usage across the run's turns, or ``None`` if none was logged.

    Sums ``message.usage`` over every assistant message that carries it
    (``input_tokens`` / ``output_tokens`` and the cache-read / cache-creation
    input variants); ``total_tokens`` is input+output. Returns ``None`` when NO
    turn logged usage, so an absent metric reads as absent rather than a hollow
    all-zero block (PRD: "token totals if logged … else omit/None").

    Streamed parts of one response share a ``message.id`` and may each carry the
    same usage block, so usage is consumed ONCE per id (mirroring :func:`turn_count`)
    rather than summed per event — otherwise a single turn's tokens double-count.
    Only assistant messages are summed, matching the documented scope.
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


# --------------------------------------------------------------------------- #
# Exit hygiene (the one live check — coordinator run only)
# --------------------------------------------------------------------------- #


def _no_stray_pids() -> list[int]:
    """The default stray-PID source: none.

    A reliable stray-PID check needs a registry of the PIDs a run spawned (a
    background-shell tracker), which the harness does not yet keep — so the default
    reports none and this function is the thin, patchable seam a future tracker
    feeds. Injecting a lister into :func:`exit_hygiene` is how tests (and a later
    tracker) supply candidate PIDs.
    """
    return []


def exit_hygiene(
    repo_root: str | Path,
    *,
    list_stray_pids: Callable[[], Sequence[int]] = _no_stray_pids,
) -> dict[str, Any]:
    """The coordinator run's exit-hygiene check: clean worktree + no stray PIDs.

    The one impure extractor — a cheap process/fs check gated to the coordinator
    run's end (PRD user story 13; the live-observed failure was a run that idled
    with conflict markers still in the tree). The worktree read goes through the
    :mod:`shipit.gh` boundary (``git status --porcelain``); a git failure degrades
    to ``worktree_clean=None`` rather than raising, honouring the hook's fail-open
    contract. ``list_stray_pids`` is the injectable PID seam (see :func:`_no_stray_pids`).
    """
    try:
        porcelain = gh.git_status_porcelain(cwd=str(repo_root))
    except gh.GhError:
        worktree_clean: bool | None = None
        dirty_file_count: int | None = None
    else:
        dirty = [line for line in porcelain.splitlines() if line.strip()]
        worktree_clean = not dirty
        dirty_file_count = len(dirty)
    stray = list(list_stray_pids())
    return {
        "worktree_clean": worktree_clean,
        "dirty_file_count": dirty_file_count,
        "stray_pid_count": len(stray),
    }


# --------------------------------------------------------------------------- #
# Internal helpers (pure over parsed events)
# --------------------------------------------------------------------------- #


def _content_blocks(events: Iterable[Mapping[str, Any]]) -> Iterator[Mapping[str, Any]]:
    """Yield every content block across all messages (assistant + user turns).

    A message's ``content`` is a list of typed blocks (text, tool_use, tool_result,
    …); events without a list ``message.content`` (summaries, string-content turns,
    attachments) contribute nothing and never raise.
    """
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
    """Yield each ``tool_use`` block across the run's messages, in transcript order."""
    for block in _content_blocks(events):
        if block.get("type") == "tool_use":
            yield block


def _turn_tool_use_blocks(
    events: Iterable[Mapping[str, Any]],
) -> Iterator[list[Mapping[str, Any]]]:
    """Yield, per assistant TURN, the list of that turn's ``tool_use`` blocks.

    A turn is one assistant message. Streamed parts that share a ``message.id`` are
    the SAME turn and contribute once — taken from the first event bearing that id,
    mirroring :func:`turn_count`'s dedup so the per-turn stuck-loop count agrees with
    the turn count on what a "turn" is. An assistant event with no id is its own
    turn; non-assistant events contribute nothing.
    """
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
    """A ``(tool-name, canonical-args)`` fingerprint for one tool_use block.

    The args are canonicalized with sorted keys so two calls with identically-valued
    inputs collide regardless of key order; non-serializable inputs fall back to
    ``repr`` so a fingerprint is always computable.
    """
    name = str(block.get("name") or "")
    inp = block.get("input")
    try:
        args = json.dumps(inp, sort_keys=True, default=str)
    except (TypeError, ValueError):
        args = repr(inp)
    return (name, args)


def _input_text(block: Mapping[str, Any]) -> str:
    """The tool_use input serialized to text for marker greps (``""`` if none)."""
    inp = block.get("input")
    if inp is None:
        return ""
    if isinstance(inp, str):
        return inp
    try:
        # sort_keys mirrors _fingerprint's canonicalization, so grep/text metrics
        # are deterministic regardless of input key order.
        return json.dumps(inp, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(inp)


def _turn_iteration_counts(events: Iterable[Mapping[str, Any]]) -> Iterator[int]:
    """Yield the ``message.usage.iterations`` length for each assistant turn that logs it."""
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
    """Coerce a usage field to ``int`` (a missing/non-numeric field counts as 0)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0
