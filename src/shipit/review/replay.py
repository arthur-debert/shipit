"""Review a commit range offline, writing a record and touching no PR."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .. import execrun, git, identity
from ..agent.backend import Backend
from ..identity import Sha
from ..spawn import launch
from . import artifacts as artifacts_mod
from . import fanout, producer, roundrecord
from .calibrator import CalibratorConfig
from .diff import RangeView, ReviewError
from .dimensions import DEFAULT_DIMENSION_NAMES, resolve_dimensions

logger = logging.getLogger("shipit.review")


def parse_range(spec: str) -> tuple[str, str, bool]:
    """Split ``A..B`` or ``A...B`` into ``(base, head, merge_base_wanted)``; a boundary dot is malformed."""
    spec = spec.strip()
    if "..." in spec:
        base, _, head = spec.partition("...")
        merge_base_wanted = True
    else:
        base, _, head = spec.partition("..")
        merge_base_wanted = False
    base, head = base.strip(), head.strip()
    if (
        not base
        or not head
        or ".." in base
        or ".." in head
        or base.endswith(".")
        or head.startswith(".")
    ):
        raise ReviewError(
            f"unusable commit range {spec!r} — pass `<base>..<head>` (exactly that "
            "diff) or `<base>...<head>` (from their merge base, the historical "
            "round-1 replay spelling), with a revision on both sides."
        )
    return base, head, merge_base_wanted


def resolve_range(spec: str, *, workdir: str | None = None) -> RangeView:
    """Resolve range ``spec`` against the checkout at ``workdir`` (default: cwd), offline."""
    workdir = workdir or "."
    toplevel = git.repo_root(cwd=workdir)
    if toplevel is None:
        raise ReviewError(
            f"{workdir!r} is not a git checkout — `shipit pr review replay` diffs "
            "a commit range inside a clone of the repository. cd into the repo "
            "and re-run."
        )
    workdir = toplevel

    try:
        repo = identity.resolve_repo(workdir)
    except (execrun.ExecError, ValueError) as exc:
        raise ReviewError(
            f"cannot key the review-round record: {workdir!r} has no resolvable "
            f"origin owner/name identity ({exc}). Replay records are stored "
            "per-repo (ADR-0024), so the checkout needs an `origin` remote."
        ) from exc

    raw_base, raw_head, merge_base_wanted = parse_range(spec)
    base_sha = _resolve_endpoint(raw_base, workdir)
    head_sha = _resolve_endpoint(raw_head, workdir)

    if merge_base_wanted:
        merged = git.merge_base(base_sha, head_sha, cwd=workdir)
        if merged is None:
            raise ReviewError(
                f"{raw_base!r} and {raw_head!r} share no common ancestor — "
                f"`{raw_base}...{raw_head}` has no merge base to review from. "
                "Pass an explicit `<base>..<head>` range instead."
            )
        base_sha = merged

    try:
        diff = git.diff_range(base_sha, head_sha, cwd=workdir)
        changed_files = git.diff_name_only(base_sha, head_sha, cwd=workdir)
    except execrun.ExecError as exc:
        raise ReviewError(
            f"failed to compute the diff for {spec!r} ({base_sha}..{head_sha}): {exc}"
        ) from exc
    if not diff.strip():
        raise ReviewError(
            f"the range {spec!r} ({base_sha}..{head_sha}) has an empty diff — "
            "nothing to review."
        )

    return RangeView(
        repo=repo,
        base_sha=base_sha,
        head_sha=head_sha,
        diff=diff,
        changed_files=changed_files,
        workdir=workdir,
    )


def _resolve_endpoint(rev: str, workdir: str) -> Sha:
    """One range endpoint → its commit :class:`~shipit.identity.Sha`, or ``ReviewError``."""
    sha = git.resolve_commit(rev, cwd=workdir)
    if sha is None:
        raise ReviewError(
            f"unknown revision {rev!r} in this checkout — replay is offline and "
            "never fetches. Fetch the commit (e.g. `git fetch origin <rev>`) or "
            "fix the spelling, then re-run."
        )
    return sha


def _provision_replay_defs(
    view: RangeView, backend: Backend, *, calibrator_on: bool
) -> None:
    """Provision the bundled role agent-defs when a launch in this replay reads them."""
    if not calibrator_on:
        return
    try:
        provision_agent_defs(view.workdir)
    except OSError as exc:
        hint = (
            " (or drop the `--calibrator-*` options to run the fan-out without "
            "the judge)"
        )
        raise ReviewError(
            f"cannot provision the reviewer role agent-defs into "
            f"{view.workdir!r} ({exc}) — a replay launch (the calibrator's judge) "
            f"reads them from the checkout. "
            f"Fix the checkout's writability{hint}, then re-run."
        ) from exc


def run_replay(
    backend: Backend,
    view: RangeView,
    *,
    model: str = "pro",
    timeout: str = "600s",
    instructions_path: str | None = None,
    cell: Mapping[str, Any] | None = None,
    launcher=None,
    base_dir: Path | None = None,
) -> dict:
    """Review ``view``'s range as one pass and write the round record."""

    agent = backend.funnel_agent or backend.name
    round_id = uuid.uuid4().hex
    run_id = uuid.uuid4().hex
    round_dir = artifacts_mod.round_root(view.repo.slug, round_id, base_dir=base_dir)
    bundle = artifacts_mod.RunArtifacts.under(round_dir, run_id)
    bundle.record(
        run_id=run_id,
        round_id=round_id,
        kind="range-pass",
        backend=agent,
        model=model,
        range={"base": str(view.base_sha), "head": str(view.head_sha)},
    )
    run: dict = {
        "run_id": run_id,
        "kind": "range-pass",
        "backend": agent,
        "model": model,
        "artifacts": str(bundle.dir) if bundle.dir is not None else None,
    }
    start = time.monotonic()
    try:
        captured = producer.run_range_review(
            backend,
            view,
            model=model,
            timeout=timeout,
            instructions_path=instructions_path,
            launcher=launcher,
            run_id=run_id,
            artifacts=bundle,
        )
    except Exception as exc:
        # Settle the bundle before propagating, joining the outcome to the streams.
        bundle.record(
            outcome="timed_out" if getattr(exc, "timed_out", False) else "failed",
            duration_ms=int((time.monotonic() - start) * 1000),
            error=str(exc),
        )
        raise
    review = captured.review
    run["usage"] = captured.usage.as_record()
    if captured.reasoning is not None:
        run["reasoning"] = captured.reasoning
    duration_ms = int((time.monotonic() - start) * 1000)
    run["duration_ms"] = duration_ms
    run["outcome"] = "success"
    run["findings"] = len(review.get("comments") or [])
    bundle.record(outcome="success", duration_ms=duration_ms, findings=run["findings"])
    record_path = roundrecord.record_round(
        review,
        repo_slug=view.repo.slug,
        pr=None,
        base_sha=str(view.base_sha),
        head_sha=str(view.head_sha),
        reviewer=agent,
        model=model,
        timeout=timeout,
        instructions_path=instructions_path,
        findings=roundrecord.dispositioned(review, run_id=run_id),
        runs=(run,),
        duration_ms=duration_ms,
        total_tokens=captured.usage.total_tokens,
        round_id=round_id,
        artifacts_dir=str(round_dir) if round_dir is not None else None,
        cell=cell,
        base_dir=base_dir,
    )
    logger.info(
        "replay review complete (agent=%s) over %s..%s in %dms — record at %s",
        agent,
        view.base_sha,
        view.head_sha,
        duration_ms,
        record_path,
        extra={"reviewer": agent, "duration_ms": duration_ms},
    )
    return {"review": review, "record_path": record_path}


def run_fanout_replay(
    backend: Backend,
    view: RangeView,
    *,
    model: str = "pro",
    timeout: str = "600s",
    instructions_path: str | None = None,
    dimensions: Sequence[str] | None = None,
    calibrator: CalibratorConfig | None = None,
    semantic_dedup: bool = False,
    nit_cap: int | None = None,
    invocation_overrides: Mapping[str, Mapping[str, str]] | None = None,
    cell: Mapping[str, Any] | None = None,
    launcher: launch.Runner | None = None,
    base_dir: Path | None = None,
) -> dict:
    """Fan-out-review ``view``'s range and write the round record."""
    # This driver is itself the opt-in, so unnamed means the default SET.
    dimensions = tuple(dimensions) if dimensions else DEFAULT_DIMENSION_NAMES
    _provision_replay_defs(view, backend, calibrator_on=calibrator is not None)
    agent = backend.funnel_agent or backend.name
    start = time.monotonic()
    outcome = fanout.run_fanout_review(
        backend,
        view,
        model=model,
        timeout=timeout,
        instructions_path=instructions_path,
        dimensions=dimensions,
        calibrator=calibrator,
        semantic_dedup=semantic_dedup,
        nit_cap=nit_cap,
        invocation_overrides=invocation_overrides,
        launcher=launcher,
        artifacts_base_dir=base_dir,
    )
    duration_ms = int((time.monotonic() - start) * 1000)
    record_path = roundrecord.record_round(
        outcome.review,
        repo_slug=view.repo.slug,
        pr=None,
        base_sha=str(view.base_sha),
        head_sha=str(view.head_sha),
        reviewer=agent,
        model=model,
        timeout=timeout,
        instructions_path=instructions_path,
        findings=outcome.findings,
        runs=outcome.runs,
        total_tokens=outcome.total_tokens,
        duration_ms=duration_ms,
        round_id=outcome.round_id,
        artifacts_dir=outcome.artifacts_dir,
        cell=cell,
        dimension_names=tuple(d.name for d in resolve_dimensions(dimensions)),
        dimension_overrides=invocation_overrides,
        base_dir=base_dir,
    )
    logger.info(
        "fan-out replay complete (agent=%s) over %s..%s in %dms — record at %s",
        agent,
        view.base_sha,
        view.head_sha,
        duration_ms,
        record_path,
        extra={"reviewer": agent, "duration_ms": duration_ms},
    )
    return {"review": outcome.review, "record_path": record_path}


def _provision_bundled_tree(root: Path, rel_dir: str, source) -> list[Path]:
    """Exclusive-create missing bundled files into ``root/rel_dir``; a symlinked component aborts."""
    from ..install.units import walk_files

    # Guard every component of the base dir chain before writing anything.
    probe = root
    for part in Path(rel_dir).parts:
        probe = probe / part
        if probe.is_symlink():
            logger.warning(
                "replay: refusing to provision agent-defs — %s is a symlink; "
                "leaving the untrusted checkout untouched",
                probe,
            )
            return []
    dest_dir = root / rel_dir
    written: list[Path] = []
    for rel, content in walk_files(source):
        dest = dest_dir / rel
        # One attacker-controlled component makes the whole tree suspect, so a
        # symlink aborts rather than skips.
        probe = dest_dir
        symlinked = False
        for part in Path(rel).parent.parts:
            probe = probe / part
            if probe.is_symlink():
                logger.warning(
                    "replay: refusing to provision %s — %s is a symlink; "
                    "leaving the untrusted checkout untouched",
                    rel,
                    probe,
                )
                symlinked = True
                break
        if symlinked:
            return written
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(dest, "xb") as fh:
                fh.write(content)
        except FileExistsError:
            continue
        written.append(dest)
    return written


def provision_agent_defs(workdir: str) -> list[Path]:
    """Provision the bundled role agent-defs into ``workdir``; returns the paths written."""
    from ..install.units import (
        AGENTS_DEF_DIR,
        agents_root,
    )

    root = Path(workdir).resolve()
    written: list[Path] = []
    written += _provision_bundled_tree(root, AGENTS_DEF_DIR, agents_root())
    if written:
        logger.info(
            "replay: provisioned %d role agent-def(s) into %s",
            len(written),
            root,
            extra={"files": len(written)},
        )
    return written
