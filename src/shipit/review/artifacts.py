"""The per-run artifact bundle a review sub-agent run persists beside the round
store: its exact prompt, its raw streams, and an accreting ``meta.json``. Every
write is fail-open — telemetry must not degrade the run it observes.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

from ..harness.eval.store import repo_key, store_dir
from ..identity import repo_from_slug

logger = logging.getLogger("shipit.review")

#: The bundle tree's kind directory under the shared state-family root.
ARTIFACTS_KIND = "review-artifacts"

#: The bundle's fixed member files.
PROMPT_FILENAME = "prompt.txt"
STDOUT_FILENAME = "stdout.raw"
STDERR_FILENAME = "stderr.raw"
META_FILENAME = "meta.json"

#: Generous, so only a runaway or prompt-injected reviewer ever hits it.
MAX_STREAM_CHARS = 5 * 1024 * 1024
_TRUNCATION_MARKER = "\n…[shipit: stream truncated at {cap} chars]…\n"


def round_root(
    repo_slug: str | None, round_id: str, *, base_dir: Path | None = None
) -> Path | None:
    """The directory one round's bundles live under; ``None`` — the disabled-sink
    cue — when the slug is missing or malformed."""
    slug = (repo_slug or "").strip()
    if not slug:
        logger.warning(
            "review artifact bundle disabled for round %s: no repo identity",
            round_id,
        )
        return None
    try:
        repo = repo_from_slug(slug)
    except ValueError:
        logger.warning(
            "review artifact bundle disabled for round %s: unusable repo slug %r",
            round_id,
            slug,
            exc_info=True,
        )
        return None
    return store_dir(base_dir, kind=ARTIFACTS_KIND) / repo_key(repo) / round_id


class RunArtifacts:
    """The fail-open bundle writer for one run; ``dir`` ``None`` is the disabled
    sink whose every write no-ops, so callers thread one object unconditionally."""

    def __init__(self, dir: Path | None) -> None:  # noqa: A002 - the bundle dir IS the identity
        self.dir = dir
        self._meta: dict[str, Any] = {}

    @classmethod
    def disabled(cls) -> RunArtifacts:
        return cls(None)

    @classmethod
    def under(cls, round_dir: Path | None, name: str) -> RunArtifacts:
        """The bundle named ``name`` under a round's root; a ``None``
        ``round_dir`` yields the disabled sink."""
        return cls(None if round_dir is None else Path(round_dir) / name)

    def write_prompt(self, text: str) -> None:
        """Called BEFORE the launch, so even a killed child leaves its prompt."""
        self._write(PROMPT_FILENAME, text)

    def write_streams(self, stdout: str | None, stderr: str | None) -> None:
        """Persist the raw streams, each bounded to :data:`MAX_STREAM_CHARS`;
        a truncation is recorded in the meta, never claimed as the full output."""
        out, out_truncated = _bounded(stdout or "")
        err, err_truncated = _bounded(stderr or "")
        self._write(STDOUT_FILENAME, out)
        self._write(STDERR_FILENAME, err)
        if out_truncated or err_truncated:
            self.record(stdout_truncated=out_truncated, stderr_truncated=err_truncated)

    def record(self, **fields: Any) -> None:
        """Merge ``fields`` into ``meta.json`` and rewrite it, so a crash between
        layers loses nothing already recorded. ``None`` is recorded as written."""
        self._meta.update(fields)
        self._write(
            META_FILENAME,
            json.dumps(self._meta, indent=2, sort_keys=True, default=repr) + "\n",
        )

    def _write(self, filename: str, content: str) -> None:
        """One member-file write: directory on demand, atomic, fail-open. The
        rename means a crash mid-write costs only the temp, since ``meta.json``
        is rewritten whole on every :meth:`record`."""
        if self.dir is None:
            return
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            dest = self.dir / filename
            tmp = dest.with_name(f"{filename}.{uuid.uuid4().hex[:8]}.tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(dest)
        except OSError:
            logger.warning(
                "review artifact write failed for %s (the run is unaffected)",
                self.dir / filename,
                exc_info=True,
            )


def _bounded(text: str) -> tuple[str, bool]:
    """``text`` capped to :data:`MAX_STREAM_CHARS` including the marker, and
    whether it was capped; the head is kept, since errors live there."""
    if len(text) <= MAX_STREAM_CHARS:
        return text, False
    marker = _TRUNCATION_MARKER.format(cap=MAX_STREAM_CHARS)
    head = text[: MAX_STREAM_CHARS - len(marker)]
    return head + marker, True
