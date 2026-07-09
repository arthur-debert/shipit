"""replay — review an arbitrary commit range offline: record written, no PR touched.

The RVW02-WS03 offline A/B harness: ``shipit pr review replay <base>..<head>``
resolves a commit RANGE of the current checkout (never a PR), runs a local review
backend over it through the shared range producer
(:func:`shipit.review.producer.run_range_review`), and writes the resulting
**Review-round record** (:mod:`shipit.review.roundrecord`, ``round.pr = None``)
to the local store — the review path's NO-POST mode. Nothing on GitHub is read
or written: no post, no check run, no review request. A historical PR's round 1
replays as ``merge-base..first-round-head`` — which is exactly what the
three-dot spelling ``base...head`` resolves (the merge base is computed here).

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
from dataclasses import dataclass
from pathlib import Path

from .. import execrun, git, identity
from ..agent.backend import Backend
from ..identity import Repo, Sha
from . import producer, roundrecord
from .diff import ReviewError

logger = logging.getLogger("shipit.review")


@dataclass(frozen=True)
class RangeView:
    """A resolved commit range of one checkout: the replay path's review target.

    The offline sibling of the PR path's :class:`~shipit.review.diff.ReviewView`
    — same diff/changed-files/workdir surface the producer needs, but there is
    no PR core at all (no number, no draft state): the target IS the range.
    ``repo`` is the checkout's origin identity — the round record's store key
    (ADR-0024), resolved offline.
    """

    repo: Repo
    base_sha: Sha
    head_sha: Sha
    diff: str
    changed_files: list[str]
    workdir: str


def parse_range(spec: str) -> tuple[str, str, bool]:
    """Split a range SPEC into ``(base, head, merge_base_wanted)``. PURE.

    ``A..B`` → ``(A, B, False)`` (review exactly ``A``→``B``); ``A...B`` →
    ``(A, B, True)`` (review from the merge base of ``A`` and ``B`` — the
    round-1 replay spelling). Raises :class:`~shipit.review.diff.ReviewError`
    on anything else — no separator, an empty endpoint — with the accepted
    grammar in the message, so a typo dies at parse, before any git work.
    """
    spec = spec.strip()
    if "..." in spec:
        base, _, head = spec.partition("...")
        merge_base_wanted = True
    else:
        base, _, head = spec.partition("..")
        merge_base_wanted = False
    base, head = base.strip(), head.strip()
    if not base or not head or ".." in base or ".." in head:
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
