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

from ... import identity
from ...harness.eval.extractors import exit_hygiene, extract
from ...harness.eval.locate import locate_run
from ...harness.eval.record import build
from ...harness.eval.store import append_record
from ...harness.eval.variant import resolve_variant

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
        # One resolve of the checkout's identity: the WorkingDir gives the repo-root
        # path (for exit-hygiene), the `Repo` IDENTITY the store keys on (ADR-0024 —
        # so a run's record pools by origin owner/name, not by clone path), and the
        # revision's HEAD commit for the record's `git.commit` stamp.
        wd = identity.resolve_working_dir(str(payload.get("cwd") or os.getcwd()))
        metrics = extract(run_files.transcript)
        if run_files.is_coordinator:
            # The coordinator run gets the one live check — exit-hygiene (clean
            # worktree + no stray PIDs) at its terminal hook. Gated on run KIND, not
            # on `meta is None`: a subagent with an unreadable/missing meta sidecar
            # also parses to None but must NOT run this coordinator-only check.
            metrics["exit_hygiene"] = exit_hygiene(wd.path)
        record = build(
            metrics=metrics,
            meta=meta,
            variant=_variant(meta),
            commit=wd.revision.commit,
            timestamp=_now_iso(),
            is_coordinator=run_files.is_coordinator,
        )
        append_record(record, wd.repo)
    except Exception:  # noqa: BLE001 — fail-open is the whole point.
        logger.debug("eval hook failed open (no record written)", exc_info=True)
    return 0


def _variant(meta: dict | None) -> dict | None:
    """The run's variant record (role-prompt hash + label), or ``None`` fail-open.

    The variant is best-effort attribution: if the role prompt cannot be read or
    hashed, the record is still written (with a null variant) rather than dropped —
    a tighter fail-open than the whole-pipeline guard so an attribution miss never
    costs a metric record.
    """
    try:
        return resolve_variant(meta).as_record()
    except Exception:  # noqa: BLE001 — variant is best-effort; never drop the record.
        logger.debug("variant resolution failed; stamping null", exc_info=True)
        return None


def _read_meta(meta_path: object) -> dict | None:
    """Parse a run's `.meta.json`, or ``None`` (coordinator, or unreadable)."""
    if meta_path is None:
        return None
    try:
        data = json.loads(Path(str(meta_path)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _now_iso() -> str:
    """The current UTC time as an ISO-8601 string (the record's `eval.timestamp`)."""
    return _dt.datetime.now(_dt.UTC).isoformat()
