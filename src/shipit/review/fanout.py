"""fanout — round-1 review orchestration + union post (RVW02-WS04/WS08,
ADR-0045, ADR-0052).

The orchestration between the review producer and the posting service. By
DEFAULT (ADR-0052) a local-agent reviewer's round-1 detached review run is ONE
monolithic full-scope pass — Lab measurement (v37 fixture) found the
concern-scoped fan-out matched its confirmed-major recall at ~4x the token
cost, with a between-buckets blind spot the unscoped pass caught. An explicit
per-reviewer ``dimensions`` config (or a Lab ``shape = "fanout"`` cell) opts
back into the ADR-0045 fan-out: parallel **Dimension passes**
(:mod:`shipit.review.dimensions`) on the reviewer's own backend against ONE
per-Run read-only Tree, results unioned. Either shape flows through the same
union/dedup/routing machinery below.

By DEFAULT (RVW02-WS08) the union is posted through a MECHANICAL, deterministic
dedup (:func:`dedup_union`): findings sharing a ``(file, line, claim)`` merge
into one canonical that posts with its OWN pass-assigned severity — no LLM judge
in the default path. An OPT-IN treatment (#750, ``semantic_dedup`` — a Review
Lab cell's ``dedup = "semantic"``, never the product default until a cell earns
it) additionally collapses same-round NEAR-duplicates: same non-empty file and
same concrete line (or both file-scoped), claim-token overlap at the #673
seam's threshold (:func:`~shipit.review.match.same_claim` — still deterministic
and LLM-free, ADR-0048). The WS05/F2 baseline (#638, #665) showed the LLM
**Calibrator** (:mod:`shipit.review.calibrator`) net-negative on round-1 major
recall (it refuted a true major the passes found, dragging recall below the
single-pass baseline), so it is OPTIONAL and OFF by default. It is kept warm —
concept, config, hooks, and the F2 reproduction-based floor all wired but
dormant (the ADR-0044 ``classify`` pattern) — and it is opted back on by setting
the table-level ``[reviewers].calibrator`` key (one shared judge, not a
per-reviewer entry); when on it dedups,
adversarially verifies, normalizes severity onto the one ruler, and assigns
every judged finding a **Disposition**.

What this module owns:

  * the pass fan-out (preflight every configured backend binary ONCE before
    anything launches — :func:`shipit.review.producer.preflight_round`, so a
    missing binary is one actionable error and zero passes start; provision the
    Tree once; launch the configured dimension set in parallel through
    :func:`shipit.review.producer.run_tree_review`; tolerate per-pass failures
    — a pass failure degrades coverage, it never kills the round unless EVERY
    pass failed);
  * the union (each successful pass's comments, coerced through the ONE trust
    boundary :func:`shipit.review.schema.finding_from_dict`, tagged with the
    dimension that found them AND the ``run_id`` of the pass that emitted them
    — the RVW03-WS02 finding↔pass correlation) and the merged coverage
    attestation;
  * the round's OBSERVABILITY trail (RVW03-WS02): one **round id** per fan-out,
    a per-run artifact bundle (:mod:`shipit.review.artifacts` — exact prompt,
    raw streams, machine-readable meta, written unconditionally and fail-open)
    for every pass and the calibrator, ``review.pass.launched`` /
    ``review.pass.settled`` progress events with ``run_id`` / ``dimension`` /
    ``round_id`` extras so parallel passes are separable in the log sink and a
    coordinating agent can watch a multi-minute round live;
  * the default MECHANICAL dedup (:func:`dedup_union`) that merges same-location
    same-claim candidates into one canonical carrying its pass severity — the
    off-path replacement for the LLM judge;
  * the deterministic post routing (:func:`route_calibrated`), shared by both
    paths: duplicates never post, round-1 nits post under the TABLE-LEVEL nit
    cap (over-cap nits flip to ``nit-suppressed``, recorded; ``0`` floors the
    posted review at minor), the posted status derives from what posts
    (major-or-worse → ``REQUEST_CHANGES``); and
  * the round's contributing-run trail: one entry per pass (and, when the
    calibrator is on, one for it), each with a run id, the **Variant** hash
    of the exact prompt that ran, the run's measured token ``usage`` as its
    CLI reported it (explicitly-unknown otherwise, RVW03-WS04), and — where a
    ReasoningLevel actually reached argv — the applied ``reasoning`` — what
    the review-round record's ``round.runs`` carries and ``shipit eval
    report`` reads its cost axis from (WS03/RVW03-WS04).

The fan-out is INVISIBLE below the reviewer boundary (ADR-0045): the service
posts ONE review through the reviewer's own bot exactly as before; the funnel,
reconcile, and prstate machinery are untouched. An EMPTY union has nothing to
post — it skips both the dedup and the (dormant) calibrator and posts the
attested clean review.

The offline fan-out replay (RVW03-WS01) drives this SAME orchestration: the
sanctioned experiment driver (:func:`shipit.review.replay.run_fanout_replay`)
hands :func:`run_fanout_review` a range-scoped
:class:`~shipit.review.diff.RangeView` instead of a PR ctx, and the three
PR-coupled seams dispatch on it — no Tree (the passes run in the replay
checkout), range tasks reading ``git diff <base>..<head>``
(:func:`shipit.review.producer.run_range_review` /
:func:`~shipit.review.producer.range_pass_task_text`), and the calibrator's
ground truth the same range diff. Everything else — union, dedup/calibration,
routing, run trail — is one code path for both arms.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import events
from ..agent import backend as agent_backend
from ..agent.backend import Backend
from ..finding import (
    DEFAULT_SEVERITY,
    Disposition,
    Finding,
    JudgedFinding,
    Severity,
    parse_severity,
)
from ..harness.eval.variant import label_from_env, variant_of
from ..spawn import launch
from . import artifacts as artifacts_mod
from . import producer
from .calibrator import (
    CalibratedFinding,
    CalibratorConfig,
    run_calibrator,
)
from .diff import RangeView
from .dimensions import Dimension, known_dimension_names, resolve_dimensions
from .match import Claim, same_claim
from .schema import finding_from_dict
from .usage import UNREPORTED

logger = logging.getLogger("shipit.review")


@dataclass(frozen=True)
class FanoutOutcome:
    """One fan-out round's product, ready for the service seam.

    ``review`` is the routed REVIEW_SCHEMA-shaped dict the posting path
    consumes unchanged (the fan-out's invisibility below the reviewer
    boundary) — the deduped union by default, or the calibrated result when the
    dormant judge is on; ``findings`` is the FULL judged set as
    :class:`JudgedFinding`\\ s
    — routed-out findings AND merged-away duplicates included, never erased (the
    round record's Opportunity-harvest seam), each carrying the ``run_id`` of
    the pass that originated it (RVW03-WS02); ``runs`` the contributing-run
    entries (every pass + the calibrator: run ids, variant hashes, artifact
    bundle paths, per-run ``usage`` as the CLI reported it, and ``reasoning``
    where a level actually reached argv — RVW03-WS04) for ``round.runs``;
    ``total_tokens`` the round's measured token cost — the sum of the runs'
    REPORTED usage, ``None`` when no contributing run reported any (an explicitly
    latency-only round, never a fabricated zero); ``round_id`` the
    fan-out-minted round identity and ``artifacts_dir`` the directory this
    round's per-run bundles live under (``None`` when no bundle could be keyed —
    a hand-built ctx with no repo identity, or a dry run) — what the round record
    persists as ``round.id`` / ``round.artifacts`` so the bundles are
    discoverable from the record.
    """

    review: dict
    findings: tuple[JudgedFinding, ...]
    runs: tuple[dict[str, Any], ...]
    total_tokens: int | None = None
    round_id: str = ""
    artifacts_dir: str | None = None


@dataclass(frozen=True)
class _PassResult:
    """One dimension pass's outcome: its run entry + the review it captured
    (``None`` when the pass failed — the run entry carries the why)."""

    dimension: Dimension
    run: dict[str, Any]
    review: dict | None


#: The shipped cheaper **ReasoningLevel** an INCREMENTAL round's single pass runs
#: at (RVW02-WS06, ADR-0045). Round 1 is exhaustive; rounds after it review only
#: the fix range, so they run cheaper. Since RVW03-WS04 (#685) this is a REAL argv
#: request, no longer a record-only stamp: it is threaded through
#: :func:`~shipit.review.producer.run_tree_review` to the backend adapter, which
#: applies it where the CLI has a knob (codex ``-c model_reasoning_effort``) and
#: drops it where it has none (agy). The run entry's ``reasoning`` is stamped from
#: what the adapter ACTUALLY applied — a knob-less backend records unset, never
#: this config value. :func:`run_fanout_review` takes it as an argument
#: (defaulting here), but no ``[reviewers]`` config key wires it and the service
#: never overrides the default; moving it today means changing this constant.
DEFAULT_INCREMENTAL_REASONING = "low"

#: The synthetic **Dimension** an INCREMENTAL round's single pass carries so it
#: flows through the SAME union / coverage / attestation machinery as a round-1
#: dimension pass (RVW02-WS06). A round after the first is ONE full-scope pass over
#: the fix range, NOT a dimension fan-out — so this is not a member of the closed
#: :data:`shipit.review.dimensions.DIMENSIONS` registry; it exists only to label the
#: incremental pass in the record and attestation.
_INCREMENTAL_DIMENSION = Dimension(
    name="incremental",
    title="Incremental fix-range",
    focus="the fix range only, with mandatory dependency-neighborhood context",
)

#: The synthetic **Dimension** the round-1 DEFAULT single pass carries
#: (ADR-0052). The default round-1 shape is ONE monolithic full-scope pass —
#: the pass launches with ``dimension=None`` (the unscoped task), and this
#: label exists only so the pass rides the SAME union / coverage / attestation
#: / record machinery as a fan-out pass, exactly like the incremental round's
#: synthetic dimension above. The name matches the Review Lab's ``single``
#: shape token (:data:`shipit.review.cell.SHAPES`) so records read as one
#: vocabulary. Not a member of the closed
#: :data:`shipit.review.dimensions.DIMENSIONS` registry: a config
#: ``dimensions`` list cannot name it — listing dimensions IS the fan-out
#: opt-in.
_SINGLE_PASS_DIMENSION = Dimension(
    name="single",
    title="Single full-scope pass",
    focus="the full diff, unscoped — one monolithic full-scope pass",
)


def run_fanout_review(
    backend: Backend,
    target,
    *,
    model: str = "pro",
    timeout: str = "600s",
    instructions_path: str | None = None,
    dimensions: Sequence[str] | None = None,
    calibrator: CalibratorConfig | None = None,
    semantic_dedup: bool = False,
    nit_cap: int | None = None,
    invocation_overrides: Mapping[str, Mapping[str, str]] | None = None,
    incremental: bool = False,
    incremental_reasoning: str = DEFAULT_INCREMENTAL_REASONING,
    dry_run: bool = False,
    launcher: launch.Runner | None = None,
    artifacts_base_dir: Path | None = None,
    review_tree_naming: Mapping[str, str] | None = None,
) -> FanoutOutcome:
    """Run ``backend``'s round-1 review of ``target`` — ONE monolithic
    full-scope pass by default (ADR-0052), or the dimension fan-out when
    ``dimensions`` explicitly opts in — or ONE incremental fix-range pass
    (round ≥ 2), and return the routed :class:`FanoutOutcome`.

    ``target`` is EITHER a PR review view (the live path — provisions the
    per-Run read-only Tree; a round-1 pass fetches ``gh pr diff``, a round-≥2
    ``incremental`` pass the fix-range ``git diff <base>..<head>`` instead — see
    ``incremental`` below) or a range-scoped
    :class:`~shipit.review.diff.RangeView` (the offline fan-out replay,
    RVW03-WS01 — the passes run in the replay checkout over
    ``git diff <base>..<head>``, and the calibrator's ground truth is that same
    range diff). The dispatch happens at the three PR-coupled seams only;
    union, dedup/calibration, routing, and the run trail are ONE code path for
    both arms — the sanctioned replacement for the retired transient
    monkey-patch driver (#680).

    By DEFAULT (``calibrator=None``, RVW02-WS08) the union is posted through the
    MECHANICAL dedup (:func:`dedup_union`) using each pass's OWN severity — no
    model run. A ``calibrator`` :class:`CalibratorConfig` opts the dormant LLM
    judge back on (the WS05/F2 baseline found it net-negative on major recall,
    #638/#665), routing the union through :func:`run_calibrator` instead.
    ``semantic_dedup`` (#750) opts the mechanical dedup into the additional
    NEAR-duplicate collapse (same non-empty file and same concrete line, or
    both file-scoped; differently-worded same claim — :func:`dedup_union` with
    ``semantic=True``; still deterministic and LLM-free): a Review Lab
    treatment (a cell's ``dedup = "semantic"``), never
    the product default until a cell earns it. It is the OFF path's knob, so
    combining it with a ``calibrator`` is a caller error (``ValueError``) — the
    judge does its own semantic dedup, and running both would post an arm no
    config declares.

    ``incremental`` (RVW02-WS06, ADR-0045) selects the round-≥2 shape: ONE
    full-scope pass over the FIX RANGE — ``target.base_sha..target.head_sha``,
    where ``target`` is the caller's fix-range-rescoped view
    (:func:`shipit.review.diff.rescoped_view`) — with mandatory
    dependency-neighborhood context, INSTEAD of the parallel dimension fan-out.
    The pass runs at ``incremental_reasoning`` (the cheaper level, stamped on its
    run entry) and NEW NITS ARE SUPPRESSED: the routing runs with an effective
    ``nit_cap`` of ``0``, so every fresh nit routes ``nit-suppressed`` (recorded,
    not posted) — a late round can't be recolonized by style churn. The
    calibrator, if configured, still runs (single-pass + calibrator). Round 1
    (``incremental=False``) runs the ``dimensions`` shape (default single pass,
    explicit list → fan-out — see below) with the table-level ``nit_cap``.
    ``incremental`` is a LIVE-PR shape only: rounds are
    keyed to a live PR head, so an incremental :class:`RangeView` call is a
    caller error (``ValueError`` — multi-round fix-range replay is explicitly
    out of the Review Lab's scope), as is a :class:`RangeView` ``dry_run`` (the
    dry-run contract prints a would-run TREE launch; replay has none).

    ``dimensions`` is BOTH the round-1 shape switch and the pass set
    (ADR-0052): ``None``/empty — the shipped default — runs ONE monolithic
    full-scope pass (no dimension scoping; the pass carries the synthetic
    ``single`` label through the union/record machinery), while an explicit
    non-empty list (the per-reviewer Roster option, or the fan-out replay
    arm's resolved set) opts into the ADR-0045 dimension fan-out with exactly
    the named passes. Used only in round 1. ``calibrator`` is
    the table-level judge config (``None`` → judge OFF, deduped
    union); ``nit_cap`` the table-level round-1 nit budget (``None`` → uncapped,
    ``0`` → floor at minor; IGNORED in an incremental round, which forces ``0``).
    ``model`` / ``timeout`` / ``instructions_path`` are the reviewer's own run
    options and apply to every pass, exactly as they applied to the monolithic
    run — except where ``invocation_overrides`` (RVW03-WS07) narrows one pass:
    a ``{dimension name: {"model"/"timeout": …}}`` mapping, the Review Lab's
    experiment-only per-dimension Invocation capability (ADR-0049 — it lives
    in the lab runner's cells, deliberately NOT in Roster configuration; the
    live service never passes it). Each overridden pass launches AND records
    with its own model/timeout (the run entry and bundle meta stamp the actual
    values, so the arm is never mislabeled). An override naming a dimension
    outside this round's pass set — or any override in an ``incremental`` or
    default single-pass round, neither of which has dimension passes — is a
    loud ``ValueError``: a silently-ignored override would run a different
    experiment than the reviewed cell file declares.

    Failure posture: every configured backend binary (the reviewer's own plus,
    when the judge is on, the calibrator's) is preflighted ONCE before the Tree
    is provisioned or any pass launches
    (:func:`shipit.review.producer.preflight_round`, RVW03-WS03) — a missing
    binary raises ONE actionable
    :class:`~shipit.review.backends.base.BackendUnavailable` naming it, and no
    pass processes start. Past preflight, a SINGLE pass failure is tolerated —
    its run entry records the outcome, the posted summary attests the degraded
    coverage; ALL passes failing raises ``RuntimeError`` (the service maps it
    to the ``failed`` funnel outcome; in an incremental or default single-pass
    round the sole pass failing IS all passes failing). When the judge is ON, a calibrator failure (unavailable /
    timed out / unparseable / contract-violating output) PROPAGATES — an
    uncalibrated union is never posted under the judge's ruler; the round
    degrades non-blocking exactly like a failed monolithic review (ADR-0006).
    The default dedup path is pure and cannot fail this way.

    OBSERVABILITY (RVW03-WS02): the fan-out mints ONE round id per invocation
    and, for EVERY launched run (each pass and the calibrator, success and
    failure alike), persists a per-run artifact bundle
    (:mod:`shipit.review.artifacts` — exact prompt, raw streams, meta) under
    ``<state-root>/review-artifacts/<owner>/<name>/<round_id>/``, fail-open;
    each run entry carries its bundle path as ``artifacts``, each finding the
    ``run_id`` of the pass that emitted it, and every pass emits
    ``review.pass.launched`` / ``review.pass.settled`` progress events with
    ``run_id``/``dimension``/``round_id`` extras. ``artifacts_base_dir``
    overrides the bundle family root (tests), mirroring the store's
    ``base_dir``. ``review_tree_naming`` (#1039) applies ONLY to the LIVE PR
    path, which provisions a per-Run read-only Tree: the reviewer-spawn
    coordinator's pre-minted flat-leaf coordinates thread straight through to
    :func:`shipit.review.producer.provision_review_tree`, so the Tree the live
    path clones lands at the SPAWNED payload's ``tree`` path; ``None`` (every
    non-reviewer-spawn caller) lets the producer mint that leaf itself. The
    offline replay (a ``RangeView`` target) provisions NO Tree at all — it
    reviews ``RangeView.workdir`` directly, so the naming is moot there.

    With ``dry_run=True``: prints each pass's would-run argv (one per
    dimension, or the one monolithic/incremental pass, no clone, no model
    bill) plus a
    note on how the union would be posted (mechanical dedup, or the configured
    calibrator), and returns an empty outcome — the same honest dry-run contract
    as the producer's.
    """
    range_view = target if isinstance(target, RangeView) else None
    if range_view is not None and incremental:
        raise ValueError(
            "run_fanout_review: incremental and a RangeView target are mutually "
            "exclusive — rounds are keyed to a live PR head, and multi-round "
            "fix-range replay is out of the Review Lab's scope"
        )
    if range_view is not None and dry_run:
        raise ValueError(
            "run_fanout_review: dry_run is not supported for a RangeView target "
            "— the dry-run contract prints a would-run Tree launch, and an "
            "offline replay has no Tree"
        )
    if semantic_dedup and calibrator is not None:
        raise ValueError(
            "run_fanout_review: semantic_dedup and a calibrator are mutually "
            "exclusive — semantic_dedup is the mechanical (judge-off) path's "
            "near-duplicate collapse (#750), and the calibrator does its own "
            "dedup; both at once would post an arm no config declares"
        )
    if invocation_overrides and incremental:
        raise ValueError(
            "run_fanout_review: invocation_overrides and incremental are "
            "mutually exclusive — an incremental round runs one fix-range "
            "pass, not dimension passes"
        )
    if invocation_overrides and not dimensions:
        raise ValueError(
            "run_fanout_review: invocation_overrides require an explicit "
            "`dimensions` fan-out — the round-1 default is one monolithic "
            "pass (ADR-0052), which has no dimension passes to override"
        )
    incremental_range: tuple[str, str] | None = None
    single = False
    if incremental:
        incremental_range = (str(target.base_sha), str(target.head_sha))
        dims = (_INCREMENTAL_DIMENSION,)
        effective_nit_cap = 0
    elif not dimensions:
        # The round-1 DEFAULT shape (ADR-0052): ONE monolithic full-scope pass.
        # The synthetic label rides the union/record machinery; the pass itself
        # launches unscoped (dimension=None → the full-scope task).
        single = True
        dims = (_SINGLE_PASS_DIMENSION,)
        effective_nit_cap = nit_cap
    else:
        try:
            dims = resolve_dimensions(dimensions)
        except KeyError as exc:
            raise ValueError(
                f"unknown review dimension {exc.args[0]!r} — known dimensions: "
                f"{', '.join(known_dimension_names())}"
            ) from None
        effective_nit_cap = nit_cap
    if invocation_overrides:
        unknown = sorted(set(invocation_overrides) - {d.name for d in dims})
        if unknown:
            raise ValueError(
                f"invocation_overrides name dimension(s) outside this round's "
                f"pass set: {', '.join(unknown)} (passes: "
                f"{', '.join(d.name for d in dims)})"
            )
    agent = backend.funnel_agent or backend.name
    # The round's ONE shape vocabulary: `scoped` gates the per-pass dimension
    # slice (only fan-out passes are narrowed; the monolithic and incremental
    # passes launch with dimension=None → the full-scope task), `pass_word`
    # labels the run entries, events, and log lines consistently.
    scoped = not incremental and not single
    pass_word = "incremental" if incremental else ("single" if single else "dimension")
    # The one display/telemetry split between the arms: a PR target logs and
    # emits as `pr#<n>`; a range target has no PR — its label is the range and
    # its `pr` extra is OMITTED (the domain-key contract is absent-not-null, so a
    # range record carries no `pr` key rather than `pr: null` — logcontext drops
    # None from bound keys, but a per-call `extra` does not, so drop it here).
    pr_number = None if range_view is not None else target.number
    pr_extra = {"pr": pr_number} if pr_number is not None else {}
    where = (
        f"range {range_view.base_sha}..{range_view.head_sha}"
        if range_view is not None
        else f"pr#{target.number}"
    )

    if dry_run:
        for dim in dims:
            producer.run_tree_review(
                backend,
                target,
                model=model,
                timeout=timeout,
                instructions_path=instructions_path,
                dry_run=True,
                dimension=dim if scoped else None,
                incremental_range=incremental_range,
            )
        if calibrator is None:
            collapse = (
                " with the semantic near-duplicate collapse (#750)"
                if semantic_dedup
                else ""
            )
            print(
                "(dry-run: calibrator OFF — would post the mechanically-deduped "
                f"union{collapse} using each pass's own severity)"
            )
        else:
            print(
                f"(dry-run: would calibrate the union with {calibrator.backend} "
                f"[model={calibrator.model or 'default'}, "
                f"reasoning={calibrator.reasoning}])"
            )
        return FanoutOutcome(
            review={
                "summary": {"status": "COMMENT", "overall_feedback": "(dry-run)"},
                "comments": [],
            },
            findings=(),
            runs=(),
        )

    # Round-level preflight (RVW03-WS03): every configured backend binary is
    # checked ONCE, before the Tree is provisioned or any pass launches — a
    # missing binary is ONE actionable BackendUnavailable naming the binary,
    # never "all N dimension passes failed" with N truncated per-pass details.
    # Both arms need it: the range replay launches the same backends, it just
    # skips the Tree below.
    round_backends = [backend]
    if calibrator is not None:
        round_backends.append(agent_backend.by_name(calibrator.backend))
    producer.preflight_round(round_backends)

    # The Tree seam: the live path provisions ONE per-Run read-only Tree on the
    # PR head; the offline replay reviews the checkout whose range was resolved
    # — no Tree, no gh (the replay boundary already validated the endpoints).
    workdir = (
        range_view.workdir
        if range_view is not None
        else producer.provision_review_tree(target, backend, naming=review_tree_naming)
    )
    label = label_from_env()
    # The round's observability identity (RVW03-WS02): ONE round id per fan-out
    # invocation, keying the per-run artifact bundles beside the round store. A
    # target (PR view or RangeView) with no usable repo identity disables the
    # bundles (fail-open) — the round still runs, its record just carries no
    # artifacts location.
    round_id = uuid.uuid4().hex
    # `round_root` keys on the owner/name SLUG: a PR view's `.repo` already IS
    # that slug (str), a RangeView's `.repo` is a `Repo` whose `.slug` is it.
    repo_slug = (
        range_view.repo.slug
        if range_view is not None
        else getattr(target, "repo", None)
    )
    round_dir = artifacts_mod.round_root(
        repo_slug, round_id, base_dir=artifacts_base_dir
    )

    def _one_pass(dim: Dimension) -> _PassResult:
        # The per-dimension Invocation override seam (RVW03-WS07): the lab's
        # experiment-only capability — this pass's model/timeout, everything
        # below stamps the ACTUAL values so the arm is never mislabeled.
        override = (invocation_overrides or {}).get(dim.name, {})
        pass_model = override.get("model", model)
        pass_timeout = override.get("timeout", timeout)
        if range_view is not None:
            task = producer.range_pass_task_text(
                backend,
                range_view,
                instructions_path=instructions_path,
                dimension=dim if scoped else None,
            )
        else:
            task = producer.pass_task_text(
                backend,
                target.number,
                instructions_path=instructions_path,
                dimension=dim if scoped else None,
                incremental_range=incremental_range,
            )
        run_id = uuid.uuid4().hex
        kind = f"{pass_word}-pass"
        bundle = artifacts_mod.RunArtifacts.under(round_dir, run_id)
        run: dict[str, Any] = {
            "run_id": run_id,
            "kind": kind,
            "dimension": dim.name,
            "backend": agent,
            "model": pass_model,
            "variant": variant_of(task, label=label).as_record(),
            "artifacts": str(bundle.dir) if bundle.dir is not None else None,
            # Explicitly-unknown until the launch reports back (RVW03-WS04): a
            # failed pass keeps this honest "we do not know", never a zero.
            "usage": UNREPORTED.as_record(),
        }
        if incremental:
            run["range"] = {"base": incremental_range[0], "head": incremental_range[1]}
        # The run's identity facts land in the bundle meta up front, so even a
        # pass that dies mid-launch leaves a self-describing bundle.
        bundle.record(
            run_id=run_id,
            round_id=round_id,
            kind=kind,
            dimension=dim.name,
            backend=agent,
            model=pass_model,
            variant=run["variant"],
            pr=pr_number,
        )
        # The per-pass correlation extras (RVW03-WS02): every log record and
        # event this pass emits carries them, so the 4 parallel passes'
        # interleaved lines group post-mortem — and `shipit logs --run/--round`
        # can slice to one pass or one round. `pr` is omitted for a range replay
        # (absent-not-null), so a range record's events carry no `pr` key.
        correlation = {
            **pr_extra,
            "reviewer": agent,
            "run_id": run_id,
            "round_id": round_id,
            "dimension": dim.name,
        }
        events.emit(
            logger,
            "review.pass.launched",
            "%s pass %s launched for %s (agent=%s)",
            pass_word,
            dim.name,
            where,
            agent,
            extra=correlation,
        )
        start = time.monotonic()
        try:
            if range_view is not None:
                # Offline fan-out replay (RVW03-WS01): the pass reviews the
                # range in the replay checkout. Round-1 shape, so no incremental
                # ReasoningLevel request — usage/reasoning still ride the capture.
                captured = producer.run_range_review(
                    backend,
                    range_view,
                    model=pass_model,
                    timeout=pass_timeout,
                    instructions_path=instructions_path,
                    launcher=launcher,
                    dimension=dim if scoped else None,
                    run_id=run_id,
                    artifacts=bundle,
                )
            else:
                captured = producer.run_tree_review(
                    backend,
                    target,
                    model=pass_model,
                    timeout=pass_timeout,
                    instructions_path=instructions_path,
                    launcher=launcher,
                    dimension=dim if scoped else None,
                    tree_path=workdir,
                    incremental_range=incremental_range,
                    # The cheaper incremental ReasoningLevel is a real argv
                    # REQUEST (RVW03-WS04): the adapter applies it where the
                    # CLI has a knob.
                    reasoning=incremental_reasoning if incremental else None,
                    run_id=run_id,
                    artifacts=bundle,
                )
        except Exception as exc:  # noqa: BLE001 - a pass failure degrades, never kills
            run["duration_ms"] = int((time.monotonic() - start) * 1000)
            run["outcome"] = (
                "timed_out" if getattr(exc, "timed_out", False) else "failed"
            )
            # The detail string stays a truncated SUMMARY; the FULL raw output
            # lives in the bundle the run entry's `artifacts` points at
            # (written at the launch seam, success and failure alike).
            run["detail"] = str(exc)[:500]
            bundle.record(
                outcome=run["outcome"],
                duration_ms=run["duration_ms"],
                error=str(exc),
            )
            logger.warning(
                "%s pass %s failed for %s (agent=%s) — coverage degrades, "
                "the round continues",
                pass_word,
                dim.name,
                where,
                agent,
                exc_info=True,
                extra=correlation,
            )
            events.emit(
                logger,
                "review.pass.settled",
                "%s pass %s settled %s for %s in %dms",
                pass_word,
                dim.name,
                run["outcome"],
                where,
                run["duration_ms"],
                extra={
                    **correlation,
                    "outcome": run["outcome"],
                    "duration_ms": run["duration_ms"],
                },
            )
            return _PassResult(dimension=dim, run=run, review=None)
        review = captured.review
        run["duration_ms"] = int((time.monotonic() - start) * 1000)
        run["outcome"] = "success"
        run["findings"] = len(review.get("comments") or [])
        run["usage"] = captured.usage.as_record()
        if captured.reasoning is not None:
            # Stamped from the argv ACTUALLY used (RVW03-WS04) — absent when no
            # level was applied (unset, or the backend has no knob), so the
            # record never echoes a config value that did not run.
            run["reasoning"] = captured.reasoning
        bundle.record(
            outcome="success",
            duration_ms=run["duration_ms"],
            findings=run["findings"],
        )
        events.emit(
            logger,
            "review.pass.settled",
            "%s pass %s settled success for %s in %dms (%d finding(s))",
            pass_word,
            dim.name,
            where,
            run["duration_ms"],
            run["findings"],
            extra={
                **correlation,
                "outcome": "success",
                "duration_ms": run["duration_ms"],
                "findings": run["findings"],
            },
        )
        return _PassResult(dimension=dim, run=run, review=review)

    with ThreadPoolExecutor(max_workers=len(dims)) as pool:
        results = list(pool.map(_one_pass, dims))

    runs = [r.run for r in results]
    succeeded = [r for r in results if r.review is not None]
    failed = [r for r in results if r.review is None]
    if not succeeded:
        details = "; ".join(
            f"{r.dimension.name}: {r.run.get('detail', 'failed')}" for r in failed
        )
        if incremental:
            kind = "the incremental pass failed"
        elif single:
            kind = "the single review pass failed"
        else:
            kind = f"all {len(dims)} dimension passes failed"
        raise RuntimeError(f"{kind} for {where} (agent={agent}) — {details}")

    union = _build_union(succeeded)
    coverage = _merge_coverage(succeeded)
    artifacts_dir = str(round_dir) if round_dir is not None else None

    calibrated = calibrator is not None
    if not union:
        # Nothing to post: neither the mechanical dedup nor the (dormant, never
        # originating) calibrator has anything to do with an empty union. Post
        # the attested clean review.
        review = {
            "summary": {
                "status": "COMMENT" if failed else "APPROVED",
                "overall_feedback": _attestation(
                    dims,
                    failed,
                    union_size=0,
                    entries=(),
                    posted=0,
                    calibrated=calibrated,
                ),
                "coverage": coverage,
            },
            "comments": [],
        }
        return FanoutOutcome(
            review=review,
            findings=(),
            runs=tuple(runs),
            total_tokens=_round_total(runs),
            round_id=round_id,
            artifacts_dir=artifacts_dir,
        )

    if calibrator is None:
        # DEFAULT (RVW02-WS08): post the MECHANICALLY-deduped union using each
        # pass's own severity — no model run, no LLM judge. `semantic_dedup`
        # (#750, the Lab treatment) adds the deterministic near-duplicate
        # collapse on top of the exact-claim merge.
        entries = dedup_union(union, semantic=semantic_dedup)
        feedback = ""
    else:
        # Dormant judge opted back on: route the union through the calibrator.
        # Its bundle directory is the fixed `calibrator` name (one judge per
        # round; its TRUE run id — the claude session id — is known only after
        # the launch and lands in the bundle meta + the run entry below).
        calibrator_bundle = artifacts_mod.RunArtifacts.under(round_dir, "calibrator")
        calibrator_bundle.record(
            round_id=round_id,
            kind="calibrator",
            backend=calibrator.backend,
            model=calibrator.model,
            reasoning=calibrator.reasoning,
            pr=pr_number,
        )
        calibrator_run: dict[str, Any] = {
            "kind": "calibrator",
            "backend": calibrator.backend,
            "model": calibrator.model,
            # NB (RVW03-WS04): the APPLIED reasoning is stamped below from the
            # judge run, never `calibrator.reasoning` here — a knob-less backend
            # (agy) must record unset, not the echoed config value.
            "artifacts": (
                str(calibrator_bundle.dir)
                if calibrator_bundle.dir is not None
                else None
            ),
        }
        # RVW03-WS02 correlation: the calibrator's STABLE surrogate run id is the
        # fixed `calibrator` bundle name — its true backend session id is known
        # only post-launch (it lands in the run entry + bundle meta below). The
        # passes correlate by their real run ids; the one judge per round
        # correlates by this fixed name, so `shipit logs --run calibrator` slices
        # its whole trail — launch, settle (success OR failure), and the raw-
        # output DEBUG record inside `run_calibrator` — even when a pre-id failure
        # means no true run id ever exists. `pr` is omitted for a range replay
        # (absent-not-null), matching the passes' correlation.
        calibrator_correlation = {
            **pr_extra,
            "reviewer": agent,
            "run_id": "calibrator",
            "round_id": round_id,
            "dimension": "calibrator",
        }
        events.emit(
            logger,
            "review.pass.launched",
            "calibrator (%s) launched over %d candidate(s) for %s",
            calibrator.backend,
            len(union),
            where,
            extra=calibrator_correlation,
        )
        start = time.monotonic()
        try:
            judged_run = run_calibrator(
                calibrator,
                union,
                pr_number=pr_number,
                commit_range=(
                    (str(range_view.base_sha), str(range_view.head_sha))
                    if range_view is not None
                    else None
                ),
                cwd=workdir,
                launcher=launcher,
                artifacts=calibrator_bundle,
                correlation=calibrator_correlation,
            )
        except Exception as exc:
            # The failure PROPAGATES (an uncalibrated union never posts under
            # the judge's ruler) — but it settles observably first: the bundle
            # carries the outcome (the launch seam already wrote the raw
            # streams) and the settled event closes the progress trail.
            duration_ms = int((time.monotonic() - start) * 1000)
            calibrator_bundle.record(
                outcome=("timed_out" if getattr(exc, "timed_out", False) else "failed"),
                duration_ms=duration_ms,
                error=str(exc),
            )
            events.emit(
                logger,
                "review.pass.settled",
                "calibrator (%s) settled failed for %s in %dms",
                calibrator.backend,
                where,
                duration_ms,
                extra={
                    **calibrator_correlation,
                    "outcome": "failed",
                    "duration_ms": duration_ms,
                },
            )
            raise
        result = judged_run.result
        duration_ms = int((time.monotonic() - start) * 1000)
        calibrator_run.update(
            {
                "run_id": judged_run.run_id,
                "duration_ms": duration_ms,
                "outcome": "success",
                "judged": len(result.entries),
                "variant": variant_of(judged_run.task, label=label).as_record(),
                "usage": judged_run.usage.as_record(),
            }
        )
        if judged_run.reasoning is not None:
            # Stamped from the argv actually used (RVW03-WS04), never from
            # `calibrator.reasoning` config — a knob-less backend records unset.
            calibrator_run["reasoning"] = judged_run.reasoning
        calibrator_bundle.record(
            run_id=judged_run.run_id,
            outcome="success",
            duration_ms=duration_ms,
            judged=len(result.entries),
            variant=calibrator_run["variant"],
        )
        events.emit(
            logger,
            "review.pass.settled",
            "calibrator (%s) settled success for %s in %dms (%d judged)",
            calibrator.backend,
            where,
            duration_ms,
            len(result.entries),
            extra={
                **calibrator_correlation,
                "outcome": "success",
                "duration_ms": duration_ms,
            },
        )
        runs.append(calibrator_run)
        entries = result.entries
        feedback = result.overall_feedback.strip()

    routed = route_calibrated(entries, nit_cap=effective_nit_cap)
    findings = tuple(
        JudgedFinding(
            entry.finding, d, entry.duplicate_of, run_id=_pass_run_id(union, entry.id)
        )
        for entry, d in routed
    )
    posted_entries = [judged for judged in findings if judged.posted]
    comments = [_comment_dict(judged.finding) for judged in posted_entries]
    posted = len(comments)
    status = _derive_status(
        (judged.finding for judged in posted_entries), degraded=bool(failed)
    )
    attestation = _attestation(
        dims,
        failed,
        union_size=len(union),
        entries=findings,
        posted=posted,
        calibrated=calibrated,
        semantic=semantic_dedup,
    )
    review = {
        "summary": {
            "status": status,
            "overall_feedback": (
                f"{feedback}\n\n{attestation}" if feedback else attestation
            ),
            "coverage": coverage,
        },
        "comments": comments,
    }

    operation = (
        "calibration"
        if calibrated
        else "semantic near-duplicate dedup"
        if semantic_dedup
        else "mechanical dedup"
    )
    events.emit(
        logger,
        "review.calibrated" if calibrated else "review.deduped",
        "%s completed for %s (agent=%s): %d candidate(s) -> %d posted",
        operation,
        where,
        agent,
        len(union),
        posted,
        extra={
            **pr_extra,
            "reviewer": agent,
            "round_id": round_id,
            "candidates": len(union),
            "posted": posted,
        },
    )
    for judged in findings:
        if judged.posted:
            continue
        finding = judged.finding
        # `round_id` groups this round's disposition trail; `run_id` (when the
        # finding carries its originating pass's) traces a routed-out finding
        # back to the pass that raised it — the same `--run`/`--round` slices the
        # progress events answer to.
        disposition_extra = {
            **pr_extra,
            "reviewer": agent,
            "round_id": round_id,
            "severity": finding.severity.value,
            "disposition": judged.disposition.value,
        }
        if judged.run_id is not None:
            disposition_extra["run_id"] = judged.run_id
        events.emit(
            logger,
            "finding.dispositioned",
            "finding routed out on %s: %s (%s) -> %s",
            where,
            finding.file or "(no file)",
            finding.severity.value,
            judged.disposition.value,
            extra=disposition_extra,
        )

    return FanoutOutcome(
        review=review,
        findings=findings,
        runs=tuple(runs),
        total_tokens=_round_total(runs),
        round_id=round_id,
        artifacts_dir=artifacts_dir,
    )


def _round_total(runs: Sequence[Mapping[str, Any]]) -> int | None:
    """The round's measured token total: the sum of the contributing runs'
    REPORTED usage (RVW03-WS04). PURE.

    A run whose CLI reported no usage contributes nothing (its record says
    ``unreported`` explicitly); a round where NO run reported returns ``None``
    — the honest latency-only marker the eval report distinguishes — never a
    fabricated ``0``. A partially-reported round sums what was measured (a
    lower bound; each run's own record shows which contributed).
    """
    totals = []
    for run in runs:
        usage = run.get("usage")
        if not isinstance(usage, Mapping):
            continue
        total = usage.get("total_tokens")
        if isinstance(total, int) and not isinstance(total, bool):
            totals.append(total)
    return sum(totals) if totals else None


def _pass_run_id(union: Sequence[Mapping[str, Any]], entry_id: int) -> str | None:
    """The originating pass's run id for one judged entry — the RVW03-WS02
    finding↔pass correlation.

    ``entry_id`` is the entry's union index (the contract's join key; the
    calibrator boundary already rejected out-of-range ids, and the mechanical
    dedup only ever uses real indices); the union candidate carries the
    ``run_id`` :func:`_build_union` stamped from the pass that emitted it.
    Defensive ``None`` for an out-of-range id rather than a crash — the
    correlation is telemetry, never worth failing a round over.
    """
    if 0 <= entry_id < len(union):
        raw = union[entry_id].get("run_id")
        return str(raw) if raw else None
    return None


def route_calibrated(
    entries: Sequence[CalibratedFinding], *, nit_cap: int | None
) -> tuple[tuple[CalibratedFinding, Disposition], ...]:
    """The deterministic post routing, shared by both paths (the calibrator's
    judged entries and the mechanical :func:`dedup_union`). PURE.

    Two policies the CODE enforces rather than the judge (deterministic, so
    they are testable and prompt-drift-proof):

      * DUPLICATES NEVER POST — an entry merged into a canonical twin
        (``duplicate_of`` set) shares the twin's FINAL disposition (including a
        nit-cap flip applied to the twin — it IS the same underlying finding, and
        its substance reaches the PR through the twin) but is never emitted as a
        second posted comment; and
      * the ROUND-1 NIT CAP — among post-disposition canonical findings, nits
        beyond ``nit_cap`` flip to ``nit-suppressed`` (recorded, not posted;
        severity order keeps the first-``nit_cap`` strongest-ordered nits).
        ``None`` = uncapped; ``0`` = floor at minor (no nit posts).

    Returns EVERY judged finding (canonical + merged-away duplicates) with its
    FINAL disposition, ordered highest severity first — the exact set the round
    record persists (routed-out findings ride along, never erased). A finding
    POSTS iff its final disposition is ``post`` AND it is canonical
    (``duplicate_of is None``).
    """
    ordered = sorted(entries, key=lambda e: e.finding.severity.rank)
    nits_posted = 0
    routed: list[tuple[CalibratedFinding, Disposition]] = []
    final_disposition_for: dict[int, Disposition] = {}
    for entry in ordered:
        disposition = entry.disposition
        if entry.duplicate_of is not None:
            # A merged-away duplicate shares its canonical twin's FINAL
            # disposition — including a nit-cap flip applied to the twin below.
            # Canonical-before-duplicate ordering is guaranteed: parse_calibration
            # appends duplicates after all canonicals carrying the canonical's
            # severity, and the severity sort is stable, so the twin is seen first.
            disposition = final_disposition_for[entry.duplicate_of]
        elif (
            disposition is Disposition.POST
            and entry.finding.severity is Severity.NIT
            and nit_cap is not None
        ):
            if nits_posted >= nit_cap:
                disposition = Disposition.NIT_SUPPRESSED
            else:
                nits_posted += 1
        if entry.duplicate_of is None:
            final_disposition_for[entry.id] = disposition
        routed.append((entry, disposition))
    return tuple(routed)


def dedup_union(
    union: Sequence[Mapping[str, Any]],
    *,
    semantic: bool = False,
) -> tuple[CalibratedFinding, ...]:
    """Mechanically dedup the pass ``union`` into judged entries — the DEFAULT
    round-1 path (RVW02-WS08, calibrator off). PURE, no model.

    Candidates sharing a ``(file, line, claim)`` key — where ``claim`` is the
    finding text whitespace-collapsed and case-folded — are ONE underlying
    finding: the group's most-severe member (ties → lowest union id) becomes the
    canonical (disposition ``post``, its group-mates listed in ``merged``); each
    other member becomes a merged-away duplicate (``duplicate_of`` the canonical,
    carrying the canonical's severity like :func:`~shipit.review.calibrator.parse_calibration`
    materializes its inverse edge) so the round record retains every union
    finding. The canonical keeps its OWN pass-assigned severity — there is no
    judge to renormalize onto a common ruler, and the whole point of the off
    path is to trust the passes' severities. Nothing is ever DROPPED here:
    mechanical dedup only merges duplicates; a candidate's substance always
    reaches the record (and, unless a nit-cap flip in :func:`route_calibrated`
    suppresses it, the PR).

    ``semantic`` (#750, the opt-in Review Lab treatment — never the default
    until a cell earns it) additionally collapses NEAR-duplicates: two passes
    reporting the SAME defect at the SAME location in different words (the
    #673 ``eval.rs:1299`` case the exact-claim key cannot merge). The rule is
    the deterministic #673 seam (:func:`~shipit.review.match.same_claim` — no
    LLM, ADR-0048) applied CONSERVATIVELY: both candidates must name the same
    non-empty file (:func:`_near_duplicate` — a file-less candidate never
    semantically merges, and cross-file candidates are never equivalent) with
    zero line slack (:data:`SEMANTIC_DEDUP_LINE_SLACK` — every pass reviewed
    the same head, so there is no cross-head drift to absorb), and a candidate
    joins the FIRST group (creation order) where it near-matches EVERY member
    — no transitive chaining, so a bridging middle finding can never fuse two
    genuinely distinct defects at one line. Exact-key restatements still merge
    unconditionally, exactly as without ``semantic``. Grouping is a pure
    function of the union order, so the result is deterministic across runs.

    Every entry is disposition ``post`` — the nit cap and the duplicates-never-
    post rule are applied downstream by :func:`route_calibrated`, exactly as for
    the calibrator's entries. Canonicals are emitted first (in first-seen group
    order), then their duplicates, so the stable severity sort in
    :func:`route_calibrated` always sees a canonical before its duplicate.
    """
    grouped: list[list[Mapping[str, Any]]] = []
    group_by_key: dict[tuple[str, int | None, str], list[Mapping[str, Any]]] = {}
    for candidate in union:
        key = _dedup_key(candidate)
        group = group_by_key.get(key)
        if group is None and semantic:
            # The #750 near-duplicate collapse: join the FIRST group whose
            # EVERY member is the same claim (conservative — no chaining).
            group = next(
                (
                    members
                    for members in grouped
                    if all(_near_duplicate(candidate, member) for member in members)
                ),
                None,
            )
        if group is None:
            group = []
            grouped.append(group)
        group.append(candidate)
        # An exact restatement always follows its twin into the same group,
        # even when that group was formed by a semantic join.
        group_by_key.setdefault(key, group)

    entries: list[CalibratedFinding] = []
    for members in grouped:
        canonical = min(
            members,
            key=lambda c: (
                (parse_severity(c.get("severity")) or DEFAULT_SEVERITY).rank,
                _candidate_id(c),
            ),
        )
        canonical_finding = _finding_from_candidate(canonical)
        merged = tuple(_candidate_id(c) for c in members if c is not canonical)
        entries.append(
            CalibratedFinding(
                id=_candidate_id(canonical),
                finding=canonical_finding,
                disposition=Disposition.POST,
                merged=merged,
            )
        )
        for member in members:
            if member is canonical:
                continue
            entries.append(
                CalibratedFinding(
                    id=_candidate_id(member),
                    finding=_finding_from_candidate(
                        member, severity=canonical_finding.severity
                    ),
                    disposition=Disposition.POST,
                    duplicate_of=_candidate_id(canonical),
                )
            )
    return tuple(entries)


#: How far apart two same-round findings' lines may sit and still be
#: near-duplicate candidates in the SEMANTIC collapse (#750): ZERO, on purpose.
#: The #673 seam's default slack (:data:`shipit.review.match.NEAR_MISS_LINE_SLACK`)
#: absorbs drift between a pinned fixture head and what a reviewer reports —
#: but within ONE round every pass reviewed the SAME head, so there is no
#: drift to absorb, and two findings only collapse when they name the same
#: line (or are both file-scoped). Conservatism is the contract: an
#: over-merged pair of distinct defects is worse than a surviving duplicate.
SEMANTIC_DEDUP_LINE_SLACK = 0


def _near_duplicate(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Are two union candidates the SAME defect stated in different words —
    the #750 semantic-collapse predicate? PURE, deterministic, no LLM.

    The #673 same-claim seam (:func:`~shipit.review.match.same_claim`: same
    file, lines within slack, claim-token overlap ≥ the ADR-0048 threshold)
    applied at its most conservative: the normalized lines must be equal
    (same concrete line, or both file-scoped), zero line slack
    (:data:`SEMANTIC_DEDUP_LINE_SLACK`), and a candidate with NO file never
    matches — the seam would compare two file-less claims on text alone, and a
    location-free merge has no "same location" to be conservative about.
    Cross-file candidates are never equivalent (the seam's own non-negotiable
    coordinate).
    """
    file = str(a.get("file") or "")
    if not file:
        return False
    a_line = _candidate_line(a)
    b_line = _candidate_line(b)
    if a_line != b_line:
        return False
    return same_claim(
        Claim(file=file, line=a_line, text=str(a.get("text") or "")),
        Claim(
            file=str(b.get("file") or ""),
            line=b_line,
            text=str(b.get("text") or ""),
        ),
        line_slack=SEMANTIC_DEDUP_LINE_SLACK,
    )


def _candidate_line(candidate: Mapping[str, Any]) -> int | None:
    """A union candidate's line as the domain shape: a real ``int`` or ``None``
    (file-scoped) — the same bool-excluding coercion the dedup key and
    :func:`_finding_from_candidate` apply."""
    line = candidate.get("line")
    return line if isinstance(line, int) and not isinstance(line, bool) else None


def _dedup_key(candidate: Mapping[str, Any]) -> tuple[str, int | None, str]:
    """The mechanical dedup identity: file + line + normalized claim.

    The claim is the finding text with runs of whitespace collapsed and
    case-folded, so trivially-reworded-but-identical restatements of the same
    claim at the same location still merge; anything more — the semantic
    overlap of differently-worded findings — is either the opt-in #750
    near-duplicate collapse (:func:`_near_duplicate`, still deterministic) or
    the LLM judge's job, deliberately NOT attempted in this key — the exact
    key stays maximally conservative so the default never fuses two genuinely
    distinct findings.
    """
    claim = " ".join(str(candidate.get("text") or "").split()).casefold()
    return (str(candidate.get("file") or ""), _candidate_line(candidate), claim)


def _candidate_id(candidate: Mapping[str, Any]) -> int:
    """A union candidate's stable id (its index in the union — the join key
    :func:`_build_union` stamps)."""
    raw = candidate.get("id")
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else -1


def _finding_from_candidate(
    candidate: Mapping[str, Any], *, severity: Severity | None = None
) -> Finding:
    """Coerce one union candidate dict back into a domain :class:`Finding`.

    ``severity`` overrides the candidate's own (a merged-away duplicate carries
    its canonical twin's severity); ``None`` keeps the candidate's pass-assigned
    severity through the domain fail-safe (:func:`~shipit.finding.parse_severity`
    else ``major``). The candidate already passed the ONE trust boundary in
    :func:`_build_union`, so the fields are just re-typed here.
    """
    resolved = (
        severity
        if severity is not None
        else (parse_severity(candidate.get("severity")) or DEFAULT_SEVERITY)
    )
    confidence = candidate.get("confidence")
    return Finding(
        severity=resolved,
        text=str(candidate.get("text") or ""),
        file=str(candidate.get("file") or ""),
        line=_candidate_line(candidate),
        category=str(candidate.get("category") or ""),
        confidence=(
            float(confidence)
            if isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
            else None
        ),
        evidence=str(candidate.get("evidence") or ""),
        fix=str(candidate.get("fix") or ""),
    )


def _build_union(succeeded: Sequence[_PassResult]) -> list[dict[str, Any]]:
    """The calibrator's candidate list: every successful pass's comments,
    coerced through the ONE trust boundary (:func:`finding_from_dict` — the
    same coercion the posting path applies) and tagged with the dimension that
    found them AND the ``run_id`` of the pass that emitted them (RVW03-WS02 —
    what :func:`_pass_run_id` reads back onto the judged findings). Candidate
    ``id`` == list index (the contract's join key)."""
    union: list[dict[str, Any]] = []
    for result in succeeded:
        for raw in result.review.get("comments") or []:
            if not isinstance(raw, Mapping):
                continue
            finding = finding_from_dict(raw)
            union.append(
                {
                    "id": len(union),
                    "dimension": result.dimension.name,
                    "run_id": result.run.get("run_id"),
                    "file": finding.file,
                    "line": finding.line,
                    "severity": finding.severity.value,
                    "category": finding.category or result.dimension.name,
                    "confidence": finding.confidence,
                    "text": finding.text,
                    "evidence": finding.evidence,
                    "fix": finding.fix,
                }
            )
    return union


def _merge_coverage(succeeded: Sequence[_PassResult]) -> dict[str, list]:
    """Union the passes' coverage attestations into ONE summary attestation.

    ``reviewed`` entries dedupe preserving first-seen order; ``skipped``
    entries dedupe by ``(file, reason)``. Malformed pass coverage (the
    schema-unenforced agy path) is skipped defensively, exactly like the
    posting path's coverage renderer.
    """
    reviewed: list[str] = []
    skipped: list[dict[str, str]] = []
    seen_reviewed: set[str] = set()
    seen_skipped: set[tuple[str, str]] = set()
    for result in succeeded:
        summary = result.review.get("summary")
        coverage = summary.get("coverage") if isinstance(summary, Mapping) else None
        if not isinstance(coverage, Mapping):
            continue
        raw_reviewed = coverage.get("reviewed")
        for entry in raw_reviewed if isinstance(raw_reviewed, list) else []:
            text = str(entry)
            if text not in seen_reviewed:
                seen_reviewed.add(text)
                reviewed.append(text)
        raw_skipped = coverage.get("skipped")
        for entry in raw_skipped if isinstance(raw_skipped, list) else []:
            if not isinstance(entry, Mapping):
                continue
            file = str(entry.get("file", "?"))
            reason = str(entry.get("reason", ""))
            if (file, reason) not in seen_skipped:
                seen_skipped.add((file, reason))
                skipped.append({"file": file, "reason": reason})
    return {"reviewed": reviewed, "skipped": skipped}


def _attestation(
    dims: Sequence[Dimension],
    failed: Sequence[_PassResult],
    *,
    union_size: int,
    entries: Sequence[JudgedFinding],
    posted: int,
    calibrated: bool,
    semantic: bool = False,
) -> str:
    """The round's attestation paragraph for the posted summary: what ran
    (the default single full-scope pass, ADR-0052, or the opted-in dimension
    fan-out), what it found, and how the union routed to the posted set — so a
    human reading the PR sees the coverage claim (and any degradation) without
    opening the record.

    ``calibrated`` selects the routing phrasing: the DEFAULT off path posts the
    mechanically-deduped union (only nit-suppressed and duplicate route out — no
    drop/out-of-scope, which only the LLM judge produces) — with ``semantic``
    (#750, the near-duplicate collapse) the off-path line says so, so a treated
    round never reads as the stock mechanical arm; the on path posts
    "after calibration" with the full routed-out breakdown. Either way the
    routed-out counts plus ``posted`` plus the merged-away ``duplicate`` count
    sum to ``union_size``: every candidate is accounted for, so the arithmetic a
    human checks always balances. An EMPTY union had nothing to route (both the
    dedup and the dormant calibrator were skipped), so its line never claims a
    routing that never ran.
    """
    if len(dims) == 1 and dims[0] is _SINGLE_PASS_DIMENSION:
        # The round-1 DEFAULT (ADR-0052) ran one monolithic pass — attest it
        # honestly instead of claiming a fan-out of one. Keyed off the synthetic
        # dimension's OBJECT IDENTITY, not its name: the label rides through as
        # this module singleton, so a future registry dimension that happened to
        # be named "single" can never trigger the single-pass wording.
        prelude = "Review: one full-scope pass -> "
    else:
        names = ", ".join(d.name for d in dims)
        prelude = f"Review fan-out: {len(dims)} dimension pass(es) ({names}) -> "
    duplicates = sum(1 for judged in entries if judged.duplicate_of is not None)
    nit_suppressed = sum(
        1
        for judged in entries
        if judged.disposition is Disposition.NIT_SUPPRESSED
        and judged.duplicate_of is None
    )
    if union_size == 0:
        # Nothing was routed — a dedup/judge over nothing does nothing — so
        # attest the clean pass without a misleading routing that never ran.
        lines = [f"{prelude}no candidate findings."]
    elif not calibrated:
        union_word = "semantically-deduped union" if semantic else "deduped union"
        lines = [
            f"{prelude}{union_size} candidate finding(s) -> {posted} posted as the "
            f"{union_word} ({nit_suppressed} nit-suppressed, {duplicates} "
            f"duplicate); calibrator off."
        ]
    else:
        dropped = sum(
            1
            for judged in entries
            if judged.disposition is Disposition.DROP_UNVERIFIED
            and judged.duplicate_of is None
        )
        out_of_scope = sum(
            1
            for judged in entries
            if judged.disposition is Disposition.OUT_OF_SCOPE
            and judged.duplicate_of is None
        )
        lines = [
            f"{prelude}{union_size} candidate finding(s) -> {posted} posted after "
            f"calibration ({dropped} dropped-unverified, {out_of_scope} "
            f"out-of-scope, {nit_suppressed} nit-suppressed, {duplicates} duplicate)."
        ]
    if failed:
        failures = ", ".join(
            f"{r.dimension.name} ({r.run.get('outcome', 'failed')})" for r in failed
        )
        lines.append(
            f"DEGRADED COVERAGE: pass(es) failed and did not contribute: {failures}."
        )
    return "\n".join(lines)


def _comment_dict(finding: Finding) -> dict[str, Any]:
    """One posted finding back in REVIEW_SCHEMA comment shape — what the
    posting path and the round record both re-coerce through
    :func:`finding_from_dict`, so the routed result rides the EXISTING
    pipeline unchanged (the invisibility constraint)."""
    return {
        "file": finding.file,
        "line": finding.line,
        "text": finding.text,
        "severity": finding.severity.value,
        "category": finding.category,
        "confidence": finding.confidence,
        "evidence": finding.evidence,
        "fix": finding.fix,
    }


def _derive_status(posted: object, *, degraded: bool) -> str:
    """The posted review's status, derived from what posts (severity is the
    routing key, ADR-0044): any major-or-worse → ``REQUEST_CHANGES``; anything
    posted (or degraded coverage) → ``COMMENT``; a clean, fully-covered round →
    ``APPROVED``."""
    findings = list(posted)
    if any(f.severity.blocks_merge for f in findings):
        return "REQUEST_CHANGES"
    if findings or degraded:
        return "COMMENT"
    return "APPROVED"
