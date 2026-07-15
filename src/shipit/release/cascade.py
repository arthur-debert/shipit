"""Release-side artifact-pinned Cascade — the derived cross-repo fan-out.

The PRODUCER half of the artifact-pinned Cascade (ADR-0067, ARF01-WS06 #955).
On an upstream's **stable** release, the repos that pin that upstream in
``[artifact-deps]`` must be told to bump — but the target set is never a
producer-maintained list (which drifts the moment a consumer is added and the
producer's list is not). It is **DERIVED**: the consumer's own
``[artifact-deps]`` declaration is the single source of truth, so this module
computes the fan-out from the portfolio's declarations pointing at the
releasing upstream and fires one ``repository_dispatch`` at each.

Three seams, kept apart so the derivation stays a PURE, network-free core:

- **derivation** (:func:`derive_targets`) — pure over
  ``(upstream, [(consumer_slug, [ArtifactDep])])``: a consumer is a target iff
  it declares an artifact-dep whose ``repo`` matches the releasing ``upstream``
  (canonical, case-insensitive slug compare), the upstream never targeting
  itself. Deterministic first-seen order, the matched package names carried for
  the log. No filesystem, no network — exercised entirely on values.
- **portfolio scan** (:func:`scan_portfolio`) — reuses
  :func:`shipit.fleetsweep.load_portfolio` to enumerate EXACTLY the declared
  ``[project.portfolio]`` and reads each repo's local ``.shipit.toml`` under
  ``source_root`` for its ``[artifact-deps]``. BOUNDED by construction: one
  local file read per declared portfolio entry, never a fleet crawl or a
  per-release remote index build (ADR-0067's "keep it bounded" risk). A repo
  with no ``.shipit.toml`` / no ``[artifact-deps]`` contributes nothing.
- **dispatch** (:func:`dispatch_targets`) — fires one ``repository_dispatch``
  per derived target through the gh/Exec seam (``ghio.repository_dispatch``),
  carrying the epic's shared payload contract EXACTLY:
  ``{"upstream": "<owner>/<repo>", "version": "<semver>"}``. The receiver
  (ARF01-WS07 #956) bumps every ``[artifact-deps]`` entry whose ``repo``
  matches ``upstream`` to ``version``.

Stable-only (:func:`run_cascade`): rc / prerelease versions are published to
the channel for manual pin-testing (ADR-0064) but NEVER auto-dispatched — the
orchestrator short-circuits on a prerelease with a stated skip reason and fires
nothing, mirroring the ``notify-downstreams`` stable-only gate.

Two tokens the release job must carry (ADR-0067's cross-repo cost, documented):

- the **dispatch** PAT :data:`DISPATCH_TOKEN_ENV` (``DOWNSTREAM_DISPATCH_TOKEN``
  — the SAME cross-repo write PAT the ``notify-downstreams`` rail uses, since
  the workflow's ambient ``GITHUB_TOKEN`` cannot dispatch into another repo);
- a cross-repo **read** capability for the scan — here satisfied by the local
  portfolio checkout layout under ``source_root`` (the same fleetsweep source
  convention), so no read token is spent when the checkouts are present.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import config, fleetsweep, gh, identity
from ..config import ArtifactDep

#: The ``repository_dispatch`` event type the Cascade fires — the epic's shared
#: contract with the receiver (ARF01-WS07 #956). It reuses the ``notify-downstreams``
#: dispatch rail's event name (ADR-0067: "reuse the notify-downstreams dispatch
#: rail"); a drift test asserts it equals
#: :data:`shipit.release.publish.NOTIFY_EVENT_TYPE` so the two can never
#: silently diverge. The artifact-pinned payload (``{upstream, version}``) is
#: distinct from the source-pinned rebuild payload the notify rail carries — the
#: receiver keys on the payload shape, not the event name.
CASCADE_EVENT_TYPE = "upstream-release"

#: The cross-repo write PAT the dispatch needs — the SAME secret the
#: ``notify-downstreams`` endpoint declares (:data:`shipit.release.secretreq`
#: ``ENDPOINT_SECRETS["notify-downstreams"]``). The workflow's ambient
#: ``GITHUB_TOKEN`` is scoped to the releasing repo and cannot POST a dispatch
#: into another repo, so the Cascade reuses the notify rail's cross-repo PAT.
DISPATCH_TOKEN_ENV = "DOWNSTREAM_DISPATCH_TOKEN"

#: Reason recorded when a prerelease short-circuits the fan-out (ADR-0064/0067:
#: rc versions are pinnable manually but never auto-bumped).
SKIP_PRERELEASE = (
    "prerelease: rc/beta versions are published to the channel for manual "
    "pin-testing but never auto-dispatched (stable-only, ADR-0067)"
)


class CascadeError(RuntimeError):
    """A Cascade fan-out cannot be computed or dispatched (a malformed upstream
    slug, or a required dispatch token that is absent)."""


@dataclass(frozen=True)
class CascadeTarget:
    """One derived dispatch target: a consumer repo and the ``[artifact-deps]``
    package names that pin the releasing upstream.

    ``repo`` is the canonical ``owner/name`` slug the dispatch is fired at;
    ``packages`` (in declaration order) are the matched section keys — carried
    for the operator log, not the payload (the payload is repo-level, and the
    receiver re-derives which packages to bump from ``upstream``).
    """

    repo: str
    packages: tuple[str, ...]


@dataclass(frozen=True)
class CascadeReport:
    """The outcome of one Cascade run — a typed result the verb renders.

    ``dispatched`` is the subset of ``targets`` a real (non-dry-run) dispatch
    fired at, in target order; ``skipped`` states WHY nothing was dispatched (a
    prerelease, a dry run, or an empty target set) or is ``None`` when the fan-out
    fired. The payload contract is recorded verbatim so ``--json`` consumers see
    the exact ``{upstream, version}`` that was (or would be) sent.
    """

    upstream: str
    version: str
    prerelease: bool
    targets: tuple[CascadeTarget, ...]
    dispatched: tuple[str, ...]
    skipped: str | None

    def payload(self) -> dict[str, str]:
        """The shared dispatch payload contract for this run (ADR-0067)."""
        return {"upstream": self.upstream, "version": self.version}

    def to_dict(self) -> dict:
        return {
            "upstream": self.upstream,
            "version": self.version,
            "prerelease": self.prerelease,
            "event_type": CASCADE_EVENT_TYPE,
            "payload": self.payload(),
            "targets": [
                {"repo": t.repo, "packages": list(t.packages)} for t in self.targets
            ],
            "dispatched": list(self.dispatched),
            "skipped": self.skipped,
        }


def _canonical(slug: str) -> str:
    """The canonical (lowercased ``owner/name``) form of a slug, or raise.

    The ONE normalization the rest of the CLI uses (:func:`identity.repo_from_slug`),
    so a case-only difference between an ``[artifact-deps].repo`` and the releasing
    upstream still matches, and a malformed slug is refused loudly rather than
    silently failing to match.
    """
    try:
        return identity.repo_from_slug(slug).slug
    except ValueError as exc:
        raise CascadeError(f"invalid repo slug {slug!r}: {exc}") from exc


def derive_targets(
    upstream: str,
    consumers: Sequence[tuple[str, Sequence[ArtifactDep]]],
) -> tuple[CascadeTarget, ...]:
    """Derive the dispatch target set — the PURE core (no IO).

    A consumer becomes a :class:`CascadeTarget` iff it declares at least one
    ``[artifact-deps]`` entry whose ``repo`` matches ``upstream`` (canonical,
    case-insensitive). The upstream never targets ITSELF (a repo that pins its
    own artifact does not get a cross-repo bump PR from its own release).
    Targets keep portfolio (first-seen) order; each target's matched package
    names keep declaration order. A consumer with no matching pin yields no
    target — the derivation is the single source of truth, so an absent pin is
    an absent target, never a silent misfire.
    """
    up = _canonical(upstream)
    targets: list[CascadeTarget] = []
    for consumer_slug, deps in consumers:
        consumer = _canonical(consumer_slug)
        if consumer == up:
            continue
        matched = tuple(dep.package for dep in deps if _canonical(dep.repo) == up)
        if matched:
            targets.append(CascadeTarget(repo=consumer, packages=matched))
    return tuple(targets)


def scan_portfolio(
    cfg: dict, *, source_root: Path
) -> tuple[tuple[str, tuple[ArtifactDep, ...]], ...]:
    """Read the ``[artifact-deps]`` of every declared portfolio repo — BOUNDED.

    Reuses :func:`shipit.fleetsweep.load_portfolio` to enumerate EXACTLY the
    ``[project.portfolio]`` table (never a reconstructed repo list, ADR-0033),
    then reads each entry's local ``.shipit.toml`` at ``<source_root>/<path>``
    and parses its ``[artifact-deps]``. One local file read per declared entry —
    no fleet crawl, no per-release remote index build (ADR-0067's "keep it
    bounded" risk). A missing ``.shipit.toml`` or an empty/absent
    ``[artifact-deps]`` contributes an empty dep tuple, so the repo simply never
    becomes a target; a MALFORMED config still raises (a broken portfolio member
    is loud, never a silent skip). ``source_root`` is user-expanded (``~/h`` by
    default, the fleetsweep source layout).
    """
    root = Path(source_root).expanduser()
    scanned: list[tuple[str, tuple[ArtifactDep, ...]]] = []
    for entry in fleetsweep.load_portfolio(cfg):
        toml_path = root / entry.path / config.CONFIG_NAME
        if not toml_path.is_file():
            scanned.append((entry.repo, ()))
            continue
        repo_cfg = config.load(toml_path)
        scanned.append((entry.repo, config.load_artifact_deps(repo_cfg)))
    return tuple(scanned)


def dispatch_targets(
    targets: Sequence[CascadeTarget],
    payload: dict[str, str],
    *,
    token: str,
    ghio: object = gh,
) -> tuple[str, ...]:
    """Fire one ``repository_dispatch`` per target through the gh/Exec seam.

    Each target gets ONE :data:`CASCADE_EVENT_TYPE` dispatch carrying ``payload``
    verbatim, authenticated by the cross-repo ``token``. A failed dispatch
    raises through the adapter (never a silent partial fan-out). Returns the
    slugs dispatched to, in target order.
    """
    dispatched: list[str] = []
    for target in targets:
        ghio.repository_dispatch(  # type: ignore[attr-defined]
            target.repo,
            event_type=CASCADE_EVENT_TYPE,
            payload=payload,
            token=token,
        )
        dispatched.append(target.repo)
    return tuple(dispatched)


def run_cascade(
    upstream: str,
    version: str,
    *,
    cfg: dict,
    source_root: Path,
    prerelease: bool,
    token: str | None,
    ghio: object = gh,
    dry_run: bool = False,
    scan_fn: Callable[..., Sequence[tuple[str, Sequence[ArtifactDep]]]] | None = None,
) -> CascadeReport:
    """Scan → derive → dispatch: the release-side Cascade orchestrator.

    Stable-only (ADR-0067): a ``prerelease`` version short-circuits BEFORE any
    scan or dispatch with :data:`SKIP_PRERELEASE` and an empty fan-out — an
    rc/beta notifies no one. Otherwise it scans the portfolio (``scan_fn``
    injects the scan for tests; defaults to :func:`scan_portfolio`), derives the
    target set, and — unless ``dry_run`` — dispatches to each. ``token`` is
    required for a live dispatch (the cross-repo PAT, :data:`DISPATCH_TOKEN_ENV`)
    and refused loudly when absent, so a live fan-out never silently no-ops on a
    missing secret; a ``dry_run`` needs none. An empty target set records a
    "no consumer pins this upstream" skip and dispatches nothing.
    """
    up = _canonical(upstream)
    if prerelease:
        return CascadeReport(
            upstream=up,
            version=version,
            prerelease=True,
            targets=(),
            dispatched=(),
            skipped=SKIP_PRERELEASE,
        )
    scan = scan_fn or scan_portfolio
    consumers = scan(cfg, source_root=source_root)
    targets = derive_targets(up, consumers)
    payload = {"upstream": up, "version": version}
    if not targets:
        return CascadeReport(
            upstream=up,
            version=version,
            prerelease=False,
            targets=(),
            dispatched=(),
            skipped=f"no portfolio repo declares an [artifact-deps] on {up}",
        )
    if dry_run:
        return CascadeReport(
            upstream=up,
            version=version,
            prerelease=False,
            targets=targets,
            dispatched=(),
            skipped="dry run: derived the target set but dispatched nothing",
        )
    if not token:
        raise CascadeError(
            f"cascade dispatch to {len(targets)} target(s) needs the cross-repo "
            f"PAT {DISPATCH_TOKEN_ENV} — the workflow's ambient GITHUB_TOKEN "
            f"cannot dispatch cross-repo; provision it, never skip silently"
        )
    dispatched = dispatch_targets(targets, payload, token=token, ghio=ghio)
    return CascadeReport(
        upstream=up,
        version=version,
        prerelease=False,
        targets=targets,
        dispatched=dispatched,
        skipped=None,
    )
