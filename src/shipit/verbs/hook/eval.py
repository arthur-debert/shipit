"""`shipit hook stop` / `shipit hook subagent-stop` — the eval terminal-hook boundary.

THIN and **synchronous** (objective-only ⇒ no model call ⇒ a few ms of parsing):
read the Claude Code `Stop` / `SubagentStop` payload on stdin → locate the just-
closed run's transcript + meta → extract objective metrics → build the eval record
→ append it to the local store. Both events run the SAME pipeline; the coordinator
-vs-subagent split is carried entirely by the locator (a subagent run resolves a
`.meta.json`, the coordinator run does not), so there is one `run()` core.

**Fail-open is the contract.** Eval must NEVER break a real session: any error —
bad stdin, malformed JSON, a missing transcript, a git failure — is swallowed,
logged at DEBUG, and the hook exits 0 having written nothing. The hook emits no
stdout decision (these events take none); its only effect is the record on disk.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import sys
from pathlib import Path
from typing import TextIO

import click

from ... import gh
from ...harness.eval.extractors import extract
from ...harness.eval.locate import locate_run
from ...harness.eval.record import build
from ...harness.eval.store import append_record

logger = logging.getLogger("shipit.hook")


@click.command(name="stop")
def stop_cmd() -> None:
    """Evaluate the coordinator run at its terminal `Stop` hook (fail-open, exit 0)."""
    raise SystemExit(run())


@click.command(name="subagent-stop")
def subagent_stop_cmd() -> None:
    """Evaluate a subagent run at its terminal `SubagentStop` hook (fail-open, exit 0)."""
    raise SystemExit(run())


def run(stdin: TextIO | None = None) -> int:
    """Parse stdin → locate → extract → build → store. Returns 0 always (fail-open).

    The whole pipeline is wrapped so any failure degrades to a no-op rather than
    disturbing the session that just ended.
    """
    try:
        raw = (stdin if stdin is not None else sys.stdin).read()
        payload = json.loads(raw)
        run_files = locate_run(payload)
        if run_files is None:
            return 0  # nothing named to evaluate — no-op.
        meta = _read_meta(run_files.meta)
        metrics = extract(run_files.transcript)
        repo_root = _repo_root(str(payload.get("cwd") or os.getcwd()))
        record = build(
            metrics=metrics,
            meta=meta,
            variant=None,  # WS01 placeholder — WS03's variant resolver fills this.
            commit=_git_commit(repo_root),
            timestamp=_now_iso(),
        )
        append_record(record, repo_root)
    except Exception:  # noqa: BLE001 — fail-open is the whole point.
        logger.debug("eval hook failed open (no record written)", exc_info=True)
    return 0


def _read_meta(meta_path: object) -> dict | None:
    """Parse a run's `.meta.json`, or ``None`` (coordinator, or unreadable)."""
    if meta_path is None:
        return None
    try:
        data = json.loads(Path(str(meta_path)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _repo_root(cwd: str) -> str:
    """The git working-tree root for ``cwd`` (the store's repo key), else ``cwd``."""
    try:
        root = gh._git(["rev-parse", "--show-toplevel"], cwd=cwd).strip()
    except gh.GhError:
        return cwd
    return root or cwd


def _git_commit(repo_root: str) -> str | None:
    """The current commit SHA for ``repo_root``, or ``None`` if unresolvable."""
    try:
        return gh._git(["rev-parse", "HEAD"], cwd=repo_root).strip() or None
    except gh.GhError:
        return None


def _now_iso() -> str:
    """The current UTC time as an ISO-8601 string (the record's `eval.timestamp`)."""
    return _dt.datetime.now(_dt.UTC).isoformat()
