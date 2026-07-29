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

ARTIFACTS_KIND = "review-artifacts"

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
    """``dir`` ``None`` is the disabled sink whose every write no-ops, so callers
    thread one object unconditionally."""

    def __init__(self, dir: Path | None) -> None:  # noqa: A002 - the bundle dir IS the identity
        self.dir = dir
        self._meta: dict[str, Any] = {}

    @classmethod
    def disabled(cls) -> RunArtifacts:
        return cls(None)

    @classmethod
    def under(cls, round_dir: Path | None, name: str) -> RunArtifacts:
        return cls(None if round_dir is None else Path(round_dir) / name)

    def write_prompt(self, text: str) -> None:
        self._write(PROMPT_FILENAME, text)

    def write_streams(self, stdout: str | None, stderr: str | None) -> None:
        out, out_truncated = _bounded(stdout or "")
        err, err_truncated = _bounded(stderr or "")
        self._write(STDOUT_FILENAME, out)
        self._write(STDERR_FILENAME, err)
        if out_truncated or err_truncated:
            self.record(stdout_truncated=out_truncated, stderr_truncated=err_truncated)

    def record(self, **fields: Any) -> None:
        """Merge into ``meta.json`` and rewrite, so a crash between layers loses
        nothing already recorded."""
        self._meta.update(fields)
        self._write(
            META_FILENAME,
            json.dumps(self._meta, indent=2, sort_keys=True, default=repr) + "\n",
        )

    def _write(self, filename: str, content: str) -> None:
        """Directory on demand, atomic, fail-open: ``meta.json`` is rewritten
        whole every :meth:`record`, so a crash mid-write must cost only the temp."""
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
    if len(text) <= MAX_STREAM_CHARS:
        return text, False
    marker = _TRUNCATION_MARKER.format(cap=MAX_STREAM_CHARS)
    head = text[: MAX_STREAM_CHARS - len(marker)]
    return head + marker, True
