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
from collections.abc import Sequence
from pathlib import Path

from .. import execrun, git, identity
from ..agent.backend import Backend
from ..identity import Sha
from ..spawn import launch
from . import fanout, producer, roundrecord
from .calibrator import CalibratorConfig
from .diff import RangeView, ReviewError

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


def run_replay(
    backend: Backend,
    view: RangeView,
    *,
    model: str = "pro",
    timeout: str = "600s",
    instructions_path: str | None = None,
    launcher=None,
    base_dir: Path | None = None,
) -> dict:
    """Review ``view``'s range with ``backend`` and WRITE the round record.

    The no-post pipeline: generate via the shared range producer, then write the
    **Review-round record** with ``round.pr = None`` (no PR was touched — the
    honest replay marker). Returns ``{"review": …, "record_path": …}`` so the
    verb can render what was found and where the record landed. The record
    write PROPAGATES on failure — it is the product here, not telemetry (the
    review-path tee is the fail-open twin). ``base_dir`` overrides the store
    family root (tests); ``launcher`` injects the launch seam (tests).
    """
    agent = backend.funnel_agent or backend.name
    start = time.monotonic()
    review = producer.run_range_review(
        backend,
        view,
        model=model,
        timeout=timeout,
        instructions_path=instructions_path,
        launcher=launcher,
    )
    duration_ms = int((time.monotonic() - start) * 1000)
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
        duration_ms=duration_ms,
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
    nit_cap: int | None = None,
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
    ``nit_cap`` defaults to ``None`` (uncapped): an offline experiment records
    everything; there is no PR to protect from nit churn. When ``calibrator``
    is set, the role agent-defs are provisioned into the replay checkout first
    (:func:`provision_agent_defs`) so the judge's ``claude --agent reviewer``
    launch works in a clone that never committed them.

    Returns ``{"review": …, "record_path": …}`` and PROPAGATES a record-write
    failure, both exactly as the single-pass arm does (the record is the
    product). ``base_dir`` overrides the store family root (tests); ``launcher``
    injects the launch seam (tests).
    """
    if calibrator is not None:
        provision_agent_defs(view.workdir)
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
        nit_cap=nit_cap,
        launcher=launcher,
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
        duration_ms=duration_ms,
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


def provision_agent_defs(workdir: str) -> list[Path]:
    """Provision the bundled role agent-defs into ``workdir``'s ``.claude/agents``.

    The RVW02-WS08 op gap (#680): the Calibrator's default backend launches
    ``claude --agent reviewer``, which reads ``.claude/agents/reviewer.md`` from
    the checkout it runs in — present in shipit-self and installed consumers,
    ABSENT in a bare experiment clone, where the launch fails. So a fan-out
    replay with the judge on writes the bundled agent-defs
    (:func:`shipit.install.units.agents_root` — the same source ``shipit
    install`` vendors) into the replay checkout first. Only MISSING files are
    written: an existing def (committed, installed, or deliberately edited as an
    experiment arm) is the checkout's own and is never clobbered. Returns the
    paths written (empty when everything was already present).
    """
    from ..install.units import AGENTS_DEF_DIR, agents_root

    dest_dir = Path(workdir) / AGENTS_DEF_DIR
    written: list[Path] = []
    for entry in agents_root().iterdir():
        if not entry.is_file():
            continue
        dest = dest_dir / entry.name
        if dest.exists():
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(entry.read_bytes())
        written.append(dest)
    if written:
        logger.info(
            "replay: provisioned %d role agent-def(s) into %s",
            len(written),
            dest_dir,
            extra={"files": len(written)},
        )
    return written
