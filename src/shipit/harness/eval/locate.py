"""Run locator — resolve a just-closed **run**'s on-disk files from the hook payload.

`locate_run(hook_input) -> RunFiles | None` is the filesystem boundary of the eval
wire. The terminal-hook payload carries `transcript_path`; the locator turns that
into the run's transcript plus its `.meta.json` sidecar — handling BOTH run kinds
(CONTEXT.md "Run"):

  - the **coordinator** run is the top-level session transcript `<session_id>.jsonl`,
    which has NO meta sidecar;
  - a **subagent** run is `…/subagents/agent-<id>.jsonl`, co-located with a sibling
    `agent-<id>.meta.json` carrying `agentType` / `spawnMode`.

The split is read off the transcript filename (`agent-` prefix ⇒ subagent), so the
locator is a pure function of the payload + existence probes. `None` means the
payload named no transcript, or named one that does not exist — the caller fails
open (no record, no crash).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Subagent transcripts (and their meta sidecars) are named `agent-<id>.*`; the
#: coordinator session transcript is `<session_id>.jsonl` with no such prefix.
_SUBAGENT_PREFIX = "agent-"
_META_SUFFIX = ".meta.json"


@dataclass(frozen=True)
class RunFiles:
    """The on-disk files of one run: its transcript, and its meta sidecar if any.

    ``meta`` is ``None`` for the coordinator run (the session transcript has no
    sidecar); a subagent run carries its ``agent-<id>.meta.json``.
    """

    transcript: Path
    meta: Path | None


def locate_run(hook_input: Mapping[str, Any]) -> RunFiles | None:
    """Resolve the run's transcript + meta from a `Stop` / `SubagentStop` payload.

    Returns ``None`` when the payload names no ``transcript_path`` OR names one
    that does not exist on disk — the boundary treats either as "nothing to
    evaluate" and falls through to a no-op, honouring the fail-open contract that
    a missing transcript writes nothing (rather than a hollow count-0 record).
    """
    raw = hook_input.get("transcript_path")
    if not raw:
        return None
    transcript = Path(str(raw))
    if not transcript.exists():
        return None
    return RunFiles(transcript=transcript, meta=_sibling_meta(transcript))


def _sibling_meta(transcript: Path) -> Path | None:
    """The `agent-<id>.meta.json` next to a subagent transcript, or ``None``.

    Only subagent transcripts (``agent-`` prefix) have a meta sidecar; the
    coordinator session transcript has none. The sidecar is returned only when it
    actually exists, so a renamed/missing meta degrades to a coordinator-shaped
    record rather than a dangling path.
    """
    if not transcript.name.startswith(_SUBAGENT_PREFIX):
        return None
    candidate = transcript.with_name(transcript.stem + _META_SUFFIX)
    return candidate if candidate.exists() else None
