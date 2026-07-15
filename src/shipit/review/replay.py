"""replay — review an arbitrary commit range offline: record written, no PR touched.

The sanctioned offline experiment driver (RVW02-WS03 single pass; RVW03-WS01
fan-out): ``shipit pr review replay <base>..<head>`` resolves a commit RANGE of
the current checkout (never a PR) and runs a local review backend over it —
either as ONE monolithic pass through the shared range producer
(:func:`run_replay` → :func:`shipit.review.producer.run_range_review`) or, with
``--fanout``, as the full dimension fan-out through the ONE fan-out
orchestrator (:func:`run_fanout_replay` →
:func:`shipit.review.fanout.run_fanout_review`, the SAME code path the live-PR
service drives) — and writes the resulting **Review-round record**
(:mod:`shipit.review.roundrecord`, ``round.pr = None``) to the local store —
the review path's NO-POST mode. Both arms read the diff the same way
(``git diff <base>..<head>`` in the checkout), so replays of the two arms over
the same range are comparable (no ``git diff`` vs ``gh pr diff`` skew). Nothing
on GitHub is read or written: no post, no check run, no review request. A
historical PR's round 1 replays as ``merge-base..first-round-head`` — which is
exactly what the three-dot spelling ``base...head`` resolves (the merge base is
computed here).

Range grammar (:func:`parse_range`): ``A..B`` reviews exactly the diff from
commit ``A`` to commit ``B``; ``A...B`` reviews from ``merge-base(A, B)`` to
``B`` (GitHub's "Files changed" semantics — the round-1 replay spelling). Both
endpoints are arbitrary revisions (branch, tag, sha, ``HEAD~2``); they are
resolved OFFLINE against the checkout — an unknown revision is a loud
:class:`~shipit.review.diff.ReviewError` telling the operator to fetch it, never
a silent fetch (replay is deliberately network-free).

The record write is NOT fail-open here (unlike the review-path tee): the record
IS replay's product, so a write failure fails the verb.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .. import execrun, git, identity
from ..agent.backend import ANTIGRAVITY, Backend
from ..identity import Sha
from ..spawn import launch
from . import artifacts as artifacts_mod
from . import fanout, producer, roundrecord
from .calibrator import CalibratorConfig
from .diff import RangeView, ReviewError
from .dimensions import DEFAULT_DIMENSION_NAMES, resolve_dimensions

logger = logging.getLogger("shipit.review")


def parse_range(spec: str) -> tuple[str, str, bool]:
    """Split a range SPEC into ``(base, head, merge_base_wanted)``. PURE.

    ``A..B`` → ``(A, B, False)`` (review exactly ``A``→``B``); ``A...B`` →
    ``(A, B, True)`` (review from the merge base of ``A`` and ``B`` — the
    round-1 replay spelling). Raises :class:`~shipit.review.diff.ReviewError`
    on anything else — no separator, an empty endpoint, or a dot-run longer
    than the separator (e.g. ``a....b``, whose extra dot would otherwise leak
    into an endpoint) — with the accepted grammar in the message, so a typo
    dies at parse, before any git work. A revision can carry an internal dot
    (a tag like ``v1.2.3``) but never a leading/trailing one, so a boundary
    dot is always a malformed separator.
    """
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
    """Resolve range ``spec`` against the checkout at ``workdir`` (default: cwd).

    OFFLINE by design: endpoints resolve against what the checkout already has
    (``git rev-parse``) — an unknown revision raises a
    :class:`~shipit.review.diff.ReviewError` telling the operator to fetch it,
    rather than replay silently reaching for the network. The checkout's origin
    identity is resolved too (the round record's repo key, ADR-0024): a
    checkout with no origin remote cannot key a record and fails loud. The
    three-dot spelling computes the merge base here and fails loud on unrelated
    histories, mirroring the PR path's no-silent-degrade contract.
    """
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
    """One range endpoint → its commit :class:`~shipit.identity.Sha`, or a loud
    :class:`ReviewError` — replay never fetches, so "unknown" means the operator
    fetches (or fixes the spelling) and re-runs."""
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
    """Provision the bundled role agent-defs into the replay checkout when a
    launch in THIS replay reads a ``--agent reviewer`` def from it, mapping a
    filesystem failure to the replay path's one clean :class:`ReviewError`.

    Two launches in a replay resolve ``--agent reviewer`` against the checkout
    (ABSENT in a bare experiment clone), so both require provisioning first:

    - the calibrator's dormant judge, ``claude --agent reviewer``
      (``.claude/agents``), whenever ``calibrator_on``; and
    - an ANTIGRAVITY primary reviewer backend, ``agy --agent reviewer``
      (``.agents/agents/reviewer/agent.md``, #989), on EVERY replay shape —
      single-pass :func:`run_replay` and uncalibrated fan-out alike, not only
      the calibrator case.

    A no-op when neither condition holds (a codex/claude primary with no judge
    needs nothing written). :func:`provision_agent_defs` writes only missing
    files, so provisioning both trees whenever either launch needs one is safe
    and idempotent. An :class:`OSError` (read-only checkout, permissions, a
    non-directory ``.claude``/``.agents`` component) is re-raised as a
    :class:`~shipit.review.diff.ReviewError` — the replay path's clean one-line
    refusal — BEFORE any model bills, never a raw traceback.
    """
    if not (calibrator_on or backend is ANTIGRAVITY):
        return
    try:
        provision_agent_defs(view.workdir)
    except OSError as exc:
        raise ReviewError(
            f"cannot provision the reviewer role agent-defs into "
            f"{view.workdir!r} ({exc}) — a replay launch (the ANTIGRAVITY "
            "reviewer, or the calibrator's judge) reads them from the checkout. "
            "Fix the checkout's writability, then re-run."
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
    """Review ``view``'s range with ``backend`` and WRITE the round record.

    The no-post pipeline: generate via the shared range producer, then write the
    **Review-round record** with ``round.pr = None`` (no PR was touched — the
    honest replay marker). The record's ``round.usage.total_tokens`` carries the
    launch's CLI-measured usage (RVW03-WS04; ``None`` when the backend's CLI
    reports none — the explicit latency-only marker). Returns ``{"review": …,
    "record_path": …}`` so the
    verb can render what was found and where the record landed. The record
    write PROPAGATES on failure — it is the product here, not telemetry (the
    review-path tee is the fail-open twin). ``cell`` (RVW03-WS07) is the
    experiment Cell tag the lab runner stamps onto the record's ``round.cell``
    (cell id + idempotency key; ``None`` for a plain replay). ``base_dir``
    overrides the store family root (tests) — the per-run artifact bundle
    (below) roots under the SAME injected family root; ``launcher`` injects
    the launch seam (tests).

    OBSERVABILITY (RVW03-WS02): the replay's single range pass is a review
    sub-agent run like any other, so it too persists a per-run artifact bundle
    (exact prompt, raw streams, meta — unconditional, fail-open) under a minted
    round id, and its record carries ``round.id`` / ``round.artifacts``, one
    ``round.runs`` entry, and the run's id on every finding — the same
    finding↔pass trail as the fan-out's, so replay evidence is as inspectable
    as a live round's.
    """
    # An ANTIGRAVITY primary reviewer launches `agy --agent reviewer`, which
    # reads `.agents/agents/reviewer/agent.md` from the checkout — absent in a
    # bare experiment clone. Provision it before the launch (no calibrator here).
    _provision_replay_defs(view, backend, calibrator_on=False)
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
        # The failure propagates (replay's record is its product) — but the
        # bundle settles first, so the prompt + raw streams the launch seam
        # already wrote are joined by the outcome on disk.
        bundle.record(
            outcome="timed_out" if getattr(exc, "timed_out", False) else "failed",
            duration_ms=int((time.monotonic() - start) * 1000),
            error=str(exc),
        )
        raise
    review = captured.review
    # RVW03-WS04: the single range pass carries its CLI-measured usage and the
    # applied reasoning on its round.runs entry, exactly like a fan-out pass.
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
    """Fan-out-review ``view``'s range with ``backend`` and WRITE the round record.

    The FAN-OUT arm of the no-post pipeline (RVW03-WS01) — the sanctioned way to
    run a fan-out experiment cell: the configured **Dimension passes** run
    offline over the range through the ONE fan-out orchestrator
    (:func:`shipit.review.fanout.run_fanout_review`, the same code path the
    live-PR service drives, handed the range-scoped ``view`` instead of a PR
    ctx), then the **Review-round record** is written with ``round.pr = None``
    exactly like the single-pass :func:`run_replay` — ``round.runs`` populated
    per pass (plus the calibrator's run when ``calibrator`` opts the dormant
    judge on), ``round.findings`` carrying the routing's real dispositions.
    ``dimensions`` defaults to the fan-out's default SET — the ADR-0045
    concern four (:data:`shipit.review.dimensions.DEFAULT_DIMENSION_NAMES`):
    calling this driver at all is the explicit fan-out opt-in (ADR-0052), so
    an unnamed set means "the fan-out, stock decomposition", never the
    orchestrator's single-pass default. ``nit_cap`` defaults to ``None``
    (uncapped): an offline experiment records everything; there is no PR to
    protect from nit churn. ``semantic_dedup`` (#750) opts the mechanical
    union dedup into the deterministic same-round near-duplicate collapse —
    the ``dedup = "semantic"`` Lab treatment, threaded verbatim to the
    orchestrator (which rejects it alongside a ``calibrator``: the judge does
    its own dedup). The role agent-defs are provisioned into the replay checkout
    first (:func:`_provision_replay_defs`) whenever a launch reads them — the
    judge's ``claude --agent reviewer`` when ``calibrator`` is set AND an
    ANTIGRAVITY primary reviewer's ``agy --agent reviewer`` on every fan-out
    (#989) — so the launch works in a clone that never committed them; a
    filesystem failure provisioning them is re-raised as a
    :class:`~shipit.review.diff.ReviewError` (the replay path's clean one-line
    refusal) rather than a raw ``OSError``.

    Returns ``{"review": …, "record_path": …}`` and PROPAGATES a record-write
    failure, both exactly as the single-pass arm does (the record is the
    product). ``invocation_overrides`` (RVW03-WS07) are the experiment-only
    per-dimension Invocation overrides (``{dimension name: {"model"/"timeout":
    …}}``) threaded to the orchestrator — a lab-cell capability, never Roster
    configuration (ADR-0049); ``cell`` is the Cell tag stamped onto the
    record's ``round.cell`` (cell id + idempotency key; ``None`` for a plain
    replay). ``base_dir`` overrides the store family root (tests); ``launcher``
    injects the launch seam (tests).
    """
    # This driver IS the explicit fan-out opt-in (a `shape = "fanout"` Lab
    # cell, or `shipit pr review replay --fanout`), so a call without a named
    # `dimensions` list means the fan-out's DEFAULT SET — the ADR-0045 concern
    # four — not the orchestrator's no-dimensions default, which is now the
    # single monolithic pass (ADR-0052). Resolve it here so the orchestrator
    # below runs the fan-out and the record folds the real pass set.
    dimensions = tuple(dimensions) if dimensions else DEFAULT_DIMENSION_NAMES
    # Provision the reviewer role agent-defs into the checkout before any model
    # bills when a launch in this fan-out reads them: the calibrator's judge
    # (`claude --agent reviewer`) when the judge is on, AND an ANTIGRAVITY primary
    # reviewer (`agy --agent reviewer`, #989) on EVERY fan-out — calibrated or not.
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
        # The round's RESOLVED pass set + overrides fold into round.variant
        # (#713): dimension focus texts are prompt material the instructions
        # file does not cover. Resolution cannot fail here — the orchestrator
        # above already ran the same names through resolve_dimensions.
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
    """Exclusive-create every bundled file under ``source`` into ``root/rel_dir``.

    The shared core of :func:`provision_agent_defs` (RVW02-WS08, extended for the
    AGY def in #989): walk the bundled ``source`` tree and write each file into
    the matching path under ``root/rel_dir``, MISSING-ONLY. Handles a NESTED
    source (the AGY def is ``reviewer/agent.md``, not a flat file) by mirroring the
    source's relative paths.

    Untrusted-checkout guard (RVW03-WS01): a SYMLINK anywhere in the destination
    directory chain — the ``rel_dir`` components OR an intermediate dir created for
    a nested file — could redirect the writes outside the checkout, so a symlinked
    component aborts this tree with nothing further written. Each file is created
    with exclusive ``open(..., "xb")``, so an existing name (a regular file OR a
    pre-planted symlink) is left untouched — never followed or truncated — which
    also makes concurrent replays on one checkout race-safe. Returns the paths
    written under this tree.
    """
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
        # Guard each intermediate dir a nested file needs (e.g. `reviewer/`).
        # A symlinked component ABORTS this tree fail-closed (nothing further
        # written) — the same fail-closed posture as the base-chain guard above:
        # once any destination component is attacker-controlled the whole tree is
        # suspect, so we stop rather than skip-and-keep-going.
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
    """Provision the bundled role agent-defs into ``workdir`` — both the Claude
    defs (``.claude/agents``) and the AGY reviewer def (``.agents/agents``, #989).

    The RVW02-WS08 op gap (#680): a reviewer/calibrator backend launches ``claude
    --agent reviewer`` (reads ``.claude/agents/reviewer.md``) or, on the agy arm,
    ``agy --agent reviewer`` (reads ``.agents/agents/reviewer/agent.md``) from the
    checkout it runs in — present in shipit-self and installed consumers, ABSENT
    in a bare experiment clone, where the launch fails. So a replay writes the
    bundled defs (:func:`shipit.install.units.agents_root` /
    :func:`~shipit.install.units.agy_agents_root` — the same sources ``shipit
    install`` vendors) into the replay checkout first. Only MISSING files are
    written: an existing def (committed, installed, or deliberately edited as an
    experiment arm) is the checkout's own and is never clobbered. Returns every
    path written across both trees (empty when everything was already present).

    Untrusted-checkout guard (RVW03-WS01): replay runs over a checkout the
    operator may not control, so the writes stay strictly inside the checkout —
    a symlinked destination component aborts that tree, and every file is
    exclusive-created (see :func:`_provision_bundled_tree`).
    """
    from ..install.units import (
        AGENTS_DEF_DIR,
        AGY_AGENTS_DEF_DIR,
        agents_root,
        agy_agents_root,
    )

    root = Path(workdir).resolve()
    written: list[Path] = []
    written += _provision_bundled_tree(root, AGENTS_DEF_DIR, agents_root())
    agy_source = agy_agents_root()
    if agy_source.is_dir():
        written += _provision_bundled_tree(root, AGY_AGENTS_DEF_DIR, agy_source)
    if written:
        logger.info(
            "replay: provisioned %d role agent-def(s) into %s",
            len(written),
            root,
            extra={"files": len(written)},
        )
    return written
