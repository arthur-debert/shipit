"""The endpoint-adapter registry — staged Artifacts → Distribution endpoints.

``shipit release publish`` (TOL02-WS05) is the TERMINAL release stage: it
walks the ``[artifacts]`` map and dispatches each declared Distribution
endpoint to its adapter here — a CLOSED registry mirroring the bundle
composition / lint Lang shape (adding an endpoint is adding an entry, never a
switch), one adapter per name of :data:`shipit.config.ENDPOINTS`:

- **gh-release** — create-or-edit the GitHub Release from the ONE coalesced
  notes text prepare wrote (PRD story 26), prerelease flag derived from the
  semver suffix and RE-ASSERTED on the resume/edit path (the legacy
  release#726 scar: ``gh release edit`` leaves the flag unchanged unless
  passed); uploads the staged bundle assets with ``--clobber``.
- **crates** — workspace crates in topological dependency order
  (:func:`crates_publish_order`); an already-uploaded crate version is
  SUCCESS, so a re-run after a mid-workspace failure resumes (PRD story 36).
- **pypi** — twine-style upload of the staged wheel+sdist with
  ``--skip-existing`` (idempotent), plus the ``--testpypi`` staging flag;
  token presence is validated before any upload. The upload is SCOPED to the
  artifact's own distribution (its ``pyproject`` ``[project].name``), so a
  multi-artifact bundle tree never leaks a sibling's wheel to the index.
- **npm** — publishes the prebuilt package tree (wasm-pack output style)
  without rebuilding (``--ignore-scripts``); publish-over-existing is
  SUCCESS.
- **brew** (the one *derived* endpoint) — renders the shared formula
  template (:mod:`shipit.release.brew`) against the FINAL release-asset
  URLs/sha256s and the crate's metadata, includes the private-repo download
  strategy when the source repo is private, ``ruby -c`` syntax-checks the
  output, and pushes it to the :data:`HOMEBREW_TAP` — where an UNCHANGED
  formula is a no-op push. Stable-channel only: prereleases never move the
  tap formula (the plan skips it, :func:`plan`).

The stage-wide invariants live HERE as pure cores, so they hold identically
for the WS06 ``wf-publish`` block and a laptop invocation (ADR-0040):

- :func:`check_gate` — the scar-#3 refusal (workflows.lex §3.3, PRD story
  32): publish takes the upstream stage results as EXPLICIT INPUTS and
  refuses unless build+bundle succeeded and sign succeeded-or-was-skipped.
- :func:`is_live_fire` — the central RC guard (PRD story 33): a
  ``-release-rc`` version publishes ONLY to the GH release (as prerelease);
  every external endpoint is skipped — one implementation, never one
  ``if:`` per YAML job.
- :func:`plan` — the two-stage ordering (PRD story 35): every ``release``
  endpoint dispatches before any ``derived`` one, because brew needs the
  final release-asset URLs/SHAs. The plan carries the skip verdicts (RC
  guard, brew's stable-only rule) as data, so a run is inspectable before
  anything external happens. Preflight (WS02) will consume this same core
  rather than re-deriving decisions.

Every adapter is idempotent-resumable (ADR-0009 phase 2): external endpoints
cannot roll back, so a re-run CONVERGES — already-published is success,
create becomes edit, an unchanged formula pushes nothing.

Effects run through the request's injected seams: ``run_cmd``/``probe`` are
the one Exec runner (ADR-0028 — the ``cargo publish`` / ``twine`` /
``npm publish`` / ``ruby -c`` argv literals below are those tools' one
publish-side assembly point, whitelisted in ``tests/test_tool_argv_sweep.py``),
``ghio`` the gh Tool adapter (:mod:`shipit.gh` — the gh-release REST/CLI
calls), ``gitio`` the git adapter (the tap clone/commit/push). Each adapter
declares its required secret names (``secrets`` — PRD story 43, the secrets
derivation registry's input); the verb validates presence of every planned
endpoint's tokens BEFORE the first dispatch, so a missing token fails loudly
at validation, never as a silent adapter skip.

The effectful shell is ``shipit release publish``
(:mod:`shipit.verbs.release`).
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import config, execrun
from ..changelog import SEMVER_RE
from . import ReleaseError
from . import brew as brew_mod
from . import integrity as integrity_mod
from .version import RELEASE_RC_PRE

#: The upstream stage results a publish invocation states (GH Actions job
#: result vocabulary): the click boundary admits exactly these.
STAGE_RESULTS: tuple[str, ...] = ("success", "failure", "cancelled", "skipped")

RESULT_SUCCESS = "success"
RESULT_SKIPPED = "skipped"

#: The one homebrew tap the brew endpoint pushes to (the portfolio's tap).
HOMEBREW_TAP = "arthur-debert/homebrew-tools"

#: The tap-clone identity the formula commit is authored as: the tap is a
#: fresh, hookless clone with no local identity, so the adapter states one.
TAP_COMMITTER = ("shipit release", "shipit-release@users.noreply.github.com")

#: testpypi's upload endpoint — the ``--testpypi`` staging lane's
#: ``--repository-url`` (the production default needs no URL).
TESTPYPI_URL = "https://test.pypi.org/legacy/"

#: Per-adapter token env keys. These are the names each adapter DECLARES to
#: the secrets derivation registry (PRD stories 43–45: gh-setup derives the
#: repo's needed secret set from what it actually ships) and validates at
#: publish time. gh-release declares NONE: ``gh`` rides its ambient auth
#: (Actions' ``GITHUB_TOKEN`` / a laptop's ``gh auth``), which is never a
#: synced secret. ``TESTPYPI_API_TOKEN`` is the ``--testpypi`` staging
#: lane's RUNTIME requirement, deliberately outside the static declaration —
#: staging is opt-in per run, not a provisioned endpoint of the repo.
CRATES_TOKEN_ENV = "CARGO_REGISTRY_TOKEN"
PYPI_TOKEN_ENV = "PYPI_API_TOKEN"
TESTPYPI_TOKEN_ENV = "TESTPYPI_API_TOKEN"
NPM_TOKEN_ENV = "NODE_AUTH_TOKEN"
TAP_TOKEN_ENV = "HOMEBREW_TAP_TOKEN"

#: cargo's already-published stderr signatures (lowercased match): the
#: idempotent-resume contract — an already-uploaded crate version is SUCCESS
#: (PRD story 36), so a re-run after a mid-workspace failure skips past it.
CRATE_ALREADY_PUBLISHED_MARKERS: tuple[str, ...] = (
    "already uploaded",
    "already exists",
)

#: npm's publish-over-existing stderr signatures (lowercased match): the same
#: already-published-is-success contract for the npm registry.
NPM_ALREADY_PUBLISHED_MARKERS: tuple[str, ...] = (
    "previously published",
    "cannot publish over",
)

#: The runner seams an adapter executes through — ``(argv, cwd, env) ->
#: ExecResult``. ``RunCmd`` has check=True semantics (a failing command
#: raises :class:`~shipit.execrun.ExecError`); ``Probe`` has check=False
#: semantics — a nonzero rc is a NORMAL answer the adapter classifies (the
#: already-published resume path). ``env``, when not ``None``, is MERGED over
#: the process environment (the Exec runner's contract) — the way a token
#: reaches ``twine``/``npm`` without riding argv. The verb injects the
#: production runners; tests inject recorders (the recorded-invocation
#: surface, PRD Testing Decisions).
RunCmd = Callable[[Sequence[str], Path, Mapping[str, str] | None], execrun.ExecResult]
Probe = Callable[[Sequence[str], Path, Mapping[str, str] | None], execrun.ExecResult]


@dataclass(frozen=True)
class PublishRequest:
    """Everything one endpoint dispatch needs: the artifact and its release
    context.

    ``assets_dir`` is the staged bundle tree (the bundle stage's ``--out``,
    plus the signer's outputs on a signed run); ``notes_path`` the ONE
    coalesced notes text prepare wrote (story 26); ``repo`` the source
    repo's ``owner/name`` slug (resolved by the verb only when a planned
    endpoint needs it — brew's asset URLs). ``env`` is the token lookup
    surface (validated by the verb before any dispatch); ``testpypi``
    reroutes the pypi adapter to the staging index.
    """

    artifact: config.Artifact
    entries: tuple[config.ToolchainEntry, ...]
    root: Path
    assets_dir: Path
    version: str
    tag: str
    prerelease: bool
    notes_path: Path
    env: Mapping[str, str]
    run_cmd: RunCmd
    probe: Probe
    ghio: Any
    gitio: Any
    repo: str | None = None
    testpypi: bool = False


@dataclass(frozen=True)
class Published:
    """One completed endpoint dispatch: what the adapter did, as short
    human-readable action lines (created/updated/uploaded/resumed …)."""

    artifact: str
    endpoint: str
    actions: tuple[str, ...]

    def to_dict(self) -> dict:
        """The ``--json`` field set — exactly the declared outputs."""
        return {
            "artifact": self.artifact,
            "endpoint": self.endpoint,
            "actions": list(self.actions),
        }


# --------------------------------------------------------------------------
# The pure cores: refusal gate, RC guard, ordering plan
# --------------------------------------------------------------------------


def check_gate(build: str, bundle: str, sign: str) -> None:
    """The scar-#3 refusal gate (workflows.lex §3.3, PRD story 32). Pure.

    Publish proceeds ONLY when build and bundle succeeded and sign either
    succeeded (signed path) or was skipped (unsigned path) — an explicit
    result check, never a plain dependency (a skipped sign must pass, a
    FAILED sign or bundle must block). Anything else raises
    :class:`ReleaseError` naming every blocking input, so the refusal is
    diagnosable in one read.
    """
    blockers = []
    if build != RESULT_SUCCESS:
        blockers.append(f"build={build}")
    if bundle != RESULT_SUCCESS:
        blockers.append(f"bundle={bundle}")
    if sign not in (RESULT_SUCCESS, RESULT_SKIPPED):
        blockers.append(f"sign={sign} (skipped-or-success required)")
    if blockers:
        raise ReleaseError(
            "publish refused — upstream stage results block the release: "
            + ", ".join(blockers)
            + " (build+bundle must be success, sign success-or-skipped; "
            "never ship a half-built set — workflows.lex §3.3)"
        )


def is_live_fire(version: str) -> bool:
    """Whether ``version`` is a ``-release-rc`` live-fire cut. Pure.

    The central RC guard's predicate (PRD story 33): the prerelease part is
    exactly ``release-rc`` or a dotted run of it (``-release-rc.2``) — the
    legacy per-job YAML expression (``endsWith(…, '-release-rc') ||
    contains(…, '-release-rc.')``), implemented ONCE. A live-fire cut
    publishes only to the GH release, as prerelease.
    """
    match = SEMVER_RE.match(version)
    pre = match.group("pre") if match else None
    return pre is not None and (
        pre == RELEASE_RC_PRE or pre.startswith(f"{RELEASE_RC_PRE}.")
    )


#: A skip verdict's reason strings — data, so the plan is renderable and
#: testable without dispatching anything.
SKIP_RC_GUARD = "rc-guard: -release-rc publishes to the GH release only"
SKIP_STABLE_ONLY = "stable-channel only: a prerelease never moves the tap formula"


@dataclass(frozen=True)
class Dispatch:
    """One planned (artifact, endpoint) pair: dispatch it, or skip it with a
    stated reason (the RC guard / brew's stable-only rule, decided pure)."""

    artifact: config.Artifact
    adapter: EndpointAdapter
    skip: str | None = None


def plan(
    artifacts: Sequence[config.Artifact],
    *,
    prerelease: bool,
    live_fire: bool,
) -> tuple[Dispatch, ...]:
    """The ordered dispatch plan over the declared endpoints. Pure.

    Two-stage ordering (PRD story 35): every ``release`` endpoint (in
    artifact declaration order) dispatches before any ``derived`` one —
    brew's formula renders against the FINAL release-asset URLs/SHAs, so
    gh-release's asset upload must complete first. Skips are decided here,
    centrally: a live-fire cut keeps ONLY gh-release (the RC guard, story
    33 — every external endpoint skipped); any prerelease skips brew (the
    tap is the stable channel). An endpoint name outside the closed registry
    is a hard :class:`ReleaseError` naming the known set.

    Cross-endpoint invariant: an unskipped brew dispatch REQUIRES an unskipped
    gh-release in the same plan — the formula points at
    ``releases/download/<tag>/…`` assets that only gh-release creates and
    uploads, so brew alone would push a tap formula referencing a release this
    run never produced. gh-release is itself idempotent-resumable, so a
    tap-repair run simply lists both.
    """
    dispatches: list[Dispatch] = []
    for stage in ("release", "derived"):
        for artifact in artifacts:
            for name in artifact.endpoints:
                adapter = adapter_for(name)
                if adapter is None:
                    known = ", ".join(names())
                    raise ReleaseError(
                        f"[artifacts.{artifact.name}] names unknown endpoint "
                        f"`{name}`; known endpoints: {known}"
                    )
                if adapter.stage != stage:
                    continue
                skip = None
                if live_fire and adapter.external:
                    skip = SKIP_RC_GUARD
                elif prerelease and adapter.name == "brew":
                    skip = SKIP_STABLE_ONLY
                dispatches.append(Dispatch(artifact, adapter, skip))
    live = [d.adapter.name for d in dispatches if d.skip is None]
    if "brew" in live and "gh-release" not in live:
        raise ReleaseError(
            "publish plan invalid — a brew endpoint renders a formula pointing "
            "at gh-release assets (`releases/download/<tag>/…`), but no unskipped "
            "gh-release endpoint is planned: declare `gh-release` so the release "
            "the formula targets is created and its assets uploaded (both "
            "endpoints are idempotent — a resume converges, nothing is duplicated)"
        )
    return tuple(dispatches)


def required_env_keys(adapter: EndpointAdapter, *, testpypi: bool) -> tuple[str, ...]:
    """The token env keys THIS run of ``adapter`` needs. Pure.

    The static ``secrets`` declaration feeds the derivation registry (story
    43); the runtime set differs only for pypi's opt-in staging lane, which
    swaps the production token for :data:`TESTPYPI_TOKEN_ENV`.
    """
    if adapter.name == "pypi" and testpypi:
        return (TESTPYPI_TOKEN_ENV,)
    return adapter.secrets


def missing_secrets(
    dispatches: Sequence[Dispatch],
    env: Mapping[str, str],
    *,
    testpypi: bool,
) -> tuple[tuple[str, str], ...]:
    """The ``(endpoint, env key)`` pairs whose token is absent from ``env``,
    across the plan's NON-SKIPPED dispatches, deduplicated in plan order.
    Pure — the verb turns a non-empty answer into one loud refusal BEFORE
    any dispatch (missing tokens fail at validation, never as a silent
    adapter skip)."""
    missing: list[tuple[str, str]] = []
    for dispatch in dispatches:
        if dispatch.skip is not None:
            continue
        for key in required_env_keys(dispatch.adapter, testpypi=testpypi):
            pair = (dispatch.adapter.name, key)
            if not env.get(key) and pair not in missing:
                missing.append(pair)
    return tuple(missing)


# --------------------------------------------------------------------------
# Shared adapter helpers
# --------------------------------------------------------------------------


def _leg_for(
    artifact: config.Artifact,
    entries: Sequence[config.ToolchainEntry],
    toolchain: str,
    endpoint: str,
) -> config.ToolchainEntry:
    """The first ``[toolchains]`` leg of ``toolchain``, or a loud refusal
    naming the endpoint that needed it (never a quiet skip)."""
    leg = next((entry for entry in entries if entry.toolchain == toolchain), None)
    if leg is None:
        raise ReleaseError(
            f"[artifacts.{artifact.name}] {endpoint} endpoint needs a "
            f"[toolchains] {toolchain} leg, and none is mapped"
        )
    return leg


def _leg_dir(root: Path, leg: config.ToolchainEntry) -> Path:
    """The leg's absolute directory (``"."`` → repo root)."""
    return root if leg.path in (".", "") else root / leg.path


def _asset_names(assets_dir: Path) -> tuple[str, ...]:
    """The staged asset file names — the regular, non-hidden files directly
    under ``assets_dir``, sorted (a missing tree is simply empty)."""
    if not assets_dir.is_dir():
        return ()
    return tuple(
        sorted(
            p.name
            for p in assets_dir.iterdir()
            if p.is_file() and not p.name.startswith(".")
        )
    )


def release_assets(assets_dir: Path) -> tuple[str, ...]:
    """The asset names gh-release ships. Pure over the listing.

    Everything :func:`_asset_names` finds EXCEPT the mac reseal payload
    (``*.unsigned-app.tar.gz``) — a cross-stage transport artifact for the
    signer (workflows.lex §3.1), never a distributable.
    """
    return tuple(
        name
        for name in _asset_names(assets_dir)
        if not name.endswith(".unsigned-app.tar.gz")
    )


def _require_token(req: PublishRequest, endpoint: str, key: str) -> str:
    """``req.env[key]``, or the loud missing-token refusal. The verb already
    validated the plan's tokens; this is the adapter-local belt for direct
    (test/library) callers."""
    token = req.env.get(key)
    if not token:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] {endpoint}: required token "
            f"{key} is not set — provision it (gh-setup derives the needed "
            f"set from the declared endpoints), never skip silently"
        )
    return token


def _tail(text: str, limit: int = 2000) -> str:
    """The last ``limit`` characters of ``text``, stripped — error context
    without megabytes of tool output."""
    return text.strip()[-limit:]


# --------------------------------------------------------------------------
# gh-release
# --------------------------------------------------------------------------


def _publish_gh_release(req: PublishRequest) -> Published:
    """Create-or-edit the GH Release from THE one notes text, then upload the
    staged assets. See the module docstring's gh-release entry.

    Idempotent-resumable: an existing release is EDITED (never duplicated),
    and the prerelease flag is re-asserted on BOTH paths — ``gh release
    edit`` leaves it unchanged unless passed (the release#726 scar), so a
    resume must state it again.
    """
    if not req.notes_path.is_file():
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] gh-release: no notes file at "
            f"{req.notes_path} — `shipit release prepare` writes the one "
            f"coalesced notes text (story 26); pass --notes to point at it"
        )
    cwd = str(req.root)
    kind = "prerelease" if req.prerelease else "release"
    actions = []
    if req.ghio.release_exists(req.tag, cwd=cwd):
        req.ghio.release_edit(
            req.tag,
            notes_file=str(req.notes_path),
            prerelease=req.prerelease,
            cwd=cwd,
        )
        actions.append(f"updated {kind} {req.tag} (prerelease flag re-asserted)")
    else:
        req.ghio.release_create(
            req.tag,
            notes_file=str(req.notes_path),
            prerelease=req.prerelease,
            cwd=cwd,
        )
        actions.append(f"created {kind} {req.tag}")
    assets = release_assets(req.assets_dir)
    if assets:
        req.ghio.release_upload(
            req.tag, [str(req.assets_dir / name) for name in assets], cwd=cwd
        )
        actions.append(f"uploaded {len(assets)} asset(s): {', '.join(assets)}")
    return Published(req.artifact.name, "gh-release", tuple(actions))


# --------------------------------------------------------------------------
# crates
# --------------------------------------------------------------------------


def crates_publish_order(metadata: dict) -> tuple[str, ...]:
    """Workspace crate names in topological dependency order. Pure.

    From parsed ``cargo metadata`` output: only workspace members are
    published, ordered so every member's in-workspace dependencies precede
    it (PRD story 36 — resumption mid-workspace needs a stable order).
    Dev-dependencies are excluded (they may legally cycle — a lib's test
    helper depending back on the lib — and do not gate publishing). Ties
    break alphabetically, so the order is deterministic. A genuine cycle
    among normal/build dependencies is a :class:`ReleaseError`.
    """
    id_to_name = {
        pkg.get("id"): pkg.get("name") for pkg in metadata.get("packages", [])
    }
    member_names = {
        id_to_name[member]
        for member in metadata.get("workspace_members", [])
        if member in id_to_name
    }
    deps: dict[str, set[str]] = {}
    for pkg in metadata.get("packages", []):
        name = pkg.get("name")
        if name not in member_names:
            continue
        deps[name] = {
            dep.get("name")
            for dep in pkg.get("dependencies", [])
            if dep.get("kind") != "dev"
            and dep.get("name") in member_names
            and dep.get("name") != name
        }
    order: list[str] = []
    remaining = dict(deps)
    while remaining:
        ready = sorted(
            name for name, needs in remaining.items() if not (needs & remaining.keys())
        )
        if not ready:
            raise ReleaseError(
                "crates: dependency cycle among workspace crates: "
                + ", ".join(sorted(remaining))
            )
        for name in ready:
            order.append(name)
            del remaining[name]
    return tuple(order)


def crate_already_published(stderr: str) -> bool:
    """Whether a failed ``cargo publish`` is the already-uploaded resume case
    (:data:`CRATE_ALREADY_PUBLISHED_MARKERS`). Pure."""
    lowered = stderr.lower()
    return any(marker in lowered for marker in CRATE_ALREADY_PUBLISHED_MARKERS)


def _publish_crates(req: PublishRequest) -> Published:
    """Publish the workspace crates in topological dependency order. See the
    module docstring's crates entry.

    ``cargo publish`` runs per crate through the PROBE seam: a nonzero exit
    whose stderr says already-uploaded is SUCCESS (the resume contract);
    anything else aborts with the stderr tail. cargo 1.66+ waits for the
    sparse index between dependent publishes natively, so there is no
    inter-publish sleep (the legacy composite's wait defaulted to 0).

    The registry token rides the ``cargo publish`` child env
    (:data:`CRATES_TOKEN_ENV`), never argv and never the ambient process
    environment — consistent with the pypi/npm adapters, so an injected
    ``env`` (recorded tests, workflow composition) authenticates the publish.
    ``cargo metadata`` needs no token.
    """
    leg = _leg_for(req.artifact, req.entries, "rust", "crates")
    leg_dir = _leg_dir(req.root, leg)
    token = _require_token(req, "crates", CRATES_TOKEN_ENV)
    metadata = req.run_cmd(
        ["cargo", "metadata", "--format-version", "1", "--no-deps"], leg_dir, None
    )
    order = crates_publish_order(json.loads(metadata.stdout))
    if not order:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] crates: `cargo metadata` names "
            f"no workspace members under {leg_dir}"
        )
    actions = []
    for crate in order:
        result = req.probe(
            ["cargo", "publish", "-p", crate], leg_dir, {CRATES_TOKEN_ENV: token}
        )
        if result.rc == 0:
            actions.append(f"{crate} {req.version} published")
        elif crate_already_published(result.stderr):
            actions.append(f"{crate} {req.version} already published — resumed")
        else:
            raise ReleaseError(
                f"[artifacts.{req.artifact.name}] crates: `cargo publish -p "
                f"{crate}` failed:\n{_tail(result.stderr)}"
            )
    return Published(req.artifact.name, "crates", tuple(actions))


# --------------------------------------------------------------------------
# pypi
# --------------------------------------------------------------------------


def _canonical_dist(name: str) -> str:
    """The PEP 503/427 canonical distribution key of a filename component:
    runs of ``-``/``_``/``.`` collapse to a single ``_`` and case folds. Pure.

    So one distribution's wheel form (PEP 427 escapes every run to ``_``) and
    its sdist form (PEP 625 does the same; a legacy sdist keeps the original
    hyphens/dots) compare EQUAL — ``my-awesome-pkg`` / ``my_awesome_pkg`` /
    ``My.Awesome.Pkg`` are one key.
    """
    return re.sub(r"[-_.]+", "_", name).lower()


def pypi_uploads(names: Sequence[str], dist: str) -> tuple[str, ...]:
    """The staged wheel(s) of distribution ``dist`` plus each wheel's MATCHING
    sdist, from the asset names. Pure.

    ``dist`` is the artifact's Python distribution name; the upload is scoped
    to THIS artifact's outputs, so a multi-artifact run never ships another
    artifact's wheel to PyPI under this artifact's token (registry publishes
    are irreversible). A wheel is ``<dist>-<version>-…\\ .whl`` and its sdist
    ``<dist>-<version>.tar.gz``; the dist part is matched CANONICALLY
    (:func:`_canonical_dist`), so the wheel's underscore form and a legacy
    hyphenated sdist both match, while the archive composition's
    ``<name>-<target>.tar.gz`` tarballs (NOT python distributions) stay out.
    """
    want = _canonical_dist(dist)
    sdists = sorted(n for n in names if n.endswith(".tar.gz"))
    files: list[str] = []
    for wheel in sorted(n for n in names if n.endswith(".whl")):
        parts = wheel.split("-")
        if len(parts) < 2 or _canonical_dist(parts[0]) != want:
            continue
        files.append(wheel)
        version = parts[1]
        for sdist in sdists:
            cand_dist, _sep, cand_version = sdist[: -len(".tar.gz")].rpartition("-")
            if (
                cand_version == version
                and _canonical_dist(cand_dist) == want
                and sdist not in files
            ):
                files.append(sdist)
                break
    return tuple(files)


def _pypi_dist_name(req: PublishRequest) -> str:
    """The artifact's Python distribution name — ``[project].name`` of the
    python leg's ``pyproject.toml`` — the key publish scopes the upload to.

    Resolved like every other adapter's leg (:func:`_leg_for`); a missing
    leg, unreadable ``pyproject``, or a ``pyproject`` with no
    ``[project].name`` is a LOUD :class:`ReleaseError` — publish never falls
    back to scanning the whole bundle tree.
    """
    leg = _leg_for(req.artifact, req.entries, "python", "pypi")
    pyproject = _leg_dir(req.root, leg) / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] pypi: cannot read {pyproject} "
            f"to scope the upload to this artifact's distribution: {exc}"
        ) from exc
    project = data.get("project") if isinstance(data, dict) else None
    name = project.get("name") if isinstance(project, dict) else None
    if not name:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] pypi: {pyproject} has no "
            f"[project].name — publish scopes the upload by distribution name, "
            f"never ships the whole bundle tree to the index"
        )
    return str(name)


def _publish_pypi(req: PublishRequest) -> Published:
    """Twine-style upload of the artifact's staged wheel+sdist. See the module
    docstring's pypi entry.

    The upload is SCOPED to the artifact's distribution (:func:`_pypi_dist_name`
    over the python leg's ``pyproject``), so a multi-artifact bundle tree never
    leaks another artifact's wheel to the index. ``--skip-existing`` is the
    idempotence contract (a re-run over already-uploaded files converges);
    ``--testpypi`` reroutes to :data:`TESTPYPI_URL` with the staging token. The
    token rides the child env (``TWINE_USERNAME``/``TWINE_PASSWORD``), never argv.
    """
    key = TESTPYPI_TOKEN_ENV if req.testpypi else PYPI_TOKEN_ENV
    token = _require_token(req, "pypi", key)
    dist = _pypi_dist_name(req)
    files = pypi_uploads(_asset_names(req.assets_dir), dist)
    if not files:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] pypi: no wheel for distribution "
            f"`{dist}` under {req.assets_dir} — the bundle stage's wheel "
            f"composition produces it; run `shipit release bundle` first"
        )
    argv = ["twine", "upload", "--non-interactive", "--skip-existing"]
    if req.testpypi:
        argv += ["--repository-url", TESTPYPI_URL]
    argv += [str(req.assets_dir / name) for name in files]
    req.run_cmd(
        argv, req.root, {"TWINE_USERNAME": "__token__", "TWINE_PASSWORD": token}
    )
    where = "testpypi" if req.testpypi else "pypi"
    return Published(
        req.artifact.name,
        "pypi",
        (f"uploaded to {where}: {', '.join(files)}",),
    )


# --------------------------------------------------------------------------
# npm
# --------------------------------------------------------------------------


def npm_already_published(stderr: str) -> bool:
    """Whether a failed ``npm publish`` is the publish-over-existing resume
    case (:data:`NPM_ALREADY_PUBLISHED_MARKERS`). Pure."""
    lowered = stderr.lower()
    return any(marker in lowered for marker in NPM_ALREADY_PUBLISHED_MARKERS)


def _publish_npm(req: PublishRequest) -> Published:
    """Publish the prebuilt npm package tree — no rebuild. See the module
    docstring's npm entry.

    ``--ignore-scripts`` is the no-rebuild contract: the tree (a wasm-pack
    ``pkg/``, a prepared package dir) was produced by the build/bundle
    stages, and a lifecycle script re-running a build here would be a second
    build path. The token rides the child env (the setup-node
    ``NODE_AUTH_TOKEN`` convention), never argv.
    """
    leg = _leg_for(req.artifact, req.entries, "npm", "npm")
    token = _require_token(req, "npm", NPM_TOKEN_ENV)
    pkg_dir = _leg_dir(req.root, leg)
    result = req.probe(
        ["npm", "publish", "--ignore-scripts"],
        pkg_dir,
        {NPM_TOKEN_ENV: token},
    )
    if result.rc == 0:
        action = f"published {req.version} from {leg.path or '.'}"
    elif npm_already_published(result.stderr):
        action = f"{req.version} already published — resumed"
    else:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] npm: `npm publish` failed:\n"
            f"{_tail(result.stderr)}"
        )
    return Published(req.artifact.name, "npm", (action,))


# --------------------------------------------------------------------------
# brew (derived)
# --------------------------------------------------------------------------


def brew_archives(artifact_name: str, names: Sequence[str]) -> dict[str, str]:
    """``{target triple: archive name}`` for the artifact's staged
    ``<name>-<triple>.tar.gz`` tarballs. Pure.

    Only mac/linux triples qualify (the platforms a formula installs);
    the name filter keeps a wheel's ``<dist>-<version>.tar.gz`` sdist and
    the mac reseal payload out.
    """
    prefix = f"{artifact_name}-"
    archives: dict[str, str] = {}
    for name in sorted(names):
        if not (name.startswith(prefix) and name.endswith(".tar.gz")):
            continue
        triple = name[len(prefix) : -len(".tar.gz")]
        if "apple-darwin" in triple or "linux" in triple:
            archives[triple] = name
    return archives


def _sha256(path: Path) -> str:
    """The file's sha256 hex digest — computed over the LOCAL staged tarball,
    which is byte-identical to the uploaded asset (gh-release uploaded this
    exact file in the release stage that just ran)."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _publish_brew(req: PublishRequest) -> Published:
    """Render, syntax-check, and push the tap formula. See the module
    docstring's brew entry.

    Derived-stage contract: runs only after gh-release uploaded the assets,
    so the rendered URLs point at live release assets and the sha256s are
    computed over the exact staged bytes. Idempotent: an UNCHANGED formula
    in the tap clone is a no-op (nothing committed, nothing pushed).
    """
    assert req.repo is not None  # the verb resolves the slug for a planned brew
    token = _require_token(req, "brew", TAP_TOKEN_ENV)
    archives = brew_archives(req.artifact.name, _asset_names(req.assets_dir))
    if not archives:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] brew: no "
            f"{req.artifact.name}-<triple>.tar.gz archives under "
            f"{req.assets_dir} — the archive composition produces the "
            f"release assets the formula points at"
        )
    targets = {
        triple: (
            f"https://github.com/{req.repo}/releases/download/{req.tag}/{name}",
            _sha256(req.assets_dir / name),
        )
        for triple, name in archives.items()
    }
    leg = _leg_for(req.artifact, req.entries, "rust", "brew")
    metadata = req.run_cmd(
        ["cargo", "metadata", "--format-version", "1", "--no-deps"],
        _leg_dir(req.root, leg),
        None,
    )
    desc, homepage, license_ = brew_mod.metadata_for(
        json.loads(metadata.stdout), req.artifact
    )
    binary = integrity_mod.expected_main_binary(req.artifact)
    text = brew_mod.render(
        binary=binary,
        version=req.version,
        desc=desc,
        homepage=homepage,
        license_=license_,
        targets=targets,
        private=bool(req.ghio.repo_is_private(req.repo)),
    )
    # The render lands in a scratch subdir of the assets tree (never a
    # top-level file, so a gh-release re-run can never ship it as an asset),
    # gets `ruby -c`'d there, then travels into the tap clone.
    formula_rel = f"Formula/{binary}.rb"
    scratch = req.assets_dir / "brew"
    scratch.mkdir(parents=True, exist_ok=True)
    rendered = scratch / f"{binary}.rb"
    rendered.write_text(text, encoding="utf-8", newline="\n")
    req.run_cmd(["ruby", "-c", str(rendered)], req.root, None)
    actions = [f"rendered {formula_rel} ({', '.join(sorted(targets))})"]
    with tempfile.TemporaryDirectory(prefix="shipit-brew-tap-") as tmp:
        tap_dir = Path(tmp) / "tap"
        # The token authenticates the clone AND the push; it is registered
        # with the central redactor by the verb's token validation, so the
        # URL is masked in every Exec record.
        req.gitio.clone(
            f"https://x-access-token:{token}@github.com/{HOMEBREW_TAP}.git",
            str(tap_dir),
        )
        dest = tap_dir / formula_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8", newline="\n")
        tap_cwd = str(tap_dir)
        if not req.gitio.status_porcelain(cwd=tap_cwd):
            # Idempotence prior art (legacy push-brew-tap): the formula the
            # tap already carries is byte-identical — push nothing.
            actions.append(f"{HOMEBREW_TAP} unchanged — nothing to push")
        else:
            branch = req.gitio.current_branch(cwd=tap_cwd)
            if branch is None:  # pragma: no cover — a fresh clone has a branch
                raise ReleaseError(f"brew: tap clone at {tap_dir} has no branch")
            name, email = TAP_COMMITTER
            req.gitio.configure_identity(name, email, cwd=tap_cwd)
            req.gitio.add([formula_rel], cwd=tap_cwd)
            req.gitio.commit(f"{binary} {req.version}", [formula_rel], cwd=tap_cwd)
            req.gitio.push(branch, cwd=tap_cwd)
            actions.append(f"pushed {formula_rel} to {HOMEBREW_TAP}")
    return Published(req.artifact.name, "brew", tuple(actions))


# --------------------------------------------------------------------------
# The closed registry
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EndpointAdapter:
    """One registry entry: an endpoint name, its ordering stage, the secret
    names it declares to the derivation registry, and its publish function.

    ``stage`` is ``"release"`` or ``"derived"`` (PRD story 35 ordering);
    ``external`` marks the endpoints the RC guard skips on a live-fire cut
    (everything but gh-release — story 33). ``secrets`` is the STATIC
    declaration gh-setup's derivation traverses (stories 43–45); the runtime
    validation set is :func:`required_env_keys`.
    """

    name: str
    stage: str
    publish: Callable[[PublishRequest], Published]
    secrets: tuple[str, ...] = ()
    external: bool = True


GH_RELEASE = EndpointAdapter(
    "gh-release", "release", _publish_gh_release, external=False
)
CRATES = EndpointAdapter(
    "crates", "release", _publish_crates, secrets=(CRATES_TOKEN_ENV,)
)
PYPI = EndpointAdapter("pypi", "release", _publish_pypi, secrets=(PYPI_TOKEN_ENV,))
NPM = EndpointAdapter("npm", "release", _publish_npm, secrets=(NPM_TOKEN_ENV,))
BREW = EndpointAdapter("brew", "derived", _publish_brew, secrets=(TAP_TOKEN_ENV,))

#: The CLOSED registry, in a stable order (the config boundary's
#: :data:`shipit.config.ENDPOINTS` names exactly this set — asserted in the
#: tests, so the two can never drift). Adding an endpoint is adding an entry
#: here plus the config name — never a switch. Marketplace-class adapters
#: (VS Marketplace, Open VSX, Zed, tree-sitter) are deliberately ABSENT
#: (PRD Out of Scope): their entries land when their repos migrate.
ADAPTERS: tuple[EndpointAdapter, ...] = (GH_RELEASE, CRATES, PYPI, NPM, BREW)


def names() -> tuple[str, ...]:
    """The registered endpoint names, in registry order."""
    return tuple(a.name for a in ADAPTERS)


def adapter_for(name: str) -> EndpointAdapter | None:
    """The registry entry named ``name``, or ``None`` when unregistered
    (:func:`plan` turns ``None`` into the hard error naming the known set)."""
    for adapter in ADAPTERS:
        if adapter.name == name:
            return adapter
    return None
