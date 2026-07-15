"""Consumer-side Artifact channel — ``[artifact-deps]`` → managed pixi blocks.

The CONSUMER half of the Artifact channel (ADR-0064/0065, ARF01-WS02 #952). A
downstream repo declares a cross-repo artifact-pinned dependency as
``.shipit.toml`` ``[artifact-deps.<pkg>]`` (parsed to a typed
:class:`shipit.config.ArtifactDep`), and ``shipit install`` PROJECTS it here
into managed pixi blocks so pixi resolves/locks/fetches it like any ordinary
dependency and a ``version`` bump re-resolves transparently.

Two halves, kept apart so the projection stays a PURE, network-free core:

- **tier derivation** (:func:`channel_url`) — the access tier is DERIVED from
  the producing repo's visibility (ADR-0065), never declared. A PUBLIC
  producing repo resolves to its authless HTTPS per-repo channel URL; a PRIVATE
  one resolves to an ``s3://<bucket>/<repo>`` channel reached over GCS's
  S3-interop endpoint, whose ``[s3-options]`` config and env-var credentials the
  consumer supplies (ARF01-WS04 #953). The one network-touching read — the
  repo's visibility — lives in the install verb glue (``gh.repo_is_private``);
  this module maps an ALREADY-resolved boolean to a URL, so a test exercises it
  without a round-trip.
- **projection** (:func:`project`) — pure over ``(ArtifactDep, channel_url)``
  pairs: it emits the managed :class:`~shipit.install.units.Unit` blocks the
  reconcile then treats exactly like every other managed block (four-case
  hash-compare, idempotent reconcile-to-noop, ADD/UPDATE on a bump). No
  filesystem, no network — the projection is exercised entirely on values. A
  PRIVATE channel (an ``s3://`` URL) additionally emits the reserved
  ``[s3-options.<bucket>]`` block (endpoint-url / region / force-path-style)
  templated DIRECTLY into TOML — never ``pixi config set s3-options.*``, a
  silent no-op in pixi 0.71.0 (ADR-0065).

Projection shape (the WS02 design, documented for the shepherd): each distinct
TARGET (a declared ``feature`` name, or the default target when ``feature`` is
omitted) becomes one dedicated, shipit-reserved pixi FEATURE carrying that
target's channel URL(s) and version pin(s), wired into an ENVIRONMENT:

- the DEFAULT target (no ``feature``) → the ``shipit-artifacts`` feature, added
  to the ``default`` environment — so the pin resolves on a bare ``pixi
  install`` / ``pixi run`` (the Spec's headline consumer example);
- a NAMED ``feature`` F → the ``shipit-artifacts-<F>`` feature, added to a
  dedicated ``shipit-artifacts-<F>`` environment.

Reserved ``shipit-artifacts*`` feature/env names and EOF-appended feature
tables keep the FEATURE/ENV projection collision-free: it never re-declares a
feature/env table the consumer or another managed block already owns, and never
merges into an existing array (which a marker block cannot do). The one
non-reserved table the projection emits is the private-tier
``[s3-options.<bucket>]`` block (ARF01-WS04): a consumer may already declare it
by hand (the documented manual runbook), so its first splice is guarded by the
reconcile's :class:`~shipit.install.reconcile.PixiTableConflict` — a
pre-existing table skips the block rather than redeclaring it into an
unparseable manifest. BINDING a pin into a pre-existing consumer environment (an
array merge on that env's feature list or on ``[workspace].channels``) is
deliberately out of WS02 scope — see the PR Context handoff.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from ..channel import buckets
from ..config import ArtifactDep
from .units import PIXI_FILE, Unit

#: The pixi env root under a checkout — ``<root>/.pixi/envs/<env>`` is the
#: prefix pixi materializes a projected artifact-dep into (mirrors
#: :data:`shipit.pixienv.run.DEFAULT_ENV_DIR`, the default-env sentinel). The
#: vsix bundle staging (:func:`shipit.release.bundle._stage_vsix_natives`,
#: release#974) joins this to locate a TOOL artifact-dep's on-disk binary.
_PIXI_ENVS_DIR = (".pixi", "envs")

#: The public-tier Artifact channel host + bucket (ADR-0065 — the public-read,
#: authless tier; the private tier's bucket is :data:`PRIVATE_ARTIFACT_BUCKET`
#: below). The per-repo channel root is
#: ``<bucket>/<owner/name>`` (each repo the sole writer of its own repodata,
#: ADR-0064), reached over the authless HTTPS object-storage URL. These re-export
#: the ONE source of truth (:mod:`shipit.channel.buckets`) the producer ``conda``
#: endpoint writes to and the WS03 store provisioner CREATES — a drift test
#: asserts all three agree so the consumer can never read from a bucket the
#: producer never writes to (or the provisioner never made).
PUBLIC_CHANNEL_HOST = buckets.CHANNEL_HOST
PUBLIC_ARTIFACT_BUCKET = buckets.PUBLIC_ARTIFACT_BUCKET

#: The private-tier Artifact channel bucket (ADR-0065 — the credentialed,
#: no-public-access bucket). A private producing repo's channel is reached as an
#: S3-compatible conda channel — ``s3://<bucket>/<repo>`` — over GCS's interop
#: endpoint, NOT the authless HTTPS URL. Re-exports the same source of truth
#: (:data:`shipit.channel.buckets.PRIVATE_ARTIFACT_BUCKET`) the producer writes
#: to and WS03 provisions; the same drift test that pins the public pair pins
#: this one.
PRIVATE_ARTIFACT_BUCKET = buckets.PRIVATE_ARTIFACT_BUCKET

#: The S3-interop scheme a private-tier channel URL carries. The projection keys
#: tier off this prefix — an ``s3://`` channel is private and needs the
#: ``[s3-options]`` block below; an ``https://`` channel is public and authless.
PRIVATE_CHANNEL_SCHEME = "s3://"

#: The validated ``[s3-options.<bucket>]`` values (ADR-0065, live-proven):
#: ``region = "auto"`` and ``force-path-style = true`` are load-bearing for GCS
#: interop, and the endpoint is the global ``storage.googleapis.com`` (the same
#: host the public tier reads over — :data:`PUBLIC_CHANNEL_HOST`). These are
#: templated DIRECTLY into the consumer's pixi TOML — never via ``pixi config
#: set s3-options.*``, a silent no-op in pixi 0.71.0. The read credentials
#: (``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` — a GCS HMAC interop key —
#: or a ``RATTLER_AUTH_FILE``) arrive as ENV VARS (Doppler locally, the sccache
#: credential path in CI), never ``pixi auth login`` (unwired for S3 in 0.71.0),
#: so they are NOT projected into the committed manifest.
S3_OPTIONS_ENDPOINT_URL = PUBLIC_CHANNEL_HOST
S3_OPTIONS_REGION = "auto"
S3_OPTIONS_FORCE_PATH_STYLE = True

#: The reserved pixi feature name the DEFAULT target's channel+pins land in, and
#: the environment prefix a NAMED feature target's isolated feature/env carry.
#: ``shipit-artifacts``-prefixed names are shipit's own namespace — never a
#: consumer feature — so the projection never collides with a consumer table.
DEFAULT_FEATURE = "shipit-artifacts"
DEFAULT_ENV = "default"

#: The managed environments block's unit key + anchor. One consolidated block
#: (all targets' env wiring) anchored under ``[environments]`` — a sibling of
#: the managed lint-env block under the same table header (the proven
#: coexisting-marker-blocks pattern).
ENVIRONMENTS_KEY = f"{PIXI_FILE}#shipit-artifact-deps-environments"
ENVIRONMENTS_ANCHOR = "[environments]"
ENVIRONMENTS_OPEN = (
    "# >>> shipit-managed artifact-dep environments "
    "(do not edit; regenerate via `shipit install`) >>>"
)
ENVIRONMENTS_CLOSE = "# <<< shipit-managed artifact-dep environments <<<"

#: The managed ``[s3-options]`` block's unit key. One consolidated block carries
#: an ``[s3-options.<bucket>]`` table per DISTINCT private bucket present — a
#: top-level table appended at EOF (anchor-less, like a feature block). UNLIKE
#: the reserved ``shipit-artifacts*`` feature tables, ``[s3-options.<bucket>]``
#: reuses a NON-reserved name a consumer may already declare by hand (the
#: documented manual private-tier runbook), so a first splice over a
#: pre-existing table would REDECLARE it and make ``pixi.toml`` unparseable —
#: the reconcile's :class:`~shipit.install.reconcile.PixiTableConflict` guard
#: catches that ADD and skips the block, leaving the consumer's own table
#: authoritative. Emitted ONLY when a private channel is projected; a
#: purely-public consumer never gets it.
S3_OPTIONS_KEY = f"{PIXI_FILE}#shipit-artifact-deps-s3-options"
S3_OPTIONS_OPEN = (
    "# >>> shipit-managed artifact-dep s3-options "
    "(do not edit; regenerate via `shipit install`) >>>"
)
S3_OPTIONS_CLOSE = "# <<< shipit-managed artifact-dep s3-options <<<"


def public_channel_url(repo_slug: str) -> str:
    """The authless HTTPS per-repo channel URL for a PUBLIC producing repo.

    ``<host>/<bucket>/<owner/name>`` (ADR-0065) — the exact channel a consumer
    lists so pixi resolves the artifact with no credentials.
    """
    return f"{PUBLIC_CHANNEL_HOST}/{PUBLIC_ARTIFACT_BUCKET}/{repo_slug}"


def private_channel_url(repo_slug: str) -> str:
    """The S3-interop per-repo channel URL for a PRIVATE producing repo.

    ``s3://<bucket>/<owner/name>`` (ADR-0065) — the S3-compatible conda channel
    a consumer lists; pixi resolves it over GCS's interop endpoint using the
    ``[s3-options.<bucket>]`` config the projection templates and the env-var
    credentials the consumer supplies out of band.
    """
    return f"{PRIVATE_CHANNEL_SCHEME}{PRIVATE_ARTIFACT_BUCKET}/{repo_slug}"


def channel_url(repo_slug: str, *, private: bool) -> str:
    """Derive the channel URL from the producing repo's visibility (ADR-0065).

    ``private`` is the ALREADY-resolved visibility (the verb glue reads it once
    via ``gh.repo_is_private``); the tier is DERIVED from it, never declared. A
    public repo resolves to its authless HTTPS URL; a private one to its
    ``s3://`` S3-interop channel (which additionally makes the projection emit
    the ``[s3-options]`` block — see :func:`project`).
    """
    return private_channel_url(repo_slug) if private else public_channel_url(repo_slug)


def _feature_name(feature: str | None) -> str:
    """The reserved pixi feature name a target's channel+pins land in."""
    return DEFAULT_FEATURE if feature is None else f"{DEFAULT_FEATURE}-{feature}"


def env_name(feature: str | None) -> str:
    """The environment a target's feature is wired into — the default env for
    the default target, an isolated ``shipit-artifacts-<F>`` env for a named one.

    Public because the vsix bundle staging (release#974) needs the SAME
    feature→env mapping the projection uses to locate a materialized artifact-dep
    on disk (:func:`materialized_bin_path`) — one source of truth, so a named
    feature never resolves to a different env in the projection than in staging.
    """
    return DEFAULT_ENV if feature is None else f"{DEFAULT_FEATURE}-{feature}"


def materialized_bin_path(root: Path, dep: ArtifactDep) -> Path:
    """On-disk path of a TOOL artifact-dep's binary in the projected pixi env.

    A tool artifact-dep (``lexd-lsp``, ``lexd``) materializes its binary at
    ``<env-prefix>/bin/<package>`` — ADR-0064: "a tool artifact puts a binary on
    PATH, while a data artifact installs its files into the env". The prefix is
    the pixi env the projection wired the pin into (:func:`env_name` off
    ``dep.feature``), under ``<root>/.pixi/envs/<env>/``. The vsix bundle staging
    (:func:`shipit.release.bundle._stage_vsix_natives`, release#974) reads this
    to copy the per-platform binary into the extension layout before ``vsce
    package``: pixi has ALREADY resolved and materialized the RIGHT platform's
    conda package at this path (the build runner's own subdir — ADR-0064's
    osx-arm64/linux-64/linux-aarch64/win-64 closure), so staging is a copy, never
    a per-target fetch. Pure path arithmetic — no filesystem probe; the caller
    checks existence and reports the "run ``shipit install``" remediation.
    """
    return root.joinpath(*_PIXI_ENVS_DIR, env_name(dep.feature), "bin", dep.package)


def _toml_str_list(values: Sequence[str]) -> str:
    """A TOML inline array of double-quoted string VALUES (the channel URLs /
    feature names the projection emits are URL/identifier-safe, so no escaping
    is needed). Table headers and dependency KEYS go through :func:`_toml_key`
    instead — a dot is a key-path separator there, but harmless inside a string
    value."""
    return "[" + ", ".join(f'"{v}"' for v in values) + "]"


#: TOML bare-key shape — the identifier chars a table header or dependency key
#: may carry UNQUOTED (``A-Za-z0-9_-``). A projected name outside this set (a
#: dotted conda package like ``ruamel.yaml``, or a dotted ``feature``) MUST be
#: emitted as a QUOTED key, else TOML reads the dot as a key-path separator and
#: the one name splits into nested tables/keys — a silently wrong pixi manifest
#: (ARF01-WS02 review). Dots are legitimately valid: the producer's conda
#: package vocabulary admits them (``release.publish._CONDA_PACKAGE_NAME_RE``),
#: so ``config._FEATURE_NAME_RE`` deliberately keeps admitting them and quoting
#: at emission — not rejecting at parse — is what keeps them safe.
_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")


def _toml_key(name: str) -> str:
    """One TOML key / table-name segment, double-quoted only when it must be.

    Bare when every char is bare-key-safe; quoted when it carries a dot (or any
    other non-bare char) so TOML treats it as ONE literal name rather than a
    dotted key-path. Projected names are constrained by ``config._FEATURE_NAME_RE``
    to ``[A-Za-z0-9._-]``, so the only unsafe-for-bare char is ``.`` and a quoted
    key can never need ``"``/``\\``/control-char escaping.
    """
    return name if _BARE_KEY_RE.fullmatch(name) else f'"{name}"'


def _feature_block(
    feature: str | None, resolved: Sequence[tuple[ArtifactDep, str]]
) -> str:
    """The inner text of one target's managed feature block: its channel URLs
    and its version pins, under the reserved ``shipit-artifacts*`` feature.

    Deterministic: channels de-duped in first-seen order (several pins from the
    same producing repo share one channel), pins in declaration order.
    """
    name = _toml_key(_feature_name(feature))
    urls: list[str] = []
    for _, url in resolved:
        if url not in urls:
            urls.append(url)
    lines = [
        f"[feature.{name}]",
        f"channels = {_toml_str_list(urls)}",
        "",
        f"[feature.{name}.dependencies]",
    ]
    lines += [f'{_toml_key(dep.package)} = "{dep.version}"' for dep, _ in resolved]
    return "\n".join(lines)


def _feature_unit(
    feature: str | None, resolved: Sequence[tuple[ArtifactDep, str]]
) -> Unit:
    """One EOF-appended managed block for a target's dedicated feature."""
    name = _feature_name(feature)
    return Unit(
        key=f"{PIXI_FILE}#{name}",
        dest=PIXI_FILE,
        kind="block",
        content=_feature_block(feature, resolved).encode("utf-8"),
        open_marker=(
            f"# >>> shipit-managed artifact-dep feature `{name}` "
            f"(do not edit; regenerate via `shipit install`) >>>"
        ),
        close_marker=f"# <<< shipit-managed artifact-dep feature `{name}` <<<",
        # No anchor: a fresh reserved `[feature.<name>]` table appends at EOF,
        # so it never re-declares a table the consumer already owns.
        anchor=None,
    )


def _environments_unit(features: Sequence[str | None]) -> Unit:
    """The one consolidated environments block wiring every target's feature
    into its environment, anchored under ``[environments]``."""
    lines = [
        f"{_toml_key(env_name(f))} = {_toml_str_list([_feature_name(f)])}"
        for f in features
    ]
    return Unit(
        key=ENVIRONMENTS_KEY,
        dest=PIXI_FILE,
        kind="block",
        content="\n".join(lines).encode("utf-8"),
        open_marker=ENVIRONMENTS_OPEN,
        close_marker=ENVIRONMENTS_CLOSE,
        anchor=ENVIRONMENTS_ANCHOR,
    )


def _s3_bucket(url: str) -> str | None:
    """The bucket of a PRIVATE (``s3://<bucket>/<repo>``) channel URL, or
    ``None`` for a public (``https://``) one — the projection's tier probe.

    The channel URL string is the tier witness: a private repo resolves to an
    ``s3://`` channel (see :func:`private_channel_url`), so the projection reads
    the tier off the URL without a second visibility parameter — the derivation
    stays entirely in :func:`channel_url`.
    """
    if not url.startswith(PRIVATE_CHANNEL_SCHEME):
        return None
    return url[len(PRIVATE_CHANNEL_SCHEME) :].split("/", 1)[0]


def _s3_options_block(buckets: Sequence[str]) -> str:
    """One ``[s3-options.<bucket>]`` table per private bucket (ADR-0065's
    validated GCS-interop shape), templated DIRECTLY into TOML."""
    force = "true" if S3_OPTIONS_FORCE_PATH_STYLE else "false"
    tables = [
        "\n".join(
            [
                f"[s3-options.{_toml_key(bucket)}]",
                f'endpoint-url = "{S3_OPTIONS_ENDPOINT_URL}"',
                f'region = "{S3_OPTIONS_REGION}"',
                f"force-path-style = {force}",
            ]
        )
        for bucket in buckets
    ]
    return "\n\n".join(tables)


def _s3_options_unit(buckets: Sequence[str]) -> Unit:
    """The one consolidated ``[s3-options]`` block for every private bucket in
    play — a fresh reserved top-level table appended at EOF (anchor-less)."""
    return Unit(
        key=S3_OPTIONS_KEY,
        dest=PIXI_FILE,
        kind="block",
        content=_s3_options_block(buckets).encode("utf-8"),
        open_marker=S3_OPTIONS_OPEN,
        close_marker=S3_OPTIONS_CLOSE,
        # No anchor: each `[s3-options.<bucket>]` is a top-level table appended
        # at EOF. The name is NOT in the reserved `shipit-artifacts*` namespace
        # (the manual runbook may have declared it by hand), so a first splice
        # over a pre-existing table would redeclare it — the reconcile's
        # PixiTableConflict guard skips the block in that case.
        anchor=None,
    )


def project(resolved: Sequence[tuple[ArtifactDep, str]]) -> list[Unit]:
    """Project resolved ``(ArtifactDep, channel_url)`` pairs into managed pixi
    :class:`~shipit.install.units.Unit` blocks — the pure, network-free core.

    Groups the deps by TARGET (the declared ``feature``, or the default target)
    in first-seen order, emits one dedicated-feature block per target, and one
    consolidated environments block wiring them in. ``[]`` for no deps (a repo
    declaring no artifact pin projects nothing). The reconcile treats these
    exactly like every other managed block: idempotent reconcile-to-noop, and a
    ``version`` bump changes a feature block's inner text into a single UPDATE.

    A PRIVATE channel (an ``s3://`` URL, ADR-0065) additionally emits ONE
    consolidated ``[s3-options]`` block carrying an ``[s3-options.<bucket>]``
    table per distinct private bucket (first-seen order) — the endpoint-url /
    region / force-path-style config pixi's S3 backend needs, templated directly
    into TOML. A purely-public consumer never gets that block.
    """
    if not resolved:
        return []
    groups: dict[str | None, list[tuple[ArtifactDep, str]]] = {}
    for dep, url in resolved:
        groups.setdefault(dep.feature, []).append((dep, url))
    units = [_feature_unit(feature, pairs) for feature, pairs in groups.items()]
    units.append(_environments_unit(list(groups)))
    buckets: list[str] = []
    for _, url in resolved:
        bucket = _s3_bucket(url)
        if bucket is not None and bucket not in buckets:
            buckets.append(bucket)
    if buckets:
        units.append(_s3_options_unit(buckets))
    return units
