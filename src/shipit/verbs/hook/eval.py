"""`shipit hook stop` / `shipit hook subagent-stop` — the eval terminal-hook boundary."""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import sys
from pathlib import Path
from typing import TextIO

import click

from ... import identity, logcontext
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
    """Returns 0 always — fail-open: an eval failure never blocks the run."""
    try:
        raw = (stdin if stdin is not None else sys.stdin).read()
        payload = json.loads(raw)
        run_files = locate_run(payload)
        if run_files is None:
            return 0
        meta = _read_meta(run_files.meta)
        wd = identity.resolve_working_dir(str(payload.get("cwd") or os.getcwd()))
        metrics = extract(run_files.transcript)
        if run_files.is_coordinator:
            metrics["exit_hygiene"] = exit_hygiene(wd.path)
        record = build(
            metrics=metrics,
            meta=meta,
            variant=_variant(meta),
            commit=None if wd.revision.commit is None else str(wd.revision.commit),
            timestamp=_now_iso(),
            is_coordinator=run_files.is_coordinator,
            spawned_role=logcontext.role_from_env(),
            run_id=run_files.run_id,
        )
        append_record(record, wd.repo)
    except Exception:  # noqa: BLE001 — fail-open is the whole point.
        logger.warning("eval hook failed open (no record written)", exc_info=True)
    return 0


def _variant(meta: dict | None) -> dict | None:
    """The run's variant record (role-prompt hash and label), or ``None``."""
    try:
        return resolve_variant(meta).as_record()
    except Exception:  # noqa: BLE001 — variant is best-effort; never drop the record.
        logger.warning("variant resolution failed; stamping null", exc_info=True)
        return None


def _read_meta(meta_path: object) -> dict | None:
    """A run's parsed `.meta.json`, or ``None`` when absent or unreadable."""
    if meta_path is None:
        return None
    try:
        data = json.loads(Path(str(meta_path)).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning(
            "eval: unreadable run meta %s — record proceeds without it",
            meta_path,
            exc_info=True,
        )
        return None
    return data if isinstance(data, dict) else None


def _now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()
