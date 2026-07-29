"""``harness/eval/locate`` — a just-closed run's transcript and meta, from the hook payload."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Subagent transcripts are `agent-<id>.*`; the coordinator's has no such prefix.
_SUBAGENT_PREFIX = "agent-"
_META_SUFFIX = ".meta.json"


@dataclass(frozen=True)
class RunFiles:
    transcript: Path
    meta: Path | None

    @property
    def run_id(self) -> str:
        """The run's stable identity — the transcript filename's stem, minting no second id."""
        return self.transcript.stem

    @property
    def is_coordinator(self) -> bool:
        """True for the coordinator run, read off the filename, NOT off whether meta parsed."""
        return not self.transcript.name.startswith(_SUBAGENT_PREFIX)


def locate_run(hook_input: Mapping[str, Any]) -> RunFiles | None:
    """The run's files, or ``None`` when the payload names no transcript or a missing one."""
    raw = hook_input.get("transcript_path")
    if not raw:
        return None
    transcript = Path(str(raw))
    if not transcript.exists():
        return None
    return RunFiles(transcript=transcript, meta=_sibling_meta(transcript))


def _sibling_meta(transcript: Path) -> Path | None:
    """The existing `agent-<id>.meta.json` next to a subagent transcript, else ``None``."""
    if not transcript.name.startswith(_SUBAGENT_PREFIX):
        return None
    candidate = transcript.with_name(transcript.stem + _META_SUFFIX)
    return candidate if candidate.exists() else None
