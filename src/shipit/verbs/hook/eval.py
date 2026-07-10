"""`shipit hook stop` / `shipit hook subagent-stop` — the eval terminal-hook boundary.

THIN and **synchronous** (objective-only ⇒ no model call ⇒ a few ms of parsing):
read the Claude Code `Stop` / `SubagentStop` payload on stdin → locate the just-
closed run's transcript + meta → extract objective metrics → build the eval record
→ append it to the local store. Both events run the SAME pipeline; the coordinator
-vs-subagent split is carried entirely by the locator (a subagent run resolves a
`.meta.json`, the coordinator run does not), so there is one `run()` core.

**Fail-open is the contract.** Eval must NEVER break a real session: any error —
bad stdin, malformed JSON, a missing transcript, a git failure — is swallowed,
logged at WARNING (the fail-open canon in :mod:`shipit.verbs.hook`: a swallowed
failure is a degraded-but-continuing outcome), and the hook exits 0 having
written nothing. The hook emits no stdout decision (these events take none); its
only effect is the record on disk.
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
            # The record is JSON: the Sha stringifies HERE, at the one
            # serialization seam — commit identity is typed everywhere upstream.
            commit=None if wd.revision.commit is None else str(wd.revision.commit),
            timestamp=_now_iso(),
            is_coordinator=run_files.is_coordinator,
            # A spawned top-level write-Run (`shipit spawn subagent --role R`) is its
            # own top-level session, so the locator classifies it a coordinator; the
            # role it was spawned as survives only in the launch-context env var the
            # spawn threaded in (`SHIPIT_LOG_CTX_ROLE`). Read it HERE at the I/O seam
            # and pass it IN so the pure builder can override the would-be
            # `coordinator` label (the genuine interactive coordinator carries none).
            spawned_role=logcontext.role_from_env(),
            # The run's transcript-stem identity — the `eval.run_id` a
            # review-round record's contributing runs join on (RVW02-WS03).
            run_id=run_files.run_id,
        )
        append_record(record, wd.repo)
    except Exception:  # noqa: BLE001 — fail-open is the whole point.
        logger.warning("eval hook failed open (no record written)", exc_info=True)
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
        logger.warning("variant resolution failed; stamping null", exc_info=True)
        return None


def _read_meta(meta_path: object) -> dict | None:
    """Parse a run's `.meta.json`, or ``None`` (coordinator, or unreadable).

    A coordinator run has no sidecar (``meta_path is None``) — a clean no-op. An
    unreadable/unparseable sidecar the locator DID find is a swallowed failure
    (the record proceeds meta-less) → WARNING per the fail-open canon.
    """
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
    """The current UTC time as an ISO-8601 string (the record's `eval.timestamp`)."""
    return _dt.datetime.now(_dt.UTC).isoformat()
