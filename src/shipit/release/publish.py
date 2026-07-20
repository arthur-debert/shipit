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
- **crates** — workspace crates in topological dependency order, excluding
  ``publish = false`` members (:func:`crates_publish_order`); an
  already-uploaded crate version is SUCCESS, so a re-run after a
  mid-workspace failure resumes (PRD story 36).
- **pypi** — twine-style upload of the staged wheel+sdist with
  ``--skip-existing`` (idempotent), plus the ``--testpypi`` staging flag;
  token presence is validated before any upload. The upload is SCOPED to the
  artifact's own distribution (its ``pyproject`` ``[project].name``), so a
  multi-artifact bundle tree never leaks a sibling's wheel to the index.
- **npm** — publishes the staged npm tarball (the wasm-pack composition's
  ``<pkg>-<version>.tgz`` artifact, WS10 #798 — the SAME file the gh-release
  ships) without rebuilding (``--ignore-scripts``), scoped to THIS artifact's
  package name (the assert-bundle identity chain); publish-over-existing is
  SUCCESS.
- **vscode-marketplace** — ``npm exec -- vsce publish --packagePath`` of this
  artifact's staged per-target ``.vsix`` (the vsix composition's output),
  run from the ``npm`` leg dir (vsce is the extension's ``node_modules/.bin``
  devDependency and reads the leg's ``package.json``), the token riding the
  ``VSCE_PAT`` env vsce reads; publish-over-existing is SUCCESS (idempotent
  resume). An external endpoint the RC guard skips — a ``-release-rc`` cut
  never touches the marketplace (rc = gh-release only).
- **open-vsx** — ``npm exec -- ovsx publish`` of this artifact's same staged
  ``.vsix`` set to the Open VSX registry (``OVSX_PAT``), run from the ``npm``
  leg dir; publish-over-existing is SUCCESS.
  Also external / RC-guarded. Declarable now; a consumer wires it on only
  once its ``OVSX_PAT`` verifies (the lex-fmt/vscode repo's open-vsx leg is
  wired-but-off pending a working PAT — issue #789).
- **brew** (the one *derived* endpoint) — renders the shared formula
  template (:mod:`shipit.release.brew`) against the FINAL release-asset
  URLs/sha256s and the crate's metadata, includes the private-repo download
  strategy when the source repo is private, ``ruby -c`` syntax-checks the
  output, and pushes it to the :data:`HOMEBREW_TAP` — where an UNCHANGED
  formula is a no-op push. Stable-channel only: prereleases never move the
  tap formula (the plan skips it, :func:`plan`).
- **notify-downstreams** (a *derived*, stable-only endpoint, TOL02-WS16
  #792) — the generated-parser release's cross-repo cascade (legacy
  ``tree-sitter.yml`` notify hook): fires ONE ``repository_dispatch``
  (:data:`NOTIFY_EVENT_TYPE`) at each declared
  :attr:`shipit.config.Artifact.downstreams` repo, carrying the source
  repo/tag/version in its client payload, through a cross-repo PAT
  (``DOWNSTREAM_DISPATCH_TOKEN`` — the ambient token cannot dispatch
  cross-repo). Fires on REAL releases only: the plan skips it on any
  prerelease (and the RC guard on a live-fire cut), so an rc/beta notifies
  no one.
- **conda** (a *derived* endpoint, ARF01-WS01 #950, ADR-0064/0065; conda-direct
  ADR-0077) — the Artifact channel's producer. It packages the artifact's
  staged BUILD OUTPUT directly into a ``.conda`` (conda-direct, ADR-0077): the
  served subdirs, their target triples, and the staged archive names are
  DERIVED from the artifact's own declared ``platforms`` (the causal single
  source) — ``<artifact>-<triple>.tar.gz``/``.zip`` names are CONSTRUCTED from
  that declaration (:func:`conda_assets`), never reverse-engineered from a
  staged filename, and the build output is present from the bundle stage with
  NO gh-release dependency (gh-release only uploads the SAME staged tree). For
  each declared platform whose triple maps to a supported conda subdir
  (:data:`CONDA_SUBDIRS` — osx-arm64/linux-64/linux-aarch64/win-64 ONLY; no
  osx-64, no musl, matching today's ``provision`` refusal) whose archive is
  staged, it renders a minimal ``rattler-build`` recipe that repackages the
  prebuilt binary into a versioned ``.conda`` (no compilation — a single runner
  produces every subdir), then ``rattler-build publish``es the built packages
  to the producing repo's per-repo channel — ``s3://<bucket>/<owner/name>``
  over GCS's S3-interop endpoint (ADR-0065) — which uploads AND reindexes the
  remote channel's ``repodata.json`` in one step. Per-repo channel roots make
  each repo the sole writer of its own repodata, so cross-repo index races are
  structurally impossible (ADR-0064). rc-INCLUSIVE: unlike brew/notify it is
  NOT ``stable_only`` (prereleases publish for manual pin-testing, ADR-0064),
  but it IS external, so a ``-release-rc`` live-fire rehearsal stays
  gh-release-only. Idempotent-resumable via ``--force`` (a re-run re-uploads
  and re-indexes; already-published converges, ADR-0009 phase 2). A cross-repo
  DATA artifact (a ``platform_independent`` composition — tarball/zed or
  wasm-pack, one platform-independent archive, no triple) takes the NOARCH mode
  (ADR-0076,
  :func:`_publish_conda_noarch`): ONE ``noarch: generic`` ``.conda`` published
  to the channel's ``noarch/`` subdir, which every conda client reads alongside
  its platform subdir (no consumer change) — its archive name is likewise
  CONSTRUCTED from the artifact + version (:func:`conda_noarch_asset_name`),
  already direct. The per-platform triple→subdir path (:data:`CONDA_SUBDIRS`) is
  untouched — the two modes are additive.
- **zed** (a *derived*, stable-only endpoint, TOL03-WS02 #973, ADR-0068) — the
  Zed-extension registry endpoint. A Zed extension "publishes" only when a PR
  into the foreign, review-gated ``zed-industries/extensions`` monorepo (bump
  the extension's ``extensions.toml`` row + advance a git submodule to the
  newly-tagged source) is merged by Zed's maintainers — an API push we do NOT
  own. So the posture (ADR-0068) is **the tag is the release**: the ``zed``
  bundle composition tarballs the committed extension source (the local
  ``shared/`` grammar assets — no cross-repo fetch) and gh-release ships it,
  and this endpoint RENDERS the registry coordinates (the extension id read
  from ``extension.toml``, the new version, and the submodule rev = the release
  tag) into a scratch subdir + REPORTS them for a MANUALLY-gated registry PR —
  the render-vs-effect split brew uses, minus the push. It performs NO
  cross-repo write, so it declares NO secret (``ENDPOINT_SECRETS["zed"] ==
  ()``). External / RC-guarded (a ``-release-rc`` cut renders nothing —
  gh-release only) and stable-only (the registry serves stable versions, so a
  prerelease renders no entry). needs_repo: the submodule rev names the source
  ``owner/name`` @ tag. Unlike brew/notify it does NOT require an
  unskipped gh-release in the plan — it references the ``release prepare`` tag
  (ADR-0041), not gh-release assets, so a zed-only map is valid.

The stage-wide invariants live HERE as pure cores, so they hold identically
for the WS06 ``wf-publish`` block and a laptop invocation (ADR-0040):

- :func:`check_gate` — the scar-#3 refusal (workflows.lex §3.3, PRD story
  32): publish takes the upstream stage results as EXPLICIT INPUTS and
  refuses unless every LIVE stage succeeded. Liveness is a plan fact, never
  read off the result strings (issue #745): a live build/bundle must be
  ``success``; a proven non-live one (empty matrix — "the tag is the
  release" — or no bundle stage in the plan) may be ``success`` or
  ``skipped``; ``failure``/``cancelled`` always refuse. Sign keeps its own
  rule: success-or-skipped (the skip IS the sanctioned unsigned path). The
  liveness facts derive from the plan verbatim
  (:func:`build_is_live` / :func:`bundle_is_live`).
- :func:`is_live_fire` — the central RC guard (PRD story 33): a
  ``-release-rc`` version publishes ONLY to the GH release (as prerelease);
  every external endpoint is skipped — one implementation, never one
  ``if:`` per YAML job.
- :func:`plan` — the two-stage ordering (PRD story 35): every ``release``
  endpoint dispatches before any ``derived`` one, because brew needs the
  final release-asset URLs/SHAs. The plan carries the skip verdicts as data,
  so a run is inspectable before anything external happens, and it is the ONE
  place that decides what fires: the RC guard, the ``stable_only`` rule, and
  the per-invocation ``--endpoint`` selector (ADR-0070) are three inputs to
  one intersection rather than three scattered behaviors. Preflight (WS02)
  will consume this same core rather than re-deriving decisions.

Every adapter is idempotent-resumable (ADR-0009 phase 2): external endpoints
cannot roll back, so a re-run CONVERGES — already-published is success,
create becomes edit, an unchanged formula pushes nothing.

Effects run through the request's injected seams: ``run_cmd``/``probe`` are
the one Exec runner (ADR-0028 — the ``cargo publish`` / ``twine`` /
``npm publish`` / ``npm exec -- vsce publish`` / ``npm exec -- ovsx publish`` /
``ruby -c`` / ``rattler-build build`` / ``rattler-build publish`` argv literals
below are those tools' one publish-side assembly point, whitelisted in
``tests/test_tool_argv_sweep.py``),
``ghio`` the gh Tool adapter (:mod:`shipit.gh` — the gh-release REST/CLI
calls), ``gitio`` the git adapter (the tap clone/commit/push). Each adapter's
required secret NAME comes from the one derivation authority
(:data:`shipit.release.secretreq.ENDPOINT_SECRETS`, WS02 — gh-setup syncs and
preflight validates the same map, so the fleet never names a secret two ways);
the verb validates presence of every planned endpoint's tokens BEFORE the
first dispatch, so a missing token fails loudly at validation, never as a
silent adapter skip. An adapter looks its token up under that secret name and
feeds it to the tool under the var the TOOL reads (``NODE_AUTH_TOKEN`` for npm,
``TWINE_PASSWORD`` for twine — :data:`NPM_AUTH_ENV` / :data:`CARGO_TOKEN_ENV`).

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
from ..channel import buckets
from . import ReleaseError, secretreq
from . import brew as brew_mod
from . import bundle as bundle_mod
from . import integrity as integrity_mod
from .bundle import VSCE_TARGETS
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

#: The GitHub SECRET NAME each adapter looks its token up under, at publish
#: time and in :func:`required_env_keys` — sourced from the ONE derivation
#: authority (:data:`shipit.release.secretreq.ENDPOINT_SECRETS` /
#: :data:`~shipit.release.secretreq.TESTPYPI_SECRET`, WS02), so publish,
#: gh-setup's sync, and preflight's validation can never name a secret
#: differently (the "one secret map, three consumers that cannot drift"
#: invariant — architecture.lex §6, PRD stories 43–46). gh-release declares
#: NONE: ``gh`` rides its ambient auth (Actions' ``GITHUB_TOKEN`` / a laptop's
#: ``gh auth``), never a synced secret. ``TESTPYPI_SECRET`` is the
#: ``--testpypi`` staging lane's RUNTIME requirement, opt-in per run.
CRATES_SECRET = secretreq.ENDPOINT_SECRETS["crates"][0]
PYPI_SECRET = secretreq.ENDPOINT_SECRETS["pypi"][0]
TESTPYPI_SECRET = secretreq.TESTPYPI_SECRET
NPM_SECRET = secretreq.ENDPOINT_SECRETS["npm"][0]
VSCE_SECRET = secretreq.ENDPOINT_SECRETS["vscode-marketplace"][0]
OVSX_SECRET = secretreq.ENDPOINT_SECRETS["open-vsx"][0]
TAP_SECRET = secretreq.ENDPOINT_SECRETS["brew"][0]
NOTIFY_SECRET = secretreq.ENDPOINT_SECRETS["notify-downstreams"][0]
#: conda's write-credential pair (the GCS HMAC interop key, ADR-0065) — sourced
#: from the one derivation authority like every other endpoint's secret name.
CONDA_KEY_ID_SECRET = secretreq.ENDPOINT_SECRETS["conda"][0]
CONDA_SECRET_KEY_SECRET = secretreq.ENDPOINT_SECRETS["conda"][1]

#: The ``repository_dispatch`` event type the notify-downstreams cascade fires
#: (TOL02-WS16 #792). A downstream repo (lex-fmt/vscode, nvim, lexed) wires
#: ``on.repository_dispatch.types: [upstream-release]`` to rebuild against the
#: freshly-released grammar. ONE stable type across the fleet so a downstream
#: filters on a single name; the source repo/tag rides the client payload.
NOTIFY_EVENT_TYPE = "upstream-release"

#: The CLOSED release-triple → conda-subdir map (ARF01-WS01 #950, ADR-0064).
#: The Artifact channel serves EXACTLY these four subdirs: osx-arm64, linux-64,
#: linux-aarch64, win-64. A release triple with no entry (``x86_64-apple-
#: darwin`` → the missing osx-64, ``x86_64-unknown-linux-musl`` → the missing
#: musl subdir) is UNSERVED — the conda endpoint silently skips its archive, so
#: an Intel-mac/musl consumer's pin simply fails to resolve (no conda subdir, no
#: package) — the same fail-closed posture win-64 has under the pause (ADR-0071).
#: The keys are a subset of
#: :data:`shipit.release.preflight.PLATFORM_MATRIX` targets (the release lanes);
#: a lane the channel does not serve simply produces no package.
CONDA_SUBDIRS: dict[str, str] = {
    "aarch64-apple-darwin": "osx-arm64",
    "x86_64-unknown-linux-gnu": "linux-64",
    "aarch64-unknown-linux-gnu": "linux-aarch64",
    "x86_64-pc-windows-msvc": "win-64",
}

#: The two-tier Artifact channel buckets (ADR-0065 — two dedicated buckets, one
#: public-read/authless and one private/credentialed). The per-repo channel root
#: is ``<bucket>/<owner/name>``, so each repo is the sole writer of its own
#: repodata (cross-repo index races structurally impossible, ADR-0064). WS03
#: automates the buckets' provisioning. The tier a publish writes to is DERIVED
#: from the producing repo's visibility (ADR-0065), never declared:
#: :func:`_publish_conda` routes a private repo to
#: :data:`PRIVATE_ARTIFACT_BUCKET` and a public one to
#: :data:`PUBLIC_ARTIFACT_BUCKET`. WRITING always needs the HMAC pair (a
#: public-read bucket is still write-protected); the tier only changes which
#: bucket, so the write path is identical for both. These re-export the ONE
#: source of truth (:mod:`shipit.channel.buckets`) the consumer projection reads
#: from and the WS03 store provisioner CREATES — a drift test pins all three so
#: a publish can never write to a bucket the consumer never reads (or the
#: provisioner never made).
PUBLIC_ARTIFACT_BUCKET = buckets.PUBLIC_ARTIFACT_BUCKET
PRIVATE_ARTIFACT_BUCKET = buckets.PRIVATE_ARTIFACT_BUCKET

#: The single platform-independent subdir the conda endpoint's NOARCH MODE
#: (ADR-0076) publishes a cross-repo DATA artifact to — re-exports the ONE source
#: of truth (:data:`shipit.channel.buckets.NOARCH_SUBDIR`). A ``noarch: generic``
#: ``.conda`` lands under ``<channel>/noarch/`` and every conda client reads it
#: alongside the platform subdir it resolves, so no consumer change is needed.
NOARCH_SUBDIR = buckets.NOARCH_SUBDIR

#: The GCS S3-interop endpoint + region (ADR-0065 — ``region = "auto"`` and the
#: global ``storage.googleapis.com`` endpoint are load-bearing for GCS
#: interop). Passed to rattler-build's S3 backend via the env seam below. The
#: endpoint is the same shared host constant the consumer/provisioner use
#: (:data:`shipit.channel.buckets.CHANNEL_HOST`).
CONDA_S3_ENDPOINT = buckets.CHANNEL_HOST
CONDA_S3_REGION = "auto"

#: The child-process env vars rattler-build's S3 backend READS the channel
#: endpoint/region/credentials under (``rattler-build upload s3`` / ``publish``
#: — the **AWS SDK credential chain**: rattler-build resolves S3 config through
#: the standard ``AWS_*`` env vars, and the ``S3_*`` names its ``--help`` once
#: suggested are IGNORED — with them it dies "Could not determine region from
#: AWS SDK configuration"; confirmed on both 0.68.* and 0.69.*, #1049). The
#: endpoint and region are the fixed GCS-interop constants above; the
#: key/secret ride from the endpoint's secret pair
#: (:data:`CONDA_KEY_ID_SECRET` / :data:`CONDA_SECRET_KEY_SECRET`), looked up
#: in ``req.env`` and fed here — never argv, so the HMAC secret is never
#: recorded in an Exec argv line.
CONDA_S3_ENDPOINT_ENV = "AWS_ENDPOINT_URL"
CONDA_S3_REGION_ENV = "AWS_REGION"
CONDA_S3_KEY_ID_ENV = "AWS_ACCESS_KEY_ID"
CONDA_S3_SECRET_KEY_ENV = "AWS_SECRET_ACCESS_KEY"

#: The scratch subdir names under the staged assets tree the conda adapter
#: renders recipes / stages the built channel into — never top-level files (so
#: a gh-release re-run can never ship them as assets, the brew scratch prior
#: art). Each is namespaced by artifact (``<scratch>/<artifact>/<subdir>``)
#: because ``assets_dir`` is stage-wide: an un-namespaced channel tree would
#: let one conda artifact's post-build glob capture another's ``.conda``.
CONDA_RECIPE_SCRATCH = "conda-recipe"
CONDA_CHANNEL_SCRATCH = "conda-channel"

#: The child-process env var each tool READS the token under — the runtime
#: feed, tool-specific and distinct from the secret NAME the token is
#: provisioned/looked-up under above. cargo reads ``CARGO_REGISTRY_TOKEN``
#: (which also happens to be its secret name); npm reads ``NODE_AUTH_TOKEN``;
#: twine reads ``TWINE_USERNAME``/``TWINE_PASSWORD`` (inline in the adapter).
CARGO_TOKEN_ENV = "CARGO_REGISTRY_TOKEN"
NPM_AUTH_ENV = "NODE_AUTH_TOKEN"

#: The env var vsce/ovsx READ their personal access token under — the same
#: string as each endpoint's secret NAME (:data:`VSCE_SECRET` /
#: :data:`OVSX_SECRET`): both tools take ``VSCE_PAT`` / ``OVSX_PAT`` from the
#: environment (or ``-p``), so provision-name and tool-read-name coincide here
#: (unlike cargo/npm, whose read var differs from the secret name).
VSCE_PAT_ENV = "VSCE_PAT"
OVSX_PAT_ENV = "OVSX_PAT"

#: The vsce/ovsx target strings (:data:`shipit.release.bundle.VSCE_TARGETS`
#: values) — the closed suffix set the vsix composition names its per-target
#: outputs with (``<artifact>-<vsce-target>.vsix``). Publish scopes an
#: artifact's ``.vsix`` uploads by exactly this suffix set so a sibling
#: extension's ``.vsix`` in the coalesced assets tree is never shipped under
#: this artifact's endpoint/token (:func:`vsix_uploads`).
VSIX_TARGET_STRINGS: frozenset[str] = frozenset(VSCE_TARGETS.values())

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

#: vsce/ovsx's already-published stderr signatures (lowercased match): the
#: same already-published-is-success resume contract for the two VS Code
#: marketplaces — re-running the terminal stage over an already-shipped
#: version converges (ADR-0009 phase 2), never a spurious failure.
VSIX_ALREADY_PUBLISHED_MARKERS: tuple[str, ...] = (
    "already exists",
    "already published",
    "is already published",
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
    ``needs_repo`` endpoint needs it — brew's asset URLs, notify-downstreams'
    dispatch payload). ``env`` is the token lookup
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


def build_is_live(matrix: str) -> bool:
    """Whether the plan's build stage is live — the matrix carries at least
    one leg. Pure over the preflight plan's ``matrix`` JSON, verbatim.

    An empty matrix is the legitimate no-build shape ("the tag is the
    release", wf-build.yml): its build job is ``if``-skipped and the caller
    job concludes ``skipped`` (canary-confirmed, issue #745), which the gate
    must accept — but only against THIS plan fact, never inferred from the
    result string. Malformed JSON is a loud :class:`ReleaseError`.
    """
    try:
        entries = json.loads(matrix)
    except json.JSONDecodeError as exc:
        raise ReleaseError(
            f"--matrix is not valid JSON ({exc}) — pass the preflight plan's "
            f"`matrix` field verbatim (wf-prepare's output)"
        ) from exc
    if not isinstance(entries, list):
        raise ReleaseError(
            "--matrix must be the preflight plan's `matrix` JSON array, "
            f"got {type(entries).__name__}"
        )
    return bool(entries)


def bundle_is_live(stages: str) -> bool:
    """Whether the plan's bundle stage is live — ``"bundle"`` appears in the
    plan's live-stage list. Pure over the preflight plan's ``stages`` JSON,
    verbatim.

    The plan names ``bundle`` iff some matrix entry actually bundles
    (:func:`shipit.release.preflight.plan`), so a build-only plan (or an
    empty matrix) proves the bundle stage non-live. Malformed JSON is a loud
    :class:`ReleaseError`.
    """
    try:
        names_ = json.loads(stages)
    except json.JSONDecodeError as exc:
        raise ReleaseError(
            f"--stages is not valid JSON ({exc}) — pass the preflight plan's "
            f"`stages` field verbatim (wf-prepare's output)"
        ) from exc
    if not isinstance(names_, list):
        raise ReleaseError(
            "--stages must be the preflight plan's `stages` JSON array, "
            f"got {type(names_).__name__}"
        )
    return "bundle" in names_


def check_gate(
    build: str,
    bundle: str,
    sign: str,
    *,
    build_live: bool = True,
    bundle_live: bool = True,
) -> None:
    """The scar-#3 refusal gate (workflows.lex §3.3, PRD story 32). Pure.

    Publish proceeds ONLY when every LIVE stage succeeded — an explicit
    result check, never a plain dependency (a skipped sign must pass, a
    FAILED sign or bundle must block). Per stage:

    - a LIVE build/bundle must be ``success``;
    - a NON-live build/bundle (``build_live``/``bundle_live`` False — the
      plan proved the stage had nothing to run: empty matrix / no bundle
      stage, issue #745) may be ``success`` or ``skipped``;
    - ``failure``/``cancelled`` always refuse, live or not;
    - sign keeps its own rule regardless of liveness: success (signed path)
      or skipped (unsigned path).

    Liveness defaults to True — the strict contract — so a caller that
    states no plan fact never weakens the gate. Anything blocked raises
    :class:`ReleaseError` naming every blocking input, so the refusal is
    diagnosable in one read.
    """
    blockers = []
    for stage, result, live in (
        ("build", build, build_live),
        ("bundle", bundle, bundle_live),
    ):
        if live:
            if result != RESULT_SUCCESS:
                blockers.append(f"{stage}={result} (live {stage} requires success)")
        elif result not in (RESULT_SUCCESS, RESULT_SKIPPED):
            blockers.append(
                f"{stage}={result} (success-or-skipped required for a non-live {stage})"
            )
    if sign not in (RESULT_SUCCESS, RESULT_SKIPPED):
        blockers.append(f"sign={sign} (success-or-skipped required)")
    if blockers:
        raise ReleaseError(
            "publish refused — upstream stage results block the release: "
            + ", ".join(blockers)
            + " (a live build/bundle must be success, a plan-proven non-live "
            "one success-or-skipped, sign success-or-skipped; never ship a "
            "half-built set — workflows.lex §3.3)"
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
SKIP_NOTIFY_PRERELEASE = (
    "notify-downstreams fires on real releases only: a prerelease notifies no one"
)
SKIP_ZED_PRERELEASE = (
    "the zed extensions registry serves stable versions: a prerelease renders "
    "no registry entry"
)
SKIP_SELECTOR = "--endpoint selector: this run publishes only the selected endpoints"

#: The endpoint the selector can never deselect (ADR-0070): gh-release IS the
#: Release. ``--endpoint`` narrows DISTRIBUTION — the Release that lands still
#: carries every declared artifact's assets, so a subsetted publish is never a
#: partial release (ADR-0009).
RELEASE_ENDPOINT = "gh-release"


@dataclass(frozen=True)
class Dispatch:
    """One planned (artifact, endpoint) pair: dispatch it, or skip it with a
    stated reason (the RC guard / brew's stable-only rule, decided pure)."""

    artifact: config.Artifact
    adapter: EndpointAdapter
    skip: str | None = None


def _check_selector(
    selector: Sequence[str], artifacts: Sequence[config.Artifact]
) -> None:
    """Refuse an unusable ``--endpoint`` selector BEFORE planning. Pure.

    Three refusals, each of them a shape that would otherwise publish a subset
    the operator did not ask for (ADR-0070 — an unknown or misspelled endpoint
    is an error, never a silent no-op):

    - a name outside the CLOSED registry (a typo — ``--endpoint conda-forge``)
      names the known set, exactly as a rogue ``[artifacts]`` declaration does;
    - a registry-valid name NO artifact declares (``--endpoint pypi`` in a repo
      with no pypi endpoint) — the run would publish everything BUT what was
      asked for, the silent no-op in its most confusing form;
    - deselecting ``gh-release`` when it is declared: it is the Release, not a
      distribution channel (ADR-0009 partial-release prevention). A repo that
      declares no gh-release has none to deselect — the derived-endpoint
      invariants below still refuse a brew/notify that would strand (conda is
      conda-direct and needs no gh-release, ADR-0077).
    """
    declared = {name for artifact in artifacts for name in artifact.endpoints}
    unknown = [name for name in selector if adapter_for(name) is None]
    if unknown:
        raise ReleaseError(
            "publish refused — `--endpoint` names unknown endpoint(s) "
            + ", ".join(f"`{name}`" for name in unknown)
            + f"; known endpoints: {', '.join(names())}"
        )
    undeclared = [name for name in selector if name not in declared]
    if undeclared:
        raise ReleaseError(
            "publish refused — `--endpoint` selects "
            + ", ".join(f"`{name}`" for name in undeclared)
            + ", which no artifact in this repo declares: nothing would "
            "publish under "
            + ("that endpoint" if len(undeclared) == 1 else "those endpoints")
            + ". Declared here: "
            + (", ".join(sorted(declared)) if declared else "(none)")
        )
    if RELEASE_ENDPOINT in declared and RELEASE_ENDPOINT not in selector:
        raise ReleaseError(
            "publish refused — `--endpoint` cannot deselect `gh-release`: it "
            "is the Release itself, not a distribution channel. The selector "
            "narrows which registries publish; the Release that lands always "
            "carries every declared artifact's assets (ADR-0009 — a partial "
            f"release is structurally impossible). Add `--endpoint "
            f"{RELEASE_ENDPOINT}` to the selection."
        )


def plan(
    artifacts: Sequence[config.Artifact],
    *,
    prerelease: bool,
    live_fire: bool,
    selector: Sequence[str] | None = None,
) -> tuple[Dispatch, ...]:
    """The ordered dispatch plan over the declared endpoints. Pure.

    Two-stage ordering (PRD story 35): every ``release`` endpoint (in
    artifact declaration order) dispatches before any ``derived`` one —
    brew's formula renders against the FINAL release-asset URLs/SHAs, so
    gh-release's asset upload must complete first. Skips are decided here,
    centrally: a live-fire cut keeps ONLY gh-release (the RC guard, story
    33 — every external endpoint skipped); any prerelease skips the
    ``stable_only`` endpoints — brew (the tap is the stable channel) and
    notify-downstreams (a prerelease notifies no one, TOL02-WS16 #792). An
    endpoint name outside the closed registry is a hard
    :class:`ReleaseError` naming the known set.

    ``selector`` is the per-invocation ``--endpoint`` subset (ADR-0070), the
    THIRD input to that one intersection: when given, every endpoint outside
    it is skipped with :data:`SKIP_SELECTOR` — its own stated reason, shown in
    the plan alongside the RC-guard and stable-only skips, so the preview says
    exactly what will fire before anything external happens. ``None`` (the
    default, and what an absent flag parses to) leaves behavior unchanged: the
    full plan fires. The selector only ever ADDS skips, so it composes with the
    guards by INTERSECTION — a ``-release-rc`` cut still skips every external
    endpoint including a selected conda, and the selector can never resurrect
    a live-fire rehearsal into a real publish. :func:`_check_selector` refuses
    the unusable selections (unknown name, undeclared name, a deselected
    gh-release) before any of this.

    Cross-endpoint invariant: an unskipped brew OR notify-downstreams dispatch
    REQUIRES an unskipped gh-release in the same plan. brew's formula points at
    ``releases/download/<tag>/…`` assets that only gh-release creates and
    uploads, so brew alone would push a tap formula referencing a release this
    run never produced; notify-downstreams tells the downstream repos to rebuild
    against this release, so notifying without a landed gh-release points them at
    a release that never existed. conda is NOT bound by this invariant
    (conda-direct, ADR-0077): it packages the staged BUILD OUTPUT directly into
    a ``.conda`` — the bundle stage produces that archive locally and gh-release
    only uploads the same tree — so conda has no dependency on gh-release and a
    conda-only plan is valid. Each bound derived endpoint is checked against the
    UNSKIPPED set (a prerelease or live-fire cut that skips it never trips the
    invariant). gh-release is itself idempotent-resumable, so a repair run simply
    lists it alongside the derived endpoint.
    """
    if selector is not None:
        _check_selector(selector, artifacts)
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
                # The guards are stated FIRST: both reasons are true for a
                # non-selected external endpoint on an rc cut, and the plan
                # must never understate the guard that makes a rehearsal safe.
                if live_fire and adapter.external:
                    skip = SKIP_RC_GUARD
                elif prerelease and adapter.stable_only:
                    skip = adapter.stable_skip_reason
                elif selector is not None and name not in selector:
                    skip = SKIP_SELECTOR
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
    if "notify-downstreams" in live and "gh-release" not in live:
        raise ReleaseError(
            "publish plan invalid — notify-downstreams tells the downstream "
            "repos to rebuild against this release, but no unskipped gh-release "
            "endpoint is planned: declare `gh-release` so the release the "
            "downstreams target lands on GitHub before they are notified (both "
            "endpoints are idempotent — a resume converges, nothing is duplicated)"
        )
    # conda is deliberately NOT bound to gh-release (conda-direct, ADR-0077): it
    # packages the staged build output directly, so a conda-only plan is valid —
    # the release-before-derived ordering constraint that once required an
    # unskipped gh-release for conda is removed.
    return tuple(dispatches)


def required_env_keys(adapter: EndpointAdapter, *, testpypi: bool) -> tuple[str, ...]:
    """The token env keys THIS run of ``adapter`` needs. Pure.

    Each adapter's ``secrets`` mirrors :data:`secretreq.ENDPOINT_SECRETS`
    (the one derivation authority, WS02); the runtime set differs only for
    pypi's opt-in staging lane, which swaps the production token for
    :data:`TESTPYPI_SECRET`.
    """
    if adapter.name == "pypi" and testpypi:
        return (TESTPYPI_SECRET,)
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
    Members with ``publish = false`` (rendered as ``"publish": []`` in the
    metadata; test helpers, example crates) are excluded — ``cargo publish``
    refuses them, which would abort a real multi-crate publish mid-workspace
    (issue #849). A non-empty ``publish`` list only restricts the target
    registry and stays in the order. Dev-dependencies are excluded (they may
    legally cycle — a lib's test helper depending back on the lib — and do
    not gate publishing). Ties break alphabetically, so the order is
    deterministic. A genuine cycle among normal/build dependencies is a
    :class:`ReleaseError`.
    """
    id_to_name = {
        pkg.get("id"): pkg.get("name")
        for pkg in metadata.get("packages", [])
        if pkg.get("publish") != []
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

    The registry token is looked up under its secret name
    (:data:`CRATES_SECRET`) and rides the ``cargo publish`` child env
    (:data:`CARGO_TOKEN_ENV`, the var cargo reads — here the same string),
    never argv and never the ambient process environment — consistent with the
    pypi/npm adapters, so an injected ``env`` (recorded tests, workflow
    composition) authenticates the publish. ``cargo metadata`` needs no token.
    """
    leg = _leg_for(req.artifact, req.entries, "rust", "crates")
    leg_dir = _leg_dir(req.root, leg)
    token = _require_token(req, "crates", CRATES_SECRET)
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
            ["cargo", "publish", "-p", crate], leg_dir, {CARGO_TOKEN_ENV: token}
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
    key = TESTPYPI_SECRET if req.testpypi else PYPI_SECRET
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


def npm_tarball_name(pkg_name: str, version: str) -> str:
    """The ``npm pack`` filename for package ``pkg_name`` at ``version``. Pure.

    ``npm pack`` names its tarball ``<pkg>-<version>.tgz`` with the package
    name FLATTENED: a leading ``@`` is dropped and the ``/`` scope separator
    becomes ``-`` (``@lex-fmt/lex-wasm`` → ``lex-fmt-lex-wasm-1.2.3.tgz``).
    This is the deterministic name the wasm-pack composition
    (:mod:`shipit.release.bundle`) stages, so publish locates THIS artifact's
    tarball without scanning package.json out of every ``.tgz`` in the tree.
    """
    stem = pkg_name.lstrip("@").replace("/", "-")
    return f"{stem}-{version}.tgz"


def _publish_npm(req: PublishRequest) -> Published:
    """Publish the staged npm tarball — the wasm-pack composition's artifact,
    no rebuild. See the module docstring's npm entry.

    The tarball IS the artifact (WS10 #798): the wasm-pack bundle composition
    (:mod:`shipit.release.bundle`) `npm pack`s the wasm/npm package into
    ``<pkg>-<version>.tgz`` and stages it beside every other release asset, so
    the SAME file the gh-release ships is what npm publishes — never a second
    build path (``--ignore-scripts`` on the prebuilt tarball forecloses one).
    The upload is SCOPED to THIS artifact's tarball via the declared npm
    package name (the assert-bundle identity chain,
    :func:`shipit.release.integrity.expected_main_binary` — the artifact's
    ``main-binary``/``product-name``): ONE declaration names the package for
    both the assert tier and this scoping, so a multi-artifact tree never
    leaks a sibling's tarball to the registry (npm publishes are irreversible).
    The token is looked up under its secret name (:data:`NPM_SECRET`) and rides
    the child env under the var npm reads (the setup-node ``NODE_AUTH_TOKEN``
    convention, :data:`NPM_AUTH_ENV`), never argv.
    """
    token = _require_token(req, "npm", NPM_SECRET)
    pkg_name = integrity_mod.expected_main_binary(req.artifact)
    tarball = npm_tarball_name(pkg_name, req.version)
    path = req.assets_dir / tarball
    if not path.is_file():
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] npm: no tarball `{tarball}` for "
            f"package `{pkg_name}` under {req.assets_dir} — the wasm-pack bundle "
            f"composition produces it; run `shipit release bundle` first"
        )
    result = req.probe(
        ["npm", "publish", str(path), "--ignore-scripts"],
        req.root,
        {NPM_AUTH_ENV: token},
    )
    if result.rc == 0:
        action = f"published {pkg_name} {req.version} ({tarball})"
    elif npm_already_published(result.stderr):
        action = f"{pkg_name} {req.version} already published — resumed"
    else:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] npm: `npm publish` failed:\n"
            f"{_tail(result.stderr)}"
        )
    return Published(req.artifact.name, "npm", (action,))


# --------------------------------------------------------------------------
# vscode-marketplace / open-vsx — per-target .vsix publish (external, RC-guarded)
# --------------------------------------------------------------------------


def vsix_uploads(names: Sequence[str], artifact: str) -> tuple[str, ...]:
    """This ARTIFACT's staged per-target ``.vsix`` files, sorted. Pure over the
    asset listing.

    Scoped to ``artifact`` exactly as pypi scopes to its distribution
    (:func:`_pypi_dist_name`): the vsix composition names every output
    ``<artifact>-<vsce-target>.vsix``
    (:func:`shipit.release.bundle._compose_vsix`), so a name counts only when
    its ``<artifact>-`` prefix AND its ``<vsce-target>`` middle
    (:data:`VSIX_TARGET_STRINGS`) both match. A sibling extension's ``.vsix``
    sharing the coalesced ``assets_dir`` — or one whose name merely starts with
    this artifact's — is never shipped under this artifact's endpoint/token.
    """
    prefix = f"{artifact}-"
    return tuple(
        sorted(
            n
            for n in names
            if n.endswith(".vsix")
            and n.startswith(prefix)
            and n[len(prefix) : -len(".vsix")] in VSIX_TARGET_STRINGS
        )
    )


def vsix_already_published(stderr: str) -> bool:
    """Whether a failed ``vsce``/``ovsx`` publish is the already-published
    resume case (:data:`VSIX_ALREADY_PUBLISHED_MARKERS`). Pure."""
    lowered = stderr.lower()
    return any(marker in lowered for marker in VSIX_ALREADY_PUBLISHED_MARKERS)


def _publish_vsix_marketplace(
    req: PublishRequest,
    endpoint: str,
    argv_head: Sequence[str],
    secret: str,
    token_env: str,
) -> Published:
    """Publish this artifact's staged ``.vsix`` files to a VS Code marketplace
    via ``argv_head`` (``npm exec -- vsce publish --packagePath`` / ``npm exec
    -- ovsx publish``). Shared body of the two marketplace adapters — same
    per-artifact vsix set, same idempotent-resume rule, differing only in the
    tool head, the ``secret`` NAME the token is looked up under, and the
    ``token_env`` var the tool reads it from.

    Runs from the ``npm`` leg directory (:func:`_leg_for`), like ``_publish_npm``
    / ``_publish_pypi``: vsce/ovsx are the extension's ``node_modules/.bin``
    devDependencies, so ``npm exec`` resolves them there, and ``vsce publish``
    reads the leg's ``package.json`` from its cwd (from ``req.root`` it would
    fail ``Manifest not found``). The uploaded set is scoped to THIS artifact
    (:func:`vsix_uploads`), so a multi-artifact release never ships a sibling
    extension's ``.vsix`` under this endpoint/token.

    Each ``.vsix`` publishes through the PROBE seam: a nonzero exit whose stderr
    says already-published is SUCCESS (the resume contract, ADR-0009 phase 2);
    anything else aborts with the stderr tail. The token rides the child env
    under the var the tool reads (``token_env``), never argv. A run with no
    staged ``.vsix`` is a loud refusal — the vsix composition
    (:func:`shipit.release.bundle._compose_vsix`) produces the assets these
    endpoints ship, so their absence is a bundle gap, never a silent skip.
    """
    token = _require_token(req, endpoint, secret)
    leg = _leg_for(req.artifact, req.entries, "npm", endpoint)
    pkg_dir = _leg_dir(req.root, leg)
    vsixes = vsix_uploads(_asset_names(req.assets_dir), req.artifact.name)
    if not vsixes:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] {endpoint}: no .vsix under "
            f"{req.assets_dir} — the vsix composition produces the per-target "
            f"packages these endpoints publish; run `shipit release bundle` first"
        )
    actions = []
    for vsix in vsixes:
        result = req.probe(
            [*argv_head, str(req.assets_dir / vsix)], pkg_dir, {token_env: token}
        )
        if result.rc == 0:
            actions.append(f"published {vsix}")
        elif vsix_already_published(result.stderr):
            actions.append(f"{vsix} already published — resumed")
        else:
            raise ReleaseError(
                f"[artifacts.{req.artifact.name}] {endpoint}: publishing {vsix} "
                f"failed:\n{_tail(result.stderr)}"
            )
    return Published(req.artifact.name, endpoint, tuple(actions))


def _publish_vscode_marketplace(req: PublishRequest) -> Published:
    """``npm exec -- vsce publish --packagePath`` of this artifact's staged
    ``.vsix`` files. See the module docstring's vscode-marketplace entry.
    External / RC-guarded: a live-fire cut skips this endpoint (:func:`plan`),
    so rc = gh-release only.

    vsce runs through ``npm exec`` from the ``npm`` leg dir (the ``@vscode/vsce``
    devDependency, and the leg's ``package.json`` manifest ``vsce publish``
    reads). The token is looked up under its secret name (:data:`VSCE_SECRET`)
    and rides the child env under the var vsce reads (:data:`VSCE_PAT_ENV` — the
    same string), never argv. ``--packagePath`` publishes the prebuilt
    per-target package (no repackage here — the bundle stage built it).
    """
    return _publish_vsix_marketplace(
        req,
        "vscode-marketplace",
        ["npm", "exec", "--", "vsce", "publish", "--packagePath"],
        VSCE_SECRET,
        VSCE_PAT_ENV,
    )


def _publish_open_vsx(req: PublishRequest) -> Published:
    """``npm exec -- ovsx publish`` of this artifact's staged ``.vsix`` files to
    Open VSX. See the module docstring's open-vsx entry. External / RC-guarded
    like vscode-marketplace.

    ovsx runs through ``npm exec`` from the ``npm`` leg dir (the ``ovsx``
    devDependency). The token is looked up under its secret name
    (:data:`OVSX_SECRET`) and rides the child env under the var ovsx reads
    (:data:`OVSX_PAT_ENV`), never argv. ``ovsx publish <file>`` takes the
    prebuilt ``.vsix`` positionally.
    """
    return _publish_vsix_marketplace(
        req,
        "open-vsx",
        ["npm", "exec", "--", "ovsx", "publish"],
        OVSX_SECRET,
        OVSX_PAT_ENV,
    )


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
    if req.repo is None:
        # Belt for direct (test/library) callers — the verb resolves the source
        # slug for any live needs_repo dispatch (brew among them), so a real
        # release never reaches here without it. A loud ReleaseError (not a
        # strippable `assert`) matches the publish stage's error handling.
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] brew: no source repo resolved — "
            f"the formula's asset URLs point at "
            f"github.com/<owner/name>/releases/…, so an unresolved repo is a "
            f"hard error"
        )
    token = _require_token(req, "brew", TAP_SECRET)
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
# notify-downstreams
# --------------------------------------------------------------------------


def _publish_notify_downstreams(req: PublishRequest) -> Published:
    """Fire ``repository_dispatch`` at each declared downstream repo — the
    cascade a generated-parser release triggers (TOL02-WS16 #792, legacy
    ``tree-sitter.yml`` notify hook). See the module docstring's
    notify-downstreams entry.

    A derived, stable-only endpoint (the plan skips it on any prerelease and
    the RC guard skips it on a live-fire cut), so it is reached ONLY for a
    real release. Each downstream gets ONE ``upstream-release`` dispatch
    carrying the source repo/tag/version/artifact in its client payload; a
    failed dispatch raises loudly (never a silent partial notify). The
    cross-repo PAT (``DOWNSTREAM_DISPATCH_TOKEN``) is required — the ambient
    ``GITHUB_TOKEN`` cannot dispatch into another repo.
    """
    if req.repo is None:
        # Belt for direct (test/library) callers — the verb resolves the source
        # slug for any live needs_repo dispatch (notify-downstreams among them),
        # so a real release never reaches here without it. A loud ReleaseError
        # (not a strippable `assert`) matches the publish stage's error handling
        # and keeps a null-repo payload from ever reaching a downstream.
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] notify-downstreams: no source "
            f"repo resolved — the dispatch payload names the upstream "
            f"`owner/name` the downstreams rebuild against, so an unresolved "
            f"repo is a hard error, never a null payload"
        )
    token = _require_token(req, "notify-downstreams", NOTIFY_SECRET)
    if not req.artifact.downstreams:
        # Belt for direct (test/library) callers — the config boundary already
        # refuses a notify-downstreams endpoint with no downstreams list, so
        # the verb never reaches here empty.
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] notify-downstreams: no "
            f"`downstreams` declared — the endpoint fires repository_dispatch "
            f"at the artifact's downstream repos, and there are none"
        )
    payload = {
        "repo": req.repo,
        "tag": req.tag,
        "version": req.version,
        "artifact": req.artifact.name,
    }
    actions = []
    for slug in req.artifact.downstreams:
        req.ghio.repository_dispatch(
            slug, event_type=NOTIFY_EVENT_TYPE, payload=payload, token=token
        )
        actions.append(f"dispatched {NOTIFY_EVENT_TYPE} to {slug}")
    return Published(req.artifact.name, "notify-downstreams", tuple(actions))


# --------------------------------------------------------------------------
# conda (derived) — the Artifact channel producer
# --------------------------------------------------------------------------

#: The conda package-name vocabulary (ADR-0064): lowercase letters, digits,
#: ``.``, ``_``, ``-``. :func:`conda_package_name` rejects anything else loudly
#: — a scoped wasm-pack identity (``@scope/name``) or a spaced ``product-name``
#: would otherwise reach ``rattler-build`` as a doomed build with an opaque
#: error instead of an actionable config fix.
_CONDA_PACKAGE_NAME_RE = re.compile(r"[a-z0-9._-]+")

#: The one NOARCH-eligible composition whose single archive is an npm ``.tgz``
#: (``npm pack``'s ``<flattened-pkg>-<version>.tgz``, :func:`npm_tarball_name`)
#: rather than the ``<artifact>.tar.gz`` the other platform-independent
#: compositions (``tarball``/``zed``) stage. :func:`conda_noarch_asset_name`
#: branches the expected asset name on this, and :func:`conda_noarch_package_name`
#: flattens its scoped ``@scope/name`` identity into a conda-safe package name.
NOARCH_WASM_COMPOSITION = bundle_mod.WASM_PACK.name

#: Where the noarch build script installs a DATA artifact's files under
#: ``$PREFIX`` (ADR-0076: "a data artifact installs its files into the env").
#: A tool artifact puts a binary on PATH (``bin``/``Scripts``,
#: :func:`_conda_binary_layout`); a data artifact has no binary, so its whole
#: payload lands under ``$PREFIX/share/<package>/`` — the conventional conda home
#: for arch-independent shared data, namespaced by package so two noarch data
#: artifacts in one env never collide. A later ``#1059`` leg stages these files
#: into each editor consumer's bundle; this WS only publishes them there.
CONDA_NOARCH_INSTALL_DIR = "share"


def conda_subdir(triple: str) -> str | None:
    """The conda subdir for a release target ``triple``, or ``None`` when the
    Artifact channel does not serve it. Pure over :data:`CONDA_SUBDIRS`.

    ``None`` is the UNSERVED verdict (osx-64, musl): the endpoint skips that
    archive rather than inventing an unsupported subdir — the closed four-subdir
    matrix (ADR-0064), matching today's ``provision`` refusal for those hosts.
    """
    return CONDA_SUBDIRS.get(triple)


def conda_assets(
    artifact: config.Artifact, staged: Sequence[str]
) -> dict[str, tuple[str, str]]:
    """``{subdir: (triple, asset_name)}`` for the artifact's DECLARED platforms
    whose triple maps to a SERVED conda subdir AND whose staged build-output
    archive is present. Pure.

    conda-direct (ADR-0077): the subdir, triple and archive name are DERIVED
    from the artifact's own ``platforms`` declaration (the causal single
    source) — never reverse-engineered from a staged filename. For each declared
    platform (none declared → the default linux lane, exactly as
    :func:`shipit.release.preflight._matrix` expands it) the release-stage
    archive name is CONSTRUCTED from :data:`shipit.release.preflight.PLATFORM_MATRIX`
    (``<artifact>-<triple><ext_archive>`` — ``.tar.gz``, or ``.zip`` on windows —
    the same ``<name>-<target>`` shape :func:`shipit.release.bundle._compose_archive`
    stages), and the entry is included only when that archive is actually staged
    under the assets tree (``staged`` is the staged asset-name listing). This is
    the SAME platform→triple→subdir derivation :func:`conda_served_subdirs`
    projects, so the subdirs a repo publishes and the ones its readiness check
    probes agree by construction.

    An unserved platform (osx-64, musl → :func:`conda_subdir` ``None``) drops
    out — no invented subdir, matching today's ``provision`` refusal. A
    served-but-unbuilt platform (its constructed archive absent from ``staged``)
    drops out too, so a partial matrix publishes exactly what it built and never
    points ``rattler-build`` at a source archive that is not there. A triple maps
    to at most one subdir, so the result is keyed by subdir without collision.
    """
    from . import preflight  # lazy — avoid a publish<->preflight import cycle

    present = set(staged)
    assets: dict[str, tuple[str, str]] = {}
    for platform in artifact.platforms or (preflight.DEFAULT_PLATFORM,):
        spec = preflight.PLATFORM_MATRIX[platform]
        subdir = conda_subdir(spec.target)
        if subdir is None:
            continue
        name = f"{artifact.name}-{spec.target}{spec.ext_archive}"
        if name in present:
            assets[subdir] = (spec.target, name)
    return assets


def conda_served_subdirs(artifacts: Sequence[config.Artifact]) -> tuple[str, ...]:
    """The served conda subdirs a repo's conda producer ACTUALLY publishes (#1076).

    The union, over the repo's conda-endpoint build-bearing artifacts, of each
    declared release platform (none declared → the default linux lane, exactly as
    :func:`shipit.release.preflight._matrix` expands it) mapped through its target
    triple to a SERVED subdir — an unserved platform (osx-64, musl) drops out via
    :func:`conda_subdir` returning ``None``. Returned in
    :data:`shipit.channel.buckets.SERVED_SUBDIRS` order. Pure (a config
    projection, no I/O).

    This is the SAME platform→triple→subdir derivation the publish stage uses
    (:func:`conda_assets` / :func:`conda_subdir`), so a channel's readiness check
    (:func:`shipit.channel.store_provision.verify`) can probe exactly the subdirs
    its own publish writes — never the FIXED all-of-served set, which false-negs a
    repo that ships fewer platforms (e.g. lexd has no windows, so its channel can
    never satisfy a win-64 probe; #1076, the sibling of the #1072 lexd fix on the
    store-verify surface).
    """
    from . import preflight  # lazy — avoid a publish<->preflight import cycle

    found: set[str] = set()
    for artifact in artifacts:
        if "conda" not in artifact.endpoints or not artifact.build:
            continue
        for platform in artifact.platforms or (preflight.DEFAULT_PLATFORM,):
            subdir = conda_subdir(preflight.PLATFORM_MATRIX[platform].target)
            if subdir is not None:
                found.add(subdir)
    return tuple(s for s in buckets.SERVED_SUBDIRS if s in found)


def conda_package_name(artifact: config.Artifact) -> str:
    """The conda package name for ``artifact`` — the consumer's
    ``[artifact-deps.<key>]`` key (ADR-0064: the key doubles as the conda
    package name). Pure.

    The name IS the artifact's main-binary name
    (:func:`shipit.release.integrity.expected_main_binary`, the same identity
    brew renders its formula for), lowercased. WS01 packages single-binary tool
    artifacts (``lexd``); data/noarch artifacts (wasm, grammar) land later.

    The lowercased name is validated against the conda package-name vocabulary
    (lowercase letters/digits/``-``/``_``/``.``, :data:`_CONDA_PACKAGE_NAME_RE`)
    and a mismatch is a loud :class:`ReleaseError`: ``expected_main_binary`` can
    return a scoped wasm-pack identity (``@scope/name``) or a spaced
    ``product-name``, neither of which can name a conda package — a config fix
    the maintainer must make, not an opaque ``rattler-build`` failure downstream.
    """
    name = integrity_mod.expected_main_binary(artifact).lower()
    if not _CONDA_PACKAGE_NAME_RE.fullmatch(name):
        raise ReleaseError(
            f"[artifacts.{artifact.name}] conda: derived package name `{name}` "
            f"is not a valid conda package name (lowercase letters, digits, "
            f"`.`, `_`, `-` only) — set `main-binary` to a conda-safe name or "
            f"drop the conda endpoint. A scoped wasm-pack identity (`@scope/x`) "
            f"or a spaced `product-name` cannot name a conda package."
        )
    return name


def _conda_binary_layout(subdir: str, binary: str) -> tuple[str, str, str]:
    """``(source_filename, install_dir, install_filename)`` for the binary in a
    ``subdir`` package. Pure.

    The release archive holds the binary as ``<binary>`` on unix and
    ``<binary>.exe`` on windows (:data:`PLATFORM_MATRIX` ext_bin). conda puts
    executables on PATH from ``bin`` on unix and ``Scripts`` on windows, so the
    build script copies the extracted binary to the platform's PATH dir under
    ``$PREFIX``. The single-runner repackage runs the copy in the host shell
    regardless of the target subdir (no compilation — ADR-0064), so the layout
    is data, not a per-OS build script.
    """
    if subdir == "win-64":
        return f"{binary}.exe", "Scripts", f"{binary}.exe"
    return binary, "bin", binary


def render_conda_recipe(
    *,
    package: str,
    version: str,
    archive_path: str,
    source_binary: str,
    install_dir: str,
    install_binary: str,
) -> str:
    """The ``rattler-build`` recipe.yaml that repackages ONE prebuilt binary
    into a ``.conda``. Pure text (the render-vs-effect split brew uses).

    rattler-build extracts the local ``archive_path`` source, then the build
    script (run in the host shell — the single-runner repackage needs no
    cross-compilation, ADR-0064) copies the extracted ``source_binary`` into
    ``$PREFIX/<install_dir>/<install_binary>``. ``source_binary`` is the
    binary's path WITHIN the extracted tree — the release archive stages it
    under a top-level ``<artifact>-<triple>/`` dir (bundle._compose_archive's
    contract), but rattler-build STRIPS that single top-level dir on
    extraction, so the copy source is the bare binary name, never a
    ``<artifact>-<triple>/`` prefix (#1049). ``archive_path`` is a POSIX path
    (``Path.as_posix``) emitted
    through ``json.dumps`` — a JSON string IS a valid YAML 1.2 double-quoted
    scalar, so spaces, ``#``, and any embedded ``"`` / ``\\`` are escaped
    rather than breaking the recipe or silently re-pointing the source.
    ``build.number`` is 0 (the tag version is the sole ordering axis,
    ADR-0041 — a re-cut of the same version is a re-upload, not a new build
    number). ``build.dynamic_linking.binary_relocation`` is false: rattler-build
    relinks binaries by default (conda-build's built-from-source assumption —
    rewrite the build machine's library paths to conda's relocatable prefix),
    but this endpoint repackages a PREBUILT, already-SIGNED release binary that
    links only system libraries: there is nothing to relocate, the relink needs
    a per-OS toolchain the single cross-platform runner lacks (macOS
    ``install_name_tool`` on a Linux runner, #1052), and rewriting the Mach-O
    would invalidate the sign stage's signature. Validated live on a
    ``file://`` channel: build → pixi resolve →
    run → version bump → transparent re-resolve (the ADR-0064 spike loop).
    """
    return (
        f"package:\n"
        f"  name: {package}\n"
        f'  version: "{version}"\n'
        f"\n"
        f"source:\n"
        f"  - path: {json.dumps(archive_path)}\n"
        f"\n"
        f"build:\n"
        f"  number: 0\n"
        f"  dynamic_linking:\n"
        f"    # prebuilt+signed binary: no relink (needs a per-OS toolchain the\n"
        f"    # single runner lacks, and rewriting would break the signature)\n"
        f"    binary_relocation: false\n"
        f"  script:\n"
        f'    - mkdir -p "${{PREFIX}}/{install_dir}"\n'
        f'    - cp "{source_binary}" "${{PREFIX}}/{install_dir}/{install_binary}"\n'
    )


def conda_noarch_eligible(artifact: config.Artifact) -> bool:
    """Whether ``artifact``'s conda endpoint runs in NOARCH mode (ADR-0076). Pure.

    True iff the artifact's bundle composition is ``platform_independent`` — the
    registry flag (:attr:`shipit.release.bundle.Composition.platform_independent`)
    marking a composition that produces a SINGLE platform-independent archive with
    no ``-<target>`` qualifier: ``tarball``/``zed`` (the tree-sitter grammar /
    Zed-extension ``<artifact>.tar.gz`` shape) AND ``wasm-pack`` (the npm
    ``<flattened-pkg>-<version>.tgz``). "One arch-independent archive to
    repackage" IS the ADR-0076 eligibility criterion — read off the composition's
    own flag, NOT the composition NAME (so a new platform-independent composition
    is eligible without editing this) and NOT an empty ``platforms`` list
    (``platforms = ()`` is the default linux lane, not platform-independence).
    A per-platform TOOL artifact (a platform-qualified composition, or none) stays
    on the existing triple→subdir path — the two modes are additive.
    """
    bundle = artifact.bundle
    if bundle is None:
        return False
    composition = bundle_mod.composition(bundle.composition)
    return composition is not None and composition.platform_independent


def conda_noarch_asset_name(artifact: config.Artifact, version: str) -> str:
    """The KNOWN staged name of the single platform-independent archive a NOARCH
    artifact repackages — the release-stage convention, never a scrape (ADR-0064).
    Pure over the artifact's composition.

    The archive name depends on the composition (each stages exactly ONE
    unqualified archive, no ``-<triple>`` suffix):

    - ``wasm-pack`` (:data:`NOARCH_WASM_COMPOSITION`) stages the npm
      ``<flattened-pkg>-<version>.tgz`` that ``npm pack`` names
      (:func:`npm_tarball_name`, off the ``@scope/name`` identity) — a ``.tgz``,
      not a ``.tar.gz``.
    - ``tarball``/``zed`` stage the bare ``<artifact>.tar.gz``
      (:func:`shipit.release.bundle._compose_tarball`, the tree-sitter grammar /
      Zed-extension shape).
    """
    bundle = artifact.bundle
    if bundle is not None and bundle.composition == NOARCH_WASM_COMPOSITION:
        return npm_tarball_name(integrity_mod.expected_main_binary(artifact), version)
    return f"{artifact.name}.tar.gz"


def conda_noarch_asset(
    artifact: config.Artifact, version: str, names: Sequence[str]
) -> str | None:
    """The single platform-independent archive a NOARCH artifact repackages, or
    ``None`` when the staged tree carries none. Pure.

    The eligible composition stages exactly ONE unqualified archive
    (:func:`conda_noarch_asset_name` — the tarball/zed ``<artifact>.tar.gz`` or
    the wasm-pack npm ``.tgz``), matched by its KNOWN name. A staged tree without
    it (the composition never ran) is ``None``, which the adapter turns into a
    loud refusal rather than a silent empty publish.
    """
    want = conda_noarch_asset_name(artifact, version)
    return want if want in names else None


def conda_noarch_package_name(artifact: config.Artifact) -> str:
    """The conda package name for a NOARCH artifact — the artifact's derived
    identity FLATTENED into the conda vocabulary. Pure.

    A ``wasm-pack`` data artifact derives a SCOPED npm identity
    (``@scope/name`` — :func:`shipit.release.integrity.expected_main_binary`),
    which the strict :func:`conda_package_name` (the tool path) rightly rejects:
    a conda package name cannot carry an ``@`` or ``/``. The noarch path instead
    FLATTENS it exactly as ``npm pack`` flattens its tarball stem
    (:func:`npm_tarball_name`: drop the leading ``@``, ``/`` scope separator →
    ``-``), so ``@lex-fmt/lex-wasm`` → ``lex-fmt-lex-wasm``. An unscoped
    tarball/zed identity has no ``@``/``/`` and is unchanged (bar the lowercase).
    The flattened name is validated against the conda vocabulary
    (:data:`_CONDA_PACKAGE_NAME_RE`) so a residually-invalid identity (a spaced
    ``product-name``) is still one loud :class:`ReleaseError`, not an opaque
    ``rattler-build`` failure.
    """
    raw = integrity_mod.expected_main_binary(artifact)
    name = raw.lstrip("@").replace("/", "-").lower()
    if not _CONDA_PACKAGE_NAME_RE.fullmatch(name):
        raise ReleaseError(
            f"[artifacts.{artifact.name}] conda (noarch): derived package name "
            f"`{name}` is not a valid conda package name (lowercase letters, "
            f"digits, `.`, `_`, `-` only) — set `main-binary` to a conda-safe "
            f"name or drop the conda endpoint. A spaced `product-name` cannot "
            f"name a conda package."
        )
    return name


def render_conda_noarch_recipe(
    *,
    package: str,
    version: str,
    archive_path: str,
    install_dir: str,
) -> str:
    """The ``rattler-build`` recipe.yaml that repackages ONE platform-independent
    DATA archive into a ``noarch: generic`` ``.conda`` (ADR-0076). Pure text
    (the render-vs-effect split brew/the per-platform recipe use).

    rattler-build extracts the local ``archive_path`` source into a
    ``payload/`` subdir (``source.target_directory``), then the build script
    (run once on the host — a noarch package is built one time and read on every
    platform) copies that payload's CONTENTS into
    ``$PREFIX/<install_dir>/<package>/``. Unlike the per-platform tool recipe
    (:func:`render_conda_recipe`), a data artifact has no binary to place on
    PATH: it installs its FILES into the env (ADR-0064), so the whole tree is
    copied verbatim under a package-namespaced dir.

    The extraction target MATTERS: rattler-build writes its own build scaffolding
    (``conda_build.sh``, ``build_env.sh``, ``.source_info.json``, …) into the
    work ROOT, so a ``cp -R .`` from there would sweep that scaffolding INTO the
    package. Extracting under ``payload/`` and copying ``payload/.`` keeps the
    package to the archive's own bytes. rattler-build strips a SINGLE top-level
    wrapper dir on extraction but leaves a multi-entry archive intact, so the copy
    is correct for both eligible shapes: the ``tarball``/``zed`` archive has
    MULTIPLE top-level entries (``src/``, ``queries/`` — a ``tar -C <leg>
    <entries>``, not a single-dir wrap) and lands intact under ``payload/``; the
    ``wasm-pack`` npm ``.tgz`` wraps its files in a single ``package/`` dir, which
    rattler-build strips, so the wasm files ALSO land directly under ``payload/``
    (never nested under ``package/``) — the same ``cp -R payload/.`` installs the
    artifact's own files either way.

    ``build.number`` is 0 (the tag version is the sole ordering axis, ADR-0041).
    ``build.noarch: generic`` is what makes rattler-build emit a noarch package
    to the ``noarch/`` subdir — it also FORBIDS a ``--target-platform noarch``
    build flag ("that should be defined in the recipe"), so the noarch build is
    driven off the recipe alone (:func:`_publish_conda_noarch`). NO
    ``dynamic_linking`` block (unlike the tool recipe): a noarch package carries
    no binary, so there is nothing to relink. ``archive_path`` is emitted through
    ``json.dumps`` — a JSON string IS a valid YAML 1.2 double-quoted scalar, so
    spaces / ``#`` / embedded ``"``\\ / ``\\`` are escaped rather than breaking
    the recipe or silently re-pointing the source (the same escaping the tool
    recipe uses).
    """
    return (
        f"package:\n"
        f"  name: {package}\n"
        f'  version: "{version}"\n'
        f"\n"
        f"source:\n"
        f"  - path: {json.dumps(archive_path)}\n"
        f"    target_directory: payload\n"
        f"\n"
        f"build:\n"
        f"  number: 0\n"
        f"  noarch: generic\n"
        f"  script:\n"
        f'    - mkdir -p "${{PREFIX}}/{install_dir}/{package}"\n'
        f'    - cp -R payload/. "${{PREFIX}}/{install_dir}/{package}"\n'
    )


def _conda_channel_env(key_id: str, secret_key: str) -> dict[str, str]:
    """The rattler-build S3 child env for a channel push — the fixed GCS-interop
    endpoint/region plus the write HMAC pair (:data:`CONDA_S3_*_ENV`). The pair
    rides the ENV (merged over the process env by the Exec runner), never argv,
    so the HMAC secret is never recorded in an Exec argv line."""
    return {
        CONDA_S3_ENDPOINT_ENV: CONDA_S3_ENDPOINT,
        CONDA_S3_REGION_ENV: CONDA_S3_REGION,
        CONDA_S3_KEY_ID_ENV: key_id,
        CONDA_S3_SECRET_KEY_ENV: secret_key,
    }


def _conda_channel_url(req: PublishRequest) -> str:
    """The per-repo channel URL a publish writes to — ``s3://<bucket>/<repo>``,
    the bucket DERIVED from the producing repo's visibility (ADR-0065): a private
    repo → the private bucket, a public one → the public bucket. Both writes ride
    the SAME S3-interop rail + HMAC pair (a public-read bucket is still
    write-protected); the tier only picks the bucket."""
    private = bool(req.ghio.repo_is_private(req.repo))
    bucket = PRIVATE_ARTIFACT_BUCKET if private else PUBLIC_ARTIFACT_BUCKET
    return f"s3://{bucket}/{req.repo}"


def _publish_conda_noarch(
    req: PublishRequest, *, key_id: str, secret_key: str
) -> Published:
    """Repackage the single platform-independent archive into ONE
    ``noarch: generic`` ``.conda`` and push+reindex it to the producing repo's
    ``noarch/`` subdir (ADR-0076). See the module docstring's conda entry.

    A DATA artifact has no target triple, so there is no per-subdir fan-out: one
    ``rattler-build build`` renders the noarch recipe into a local channel tree
    (the ``noarch: generic`` in the recipe drives the noarch output — rattler-
    build REFUSES a ``--target-platform noarch`` flag, "that should be defined in
    the recipe"), then one ``rattler-build publish`` uploads AND reindexes
    ``noarch/repodata.json`` on the remote S3 channel — the SAME upload+reindex
    step the per-platform path uses, so ``noarch/`` is written and merged with no
    special store code. Idempotent-resumable via ``--force`` (ADR-0009 phase 2).
    """
    package = conda_noarch_package_name(req.artifact)
    asset_name = conda_noarch_asset(
        req.artifact, req.version, release_assets(req.assets_dir)
    )
    if asset_name is None:
        want = conda_noarch_asset_name(req.artifact, req.version)
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] conda (noarch): no `{want}` under "
            f"{req.assets_dir} — the `{req.artifact.bundle.composition}` "
            f"composition stages the one platform-independent archive the noarch "
            f"mode repackages; run `shipit release bundle` first"
        )
    # Namespace the scratch trees by artifact (as the per-platform path does):
    # `assets_dir` is the stage-wide bundle tree, so an un-namespaced channel
    # tree would let a second conda artifact's post-build glob pick up this one's
    # `.conda`.
    recipe_dir = (
        req.assets_dir / CONDA_RECIPE_SCRATCH / req.artifact.name / NOARCH_SUBDIR
    )
    channel_dir = req.assets_dir / CONDA_CHANNEL_SCRATCH / req.artifact.name
    recipe_dir.mkdir(parents=True, exist_ok=True)
    recipe = recipe_dir / "recipe.yaml"
    recipe.write_text(
        render_conda_noarch_recipe(
            package=package,
            version=req.version,
            archive_path=(req.assets_dir / asset_name).as_posix(),
            install_dir=CONDA_NOARCH_INSTALL_DIR,
        ),
        encoding="utf-8",
        newline="\n",
    )
    req.run_cmd(
        [
            "rattler-build",
            "build",
            "--recipe",
            str(recipe),
            # NO `--target-platform noarch`: rattler-build refuses it ("that
            # should be defined in the recipe") — `build.noarch: generic` in the
            # rendered recipe is what routes the package to `noarch/`.
            "--output-dir",
            str(channel_dir),
            "--package-format",
            "conda",
            "--no-build-id",
            # A noarch package has no per-OS test binary; there is nothing to run.
            "--test",
            "native",
        ],
        req.root,
        None,
    )
    built = sorted((channel_dir / NOARCH_SUBDIR).glob("*.conda"))
    if not built:  # pragma: no cover — a successful build always writes a .conda
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] conda (noarch): rattler-build "
            f"produced no `.conda` under {channel_dir / NOARCH_SUBDIR} — the "
            f"build recorded success but emitted no package"
        )
    channel_url = _conda_channel_url(req)
    req.run_cmd(
        [
            "rattler-build",
            "publish",
            "--to",
            channel_url,
            "--force",
            *[str(path) for path in built],
        ],
        req.root,
        _conda_channel_env(key_id, secret_key),
    )
    return Published(
        req.artifact.name,
        "conda",
        (
            f"built {len(built)} noarch package(s) from {asset_name}",
            f"published {len(built)} package(s) to {channel_url}/noarch (+ reindex)",
        ),
    )


def _publish_conda(req: PublishRequest) -> Published:
    """Repackage the artifact's staged build-output archives into ``.conda``
    packages and push+reindex them to the producing repo's per-repo Artifact
    channel. See the module docstring's conda entry.

    conda-direct (ADR-0077): the ``.conda`` is packaged directly from the staged
    BUILD OUTPUT. The served subdirs, their triples, and the archive names are
    DERIVED from the artifact's declared ``platforms`` (:func:`conda_assets`, the
    causal single source) — never reverse-engineered from a staged filename — and
    the build output is present from the bundle stage with NO gh-release
    dependency (gh-release only uploads the SAME staged tree). One
    ``rattler-build build`` per served+staged subdir renders into a local channel
    tree (rattler-build indexes each subdir on build), then one
    ``rattler-build publish`` uploads AND reindexes the remote S3 channel in a
    single step. Idempotent-resumable: ``--force`` re-uploads and re-indexes,
    so a repair run converges (ADR-0009 phase 2). The S3 endpoint/region are
    the fixed GCS-interop constants; the write HMAC pair rides the env seam
    (never argv), looked up from the endpoint's secret pair.
    """
    if req.repo is None:
        # Belt for direct (test/library) callers — the verb resolves the source
        # slug for any live needs_repo dispatch (conda among them), so a real
        # release never reaches here without it. A loud ReleaseError (not a
        # strippable `assert`) matches the publish stage's error handling: the
        # per-repo channel root IS `<bucket>/<owner/name>`, so an unresolved
        # repo would write to the wrong channel.
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] conda: no source repo resolved — "
            f"the per-repo channel root is `<bucket>/<owner/name>`, so an "
            f"unresolved repo is a hard error, never a mis-rooted channel write"
        )
    key_id = _require_token(req, "conda", CONDA_KEY_ID_SECRET)
    secret_key = _require_token(req, "conda", CONDA_SECRET_KEY_SECRET)
    # NOARCH mode (ADR-0076): a DATA artifact (a platform-independent composition
    # — tarball/zed grammar, or a wasm-pack npm package) has one arch-independent
    # archive and no triple, so it takes the single-package noarch path instead of
    # the per-platform triple→subdir fan-out below. It derives its OWN (flattened)
    # package name (:func:`conda_noarch_package_name`) — the strict
    # :func:`conda_package_name` used by the tool path below would reject a scoped
    # wasm `@scope/name`, so the noarch package name is resolved inside that path,
    # never here. The two modes are additive — a tool artifact never branches.
    if conda_noarch_eligible(req.artifact):
        return _publish_conda_noarch(req, key_id=key_id, secret_key=secret_key)
    package = conda_package_name(req.artifact)
    assets = conda_assets(req.artifact, release_assets(req.assets_dir))
    if not assets:
        served = ", ".join(sorted(CONDA_SUBDIRS.values()))
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] conda: no declared platform maps "
            f"to a served conda subdir ({served}) with a staged build-output "
            f"archive under {req.assets_dir} — the endpoint packages the staged "
            f"`{req.artifact.name}-<triple>.tar.gz`/`.zip` archives derived from "
            f"the artifact's `platforms` declaration; an unserved-only set "
            f"(osx-64 / musl) or an unbuilt matrix publishes nothing"
        )
    binary = integrity_mod.expected_main_binary(req.artifact)
    # Namespace both scratch trees by artifact: `assets_dir` is the stage-wide
    # bundle tree shared by EVERY artifact in the run, so an un-namespaced
    # `conda-channel/<subdir>` would let a second conda artifact's post-build
    # glob pick up the FIRST artifact's `.conda` files (same subdir) and
    # re-publish them under the wrong package — a per-artifact root keeps each
    # build's channel tree its own.
    recipe_root = req.assets_dir / CONDA_RECIPE_SCRATCH / req.artifact.name
    channel_dir = req.assets_dir / CONDA_CHANNEL_SCRATCH / req.artifact.name
    built: list[Path] = []
    actions: list[str] = []
    for subdir, (_triple, asset_name) in sorted(assets.items()):
        binary_name, install_dir, install_binary = _conda_binary_layout(subdir, binary)
        # The release archive stages the binary under a top-level
        # `<artifact>-<triple>/` dir (bundle._compose_archive's contract:
        # `Composed(..., (archive, f"{stem}/"))`, stem == `<artifact>-<triple>`)
        # but rattler-build STRIPS that single top-level dir on extraction, so
        # the binary lands at the work root and the copy source is just
        # `<binary>` — a `<artifact>-<triple>/<binary>` prefix fails with
        # `cp: cannot stat` (#1049, validated on the real lexd archive).
        source_binary = binary_name
        recipe_dir = recipe_root / subdir
        recipe_dir.mkdir(parents=True, exist_ok=True)
        recipe = recipe_dir / "recipe.yaml"
        recipe.write_text(
            render_conda_recipe(
                package=package,
                version=req.version,
                archive_path=(req.assets_dir / asset_name).as_posix(),
                source_binary=source_binary,
                install_dir=install_dir,
                install_binary=install_binary,
            ),
            encoding="utf-8",
            newline="\n",
        )
        req.run_cmd(
            [
                "rattler-build",
                "build",
                "--recipe",
                str(recipe),
                "--target-platform",
                subdir,
                "--output-dir",
                str(channel_dir),
                "--package-format",
                "conda",
                "--no-build-id",
                # `native` runs the recipe's tests only when the build platform
                # equals the host platform; a cross-subdir repackage (linux/win
                # built on the release runner) skips them — there is nothing to
                # run, and the binary cannot execute on the build host anyway.
                "--test",
                "native",
            ],
            req.root,
            None,
        )
        produced = sorted((channel_dir / subdir).glob("*.conda"))
        built.extend(produced)
        actions.append(f"built {len(produced)} {subdir} package(s) from {asset_name}")
    if not built:  # pragma: no cover — a successful build always writes a .conda
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] conda: rattler-build produced no "
            f"`.conda` under {channel_dir} — the build recorded success but "
            f"emitted no package"
        )
    # Tier is DERIVED from the producing repo's visibility (ADR-0065), never
    # declared: a private repo publishes to the private bucket, a public one to
    # the public bucket. Both writes ride the SAME S3-interop rail and HMAC
    # write pair (a public-read bucket is still write-protected) — the tier only
    # picks the bucket, so the consumer read model (authless HTTPS vs S3-interop
    # creds, WS02/WS04) is the only place the tiers diverge. The write HMAC pair
    # rides the env (merged over the process env by the Exec runner), never argv
    # — the keys are registered with the central redactor by the verb's token
    # validation, so they are masked in every Exec record.
    channel_url = _conda_channel_url(req)
    req.run_cmd(
        [
            "rattler-build",
            "publish",
            "--to",
            channel_url,
            "--force",
            *[str(path) for path in built],
        ],
        req.root,
        _conda_channel_env(key_id, secret_key),
    )
    actions.append(f"published {len(built)} package(s) to {channel_url} (+ reindex)")
    return Published(req.artifact.name, "conda", tuple(actions))


# --------------------------------------------------------------------------
# zed (derived) — the Zed-extension registry coordinates (ADR-0068)
# --------------------------------------------------------------------------

#: The foreign, review-gated Zed extensions registry a zed extension publishes
#: THROUGH — by a maintainer-merged PR that bumps the extension's row and
#: advances a git submodule (ADR-0068). shipit never pushes here; the endpoint
#: only RENDERS the row + submodule coordinates for that manual PR.
ZED_REGISTRY = "zed-industries/extensions"

#: The Zed extension manifest the endpoint reads the extension id from. This is
#: ENDPOINT knowledge, not payload knowledge: the Zed registry row is keyed by
#: the id, so the adapter must read it. What the zed BUNDLE ships is
#: producer-declared (``bundle.payload``, ADR-0077/#1092) and unknown here —
#: an extension declares this manifest in its payload or its registry row would
#: name a package that does not carry it.
ZED_MANIFEST = "extension.toml"

#: The scratch subdir under the staged assets tree the zed adapter renders the
#: registry entry into — a SUBDIR, never a top-level file, so a gh-release
#: re-run can never ship it as an asset (the brew/conda scratch prior art).
ZED_SCRATCH = "zed"

#: The Zed extension-id grammar (the ``zed-industries/extensions`` registry id
#: vocabulary): a SINGLE lowercase path segment of letters/digits/``-``/``_``,
#: starting with an alphanumeric. The id comes from UNTRUSTED repo content
#: (``extension.toml``) and is used BOTH as a TOML table key (``[<id>]``) and as
#: a scratch FILENAME (``<id>.extensions-toml``), so a full-match against this
#: conservative grammar is a security boundary, not cosmetics: it rejects any id
#: that could traverse out of the ``dist/zed/`` scratch (``../x``, ``/tmp/x``,
#: ``foo/bar``), break the rendered TOML key (``x]\nversion = …``, newlines,
#: spaces), or blur the filename (``foo.bar``). Deliberately NO ``.`` (unlike the
#: conda name grammar): a dot in a registry id is neither valid Zed vocabulary
#: nor safe as a filename stem here. An id outside it is a loud
#: :class:`ReleaseError`, never a mis-scoped write or a malformed row.
_ZED_EXTENSION_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")


def zed_extension_id(text: str) -> str:
    """The Zed extension ``id`` parsed from an ``extension.toml`` ``text``. Pure.

    A Zed ``extension.toml`` names the extension's registry id at top-level
    ``id = "…"`` — the key both the registry's ``extensions.toml`` row and the
    submodule dir (``extensions/<id>``) are keyed by. A manifest with no ``id``
    (or an unparseable one) is a loud :class:`ReleaseError`: the registry entry
    is keyed by the id, so the endpoint never renders a null-keyed row.

    The id is UNTRUSTED repo content that becomes BOTH a rendered TOML table key
    and a scratch filename, so it is full-matched against the conservative Zed
    id grammar (:data:`_ZED_EXTENSION_ID_RE` — a single lowercase
    letters/digits/``-``/``_`` segment) BEFORE the caller renders or writes with
    it. An id that could traverse the scratch dir (``../x``, ``/tmp/x``,
    ``foo/bar``), break the TOML key (``x]\\nversion = …``, spaces, newlines), or
    blur the filename (``foo.bar``) is a loud :class:`ReleaseError`, never a
    mis-scoped write or a silently-malformed registry row.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ReleaseError(
            f"zed: cannot parse {ZED_MANIFEST} to read the extension id: {exc}"
        ) from exc
    ext_id = data.get("id") if isinstance(data, dict) else None
    if not ext_id or not isinstance(ext_id, str):
        raise ReleaseError(
            f"zed: {ZED_MANIFEST} has no top-level `id` — the "
            f"{ZED_REGISTRY} registry row and its submodule dir "
            f"(extensions/<id>) are keyed by the extension id"
        )
    if not _ZED_EXTENSION_ID_RE.fullmatch(ext_id):
        # Render the untrusted id through ascii() — a rejected id may itself
        # carry newlines / terminal control sequences, and interpolating it raw
        # would inject them into the error output (the same untrusted-content
        # boundary the grammar guards). ascii() escapes them to a safe repr.
        raise ReleaseError(
            f"zed: {ZED_MANIFEST} id {ascii(ext_id)} is not a valid Zed "
            f"extension id (one lowercase segment of letters, digits, `-`, `_`; "
            f"no slashes, dots, spaces, or newlines) — the id becomes both the "
            f"`extensions.toml` table key and a scratch filename, so it must not "
            f"escape the scratch dir or break the rendered row"
        )
    return ext_id


def render_zed_registry_entry(*, ext_id: str, version: str, repo: str, tag: str) -> str:
    """The ``zed-industries/extensions`` registry coordinates a maintainer
    applies in the manual publish PR (ADR-0068). Pure text (the render-vs-effect
    split brew uses).

    Emits the extension's ``extensions.toml`` row (submodule path + the bumped
    version) plus a header naming the submodule rev the PR advances
    ``extensions/<id>`` to — ``github.com/<owner/name>`` at the release ``tag``
    (the authoritative release, ADR-0041). No push: this text is what the human
    step applies, drift-free.
    """
    return (
        f"# {ZED_REGISTRY} registry entry — apply in a PR (ADR-0068):\n"
        f"# advance submodule extensions/{ext_id} to "
        f"github.com/{repo} @ {tag}, then set:\n"
        f"[{ext_id}]\n"
        f'submodule = "extensions/{ext_id}"\n'
        f'version = "{version}"\n'
    )


def _publish_zed(req: PublishRequest) -> Published:
    """Render the ``zed-industries/extensions`` registry coordinates for the
    manually-gated publish PR (ADR-0068). See the module docstring's zed entry.

    The tag is the release: the ``zed`` composition tarballed the extension and
    gh-release shipped it, so this endpoint does the ONE remaining thing shipit
    can own without touching the foreign registry — render the exact
    ``extensions.toml`` row + submodule rev (the extension id read from
    ``extension.toml`` under the rust leg, the bumped version, the release tag)
    into a scratch subdir and report the coordinates. It performs NO cross-repo
    write and needs NO token; opening the PR into ``zed-industries/extensions``
    is a maintainer-gated human step.
    """
    if req.repo is None:
        # Belt for direct (test/library) callers — the verb resolves the source
        # slug for any live needs_repo dispatch (zed among them), so a real
        # release never reaches here without it. A loud ReleaseError (not a
        # strippable `assert`) matches the publish stage's error handling: the
        # submodule rev names github.com/<owner/name>@<tag>, so an unresolved
        # repo would render a null-source row.
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] zed: no source repo resolved — the "
            f"registry submodule points at github.com/<owner/name> @ <tag>, so "
            f"an unresolved repo is a hard error, never a null-source row"
        )
    leg = _leg_for(req.artifact, req.entries, "rust", "zed")
    manifest = _leg_dir(req.root, leg) / ZED_MANIFEST
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] zed: cannot read {manifest} to "
            f"render the registry entry — the zed composition ships this manifest "
            f"as the extension's required core; run `shipit release bundle` first"
        ) from exc
    ext_id = zed_extension_id(text)
    entry = render_zed_registry_entry(
        ext_id=ext_id, version=req.version, repo=req.repo, tag=req.tag
    )
    scratch = req.assets_dir / ZED_SCRATCH
    scratch.mkdir(parents=True, exist_ok=True)
    rendered = scratch / f"{ext_id}.extensions-toml"
    rendered.write_text(entry, encoding="utf-8", newline="\n")
    return Published(
        req.artifact.name,
        "zed",
        (
            f"rendered {ZED_REGISTRY} registry entry for {ext_id} {req.version} "
            f"(submodule extensions/{ext_id} -> github.com/{req.repo}@{req.tag})",
            f"manual step: open a PR against {ZED_REGISTRY} applying this entry — "
            f"the tag is the release, shipit does not push into the registry",
        ),
    )


# --------------------------------------------------------------------------
# The closed registry
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EndpointAdapter:
    """One registry entry: an endpoint name, its ordering stage, the secret
    names it declares to the derivation registry, and its publish function.

    ``stage`` is ``"release"`` or ``"derived"`` (PRD story 35 ordering);
    ``external`` marks the endpoints the RC guard skips on a live-fire cut
    (everything but gh-release — story 33). ``stable_only`` marks the
    endpoints :func:`plan` skips on ANY prerelease (brew's tap is the stable
    channel; notify-downstreams fires on real releases only) with
    ``stable_skip_reason`` as the stated cause. ``needs_repo`` marks the
    endpoints whose publish reads the source ``owner/name`` slug
    (:attr:`PublishRequest.repo`) — brew's asset URLs, notify-downstreams'
    dispatch payload, conda's per-repo channel root — so the verb resolves it
    (one gh round-trip) ONLY when a live dispatch declares the need, keeping a
    laptop RC cut offline.
    ``secrets`` MIRRORS the
    endpoint's :data:`secretreq.ENDPOINT_SECRETS` entry (the one derivation
    authority gh-setup/preflight traverse, WS02, stories 43–45) rather than
    re-declaring the names; the runtime validation set is
    :func:`required_env_keys`.
    """

    name: str
    stage: str
    publish: Callable[[PublishRequest], Published]
    secrets: tuple[str, ...] = ()
    external: bool = True
    stable_only: bool = False
    stable_skip_reason: str = SKIP_STABLE_ONLY
    needs_repo: bool = False


GH_RELEASE = EndpointAdapter(
    "gh-release", "release", _publish_gh_release, external=False
)
CRATES = EndpointAdapter(
    "crates", "release", _publish_crates, secrets=secretreq.ENDPOINT_SECRETS["crates"]
)
PYPI = EndpointAdapter(
    "pypi", "release", _publish_pypi, secrets=secretreq.ENDPOINT_SECRETS["pypi"]
)
NPM = EndpointAdapter(
    "npm", "release", _publish_npm, secrets=secretreq.ENDPOINT_SECRETS["npm"]
)
VSCODE_MARKETPLACE = EndpointAdapter(
    "vscode-marketplace",
    "release",
    _publish_vscode_marketplace,
    secrets=secretreq.ENDPOINT_SECRETS["vscode-marketplace"],
)
OPEN_VSX = EndpointAdapter(
    "open-vsx",
    "release",
    _publish_open_vsx,
    secrets=secretreq.ENDPOINT_SECRETS["open-vsx"],
)
BREW = EndpointAdapter(
    "brew",
    "derived",
    _publish_brew,
    secrets=secretreq.ENDPOINT_SECRETS["brew"],
    stable_only=True,
    needs_repo=True,
)
NOTIFY_DOWNSTREAMS = EndpointAdapter(
    "notify-downstreams",
    "derived",
    _publish_notify_downstreams,
    secrets=secretreq.ENDPOINT_SECRETS["notify-downstreams"],
    stable_only=True,
    stable_skip_reason=SKIP_NOTIFY_PRERELEASE,
    needs_repo=True,
)
CONDA = EndpointAdapter(
    "conda",
    "derived",
    _publish_conda,
    secrets=secretreq.ENDPOINT_SECRETS["conda"],
    # rc-INCLUSIVE (ADR-0064): NOT stable_only — prereleases publish for manual
    # pin-testing. It stays external, so the `-release-rc` live-fire rehearsal
    # (is_live_fire) is still gh-release-only. needs_repo: the per-repo channel
    # root is `<bucket>/<owner/name>`.
    needs_repo=True,
)
ZED = EndpointAdapter(
    "zed",
    "derived",
    _publish_zed,
    secrets=secretreq.ENDPOINT_SECRETS["zed"],
    # stable_only: the zed extensions registry serves stable versions, so a
    # prerelease renders no registry entry (brew's stable-channel shape).
    # external: a `-release-rc` live-fire cut is gh-release-only. needs_repo:
    # the submodule rev names github.com/<owner/name>@<tag>. Renders the manual
    # PR coordinates only — no cross-repo push, no secret (ADR-0068). It does
    # NOT require an unskipped gh-release in the plan (unlike brew/notify):
    # it references the `release prepare` tag, not gh-release assets.
    stable_only=True,
    stable_skip_reason=SKIP_ZED_PRERELEASE,
    needs_repo=True,
)

#: The CLOSED registry, in a stable order (the config boundary's
#: :data:`shipit.config.ENDPOINTS` names exactly this set — asserted in the
#: tests, so the two can never drift). Adding an endpoint is adding an entry
#: here plus the config name — never a switch. The two VS Code marketplace
#: adapters (vscode-marketplace, open-vsx) publish the vsix composition's
#: per-target ``.vsix`` and are external / RC-guarded (TOL02-WS13 #789);
#: notify-downstreams (TOL02-WS16 #792) is present too — not a marketplace
#: publisher but the generated-parser release's cross-repo cascade. conda
#: (ARF01-WS01 #950) is the Artifact channel's producer — a derived endpoint
#: after notify-downstreams. zed (TOL03-WS02 #973, ADR-0068) is the last derived
#: entry — the Zed-extension registry endpoint that renders the manual registry
#: PR's coordinates (the tag is the release; no cross-repo push).
ADAPTERS: tuple[EndpointAdapter, ...] = (
    GH_RELEASE,
    CRATES,
    PYPI,
    NPM,
    VSCODE_MARKETPLACE,
    OPEN_VSX,
    BREW,
    NOTIFY_DOWNSTREAMS,
    CONDA,
    ZED,
)


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
