"""The release preflight planner: (artifact map, version, event) -> release plan.

Preflight decides once and everything downstream shares the answer; the
workflow chain re-derives nothing. Pure — no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from ..config import ENDPOINTS, PLATFORMS, Artifact
from . import ReleaseError, bundle, secretreq
from .version import ResolvedVersion

#: The release pipeline's stage vocabulary, in pipeline order.
STAGES: tuple[str, ...] = (
    "preflight",
    "prepare",
    "bundle",
    "assert-bundle",
    "sign",
    "publish",
)

#: The events preflight plans for; both plan identically, but the plan records
#: which so routing and the flow log can tell them apart.
EVENTS: tuple[str, ...] = ("dispatch", "local")

#: The platform an artifact builds on when it declares none.
DEFAULT_PLATFORM: str = "linux-x86_64"


@dataclass(frozen=True)
class PlatformSpec:
    """One platform's release-lane attributes: triple, runner label, suffixes, arch word."""

    target: str
    runner: str
    ext_archive: str
    ext_bin: str
    package_arch: str


#: The closed platform-attribute table, keyed by exactly
#: :data:`shipit.config.PLATFORMS`. Darwin x86_64 cross-compiles on the arm64
#: mac runner; linux-arm64 gets GitHub's native arm runner.
PLATFORM_MATRIX: dict[str, PlatformSpec] = {
    "darwin-arm64": PlatformSpec(
        target="aarch64-apple-darwin",
        runner="macos-latest",
        ext_archive=".tar.gz",
        ext_bin="",
        package_arch="arm64",
    ),
    "darwin-x86_64": PlatformSpec(
        target="x86_64-apple-darwin",
        runner="macos-latest",
        ext_archive=".tar.gz",
        ext_bin="",
        package_arch="amd64",
    ),
    "linux-x86_64": PlatformSpec(
        target="x86_64-unknown-linux-gnu",
        runner="ubuntu-latest",
        ext_archive=".tar.gz",
        ext_bin="",
        package_arch="amd64",
    ),
    "linux-x86_64-musl": PlatformSpec(
        target="x86_64-unknown-linux-musl",
        runner="ubuntu-latest",
        ext_archive=".tar.gz",
        ext_bin="",
        package_arch="amd64",
    ),
    "linux-arm64": PlatformSpec(
        target="aarch64-unknown-linux-gnu",
        runner="ubuntu-24.04-arm",
        ext_archive=".tar.gz",
        ext_bin="",
        package_arch="arm64",
    ),
    "windows-x86_64": PlatformSpec(
        target="x86_64-pc-windows-msvc",
        runner="windows-latest",
        ext_archive=".zip",
        ext_bin=".exe",
        package_arch="amd64",
    ),
}

# Two halves of one registry: import dies loudly if they drift. An explicit
# raise, not `assert`, so the guard survives `python -O`.
if tuple(PLATFORM_MATRIX) != PLATFORMS:
    raise RuntimeError(
        f"PLATFORM_MATRIX keys {tuple(PLATFORM_MATRIX)} drifted from the closed "
        f"PLATFORMS registry {PLATFORMS} — the two halves of the platform "
        f"registry must stay in lockstep"
    )


@dataclass(frozen=True)
class MatrixEntry:
    """One emitted matrix entry: an artifact's build on one platform.

    ``sign`` and ``bundle`` are THE per-entry decisions, resolved once and
    referenced everywhere downstream. The ``bundle`` stage is plan-wide but the fan
    includes every build-bearing artifact, so the per-entry flag is what gates the
    block work — a build-only leg would otherwise stage nothing yet trip
    ``if-no-files-found: error``.
    """

    artifact: str
    platform: str
    target: str
    runner: str
    sign: bool
    bundle: bool
    ext_archive: str
    ext_bin: str
    package_arch: str

    def as_matrix_entry(self) -> dict[str, str | bool]:
        """The GitHub ``matrix.include`` entry — the JSON hand-off shape."""
        return {
            "artifact": self.artifact,
            "platform": self.platform,
            "target": self.target,
            "runner": self.runner,
            "sign": self.sign,
            "bundle": self.bundle,
            "ext_archive": self.ext_archive,
            "ext_bin": self.ext_bin,
            "package_arch": self.package_arch,
        }


@dataclass(frozen=True)
class ReleasePlan:
    """The machine-readable release plan, consumed as workflow job outputs.

    ``tag_only`` IS the ``-release-rc`` live-fire cut whose endpoint set collapsed
    to GH-release-only; ``unsigned`` marks the break-glass plan so the record
    travels with the plan, not just the log.
    """

    version: str
    tag: str
    prerelease: bool
    tag_only: bool
    event: str
    unsigned: bool
    artifacts: tuple[str, ...]
    matrix: tuple[MatrixEntry, ...]
    stages: tuple[str, ...]
    endpoints: tuple[str, ...]
    secrets: tuple[str, ...]
    secret_alternatives: tuple[secretreq.AlternativeSet, ...]

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "tag": self.tag,
            "prerelease": self.prerelease,
            "tag_only": self.tag_only,
            "event": self.event,
            "unsigned": self.unsigned,
            "artifacts": list(self.artifacts),
            "matrix": [entry.as_matrix_entry() for entry in self.matrix],
            "stages": list(self.stages),
            "endpoints": list(self.endpoints),
            "secrets": list(self.secrets),
            "secret_alternatives": [alt.to_dict() for alt in self.secret_alternatives],
        }


def plan(
    artifacts: Sequence[Artifact],
    resolved: ResolvedVersion,
    *,
    event: str = "dispatch",
    unsigned: bool = False,
) -> ReleasePlan:
    """The release plan for ``artifacts`` at ``resolved`` under ``event``. Pure.

    Refused: an artifact map with zero endpoints (nothing would publish), and
    ``unsigned=True`` when the signed plan carries no sign stage. An ``event``
    outside :data:`EVENTS` is a caller bug.
    """
    if event not in EVENTS:
        raise ValueError(f"unknown release event {event!r}; expected one of {EVENTS}")
    declared = {e for artifact in artifacts for e in artifact.endpoints}
    if not declared:
        raise ReleaseError(
            "no artifact declares a distribution endpoint — a release with "
            "zero publish targets is a phantom release (declare `endpoints` "
            "in the [artifacts] map)"
        )

    signed = _matrix(artifacts)
    sign_live = any(entry.sign for entry in signed)
    if unsigned and not sign_live:
        raise ReleaseError(
            "--unsigned is a break-glass for a signing repo; this plan has "
            "no sign stage to skip (no artifact declares sign = true on a "
            "darwin platform)"
        )
    matrix = tuple(replace(e, sign=False) for e in signed) if unsigned else signed

    # Live iff some MATRIX ENTRY bundles, never the artifact-level declaration:
    # the stage and the matrix must agree on WHICH legs bundle.
    bundling = {entry.artifact for entry in matrix if entry.bundle}
    # A composition matching NONE of its artifact's platforms produces no
    # bundle anywhere — refused loudly, never a silently dropped stage.
    for artifact in artifacts:
        if artifact.bundle is not None and artifact.name not in bundling:
            platforms = ", ".join(artifact.platforms) or DEFAULT_PLATFORM
            raise ReleaseError(
                f"[artifacts.{artifact.name}] declares bundle composition "
                f"`{artifact.bundle.composition}`, but it applies to none of "
                f"the artifact's platforms ({platforms}) — a platform-specific "
                f"composition (deb → linux, mac-app → darwin) needs a matching "
                f"platform, or the bundle is never produced"
            )

    # assert-bundle is live only when a bundling composition carries a MAIN
    # BINARY: over a source `.tar.gz` the guard would fail "no main binary".
    by_name = {artifact.name: artifact for artifact in artifacts}
    asserting = {
        name
        for name in bundling
        if (spec := by_name[name].bundle) is not None
        and (comp := bundle.composition(spec.composition)) is not None
        and comp.asserts_binary
    }
    bundle_live = bool(bundling)
    live = {"preflight", "prepare", "publish"}
    if bundle_live:
        live.add("bundle")
    if asserting:
        live.add("assert-bundle")
    if sign_live and not unsigned:
        live.add("sign")
    stages = tuple(stage for stage in STAGES if stage in live)

    # The RC guard as plan shape: a -release-rc cut publishes the GH release
    # only. The order is the closed registry's, release-before-derived.
    endpoints = (
        ("gh-release",)
        if resolved.tag_only
        else tuple(e for e in ENDPOINTS if e in declared)
    )

    sign_stage = "sign" in stages
    secrets = _plan_secrets(endpoints, sign=sign_stage)
    secret_alternatives = (secretreq.NOTARY_SECRETS,) if sign_stage else ()
    return ReleasePlan(
        version=resolved.version,
        tag=resolved.tag,
        prerelease=resolved.prerelease,
        tag_only=resolved.tag_only,
        event=event,
        unsigned=unsigned,
        artifacts=tuple(artifact.name for artifact in artifacts),
        matrix=matrix,
        stages=stages,
        endpoints=endpoints,
        secrets=secrets,
        secret_alternatives=secret_alternatives,
    )


def missing_secrets(
    release_plan: ReleasePlan, env: Mapping[str, str]
) -> tuple[str, ...]:
    """The plan's required secret names absent or empty in ``env``.

    :data:`~shipit.release.secretreq.EMPTY_VALID_SECRETS` names are EXEMPT from the
    non-empty demand, so preflight never contradicts the signer. An alternative set
    is satisfied by ANY complete alternative, and contributes ONE diagnostic when
    none is.
    """
    missing = [
        name
        for name in release_plan.secrets
        if name not in secretreq.EMPTY_VALID_SECRETS and not env.get(name)
    ]
    # Empty-valid names count as present for alternative-set satisfaction too,
    # keeping the contract consistent across plain names and either-sets.
    present = {
        name for name, value in env.items() if value
    } | secretreq.EMPTY_VALID_SECRETS
    missing.extend(
        alt.describe_gap(present)
        for alt in release_plan.secret_alternatives
        if not alt.satisfied(present)
    )
    return tuple(missing)


def missing_pin_refusal(missing: Sequence[tuple[str, str]]) -> str:
    """The refusal text for reusable-workflow ``@vN`` pins that do not resolve.

    GitHub rejects the WHOLE dispatch with an opaque HTTP 422 at its
    workflow-resolution step, before any job runs, so preflight refuses first with
    the one-command bootstrap.
    """
    lines = [
        "reusable-workflow pin(s) will not resolve on the publisher — GitHub "
        "would reject this dispatch with a raw HTTP 422 at its "
        "workflow-resolution step, before any stage runs (#917):"
    ]
    lines.extend(f"  - {repo} @ {ref}" for repo, ref in missing)
    lines.append(
        "the floating v-major ref does not exist yet. Bootstrap it ONCE on the "
        "publisher at the intended stable commit (advance-major.yml then "
        "force-moves it on every later stable tag — ADR-0010):"
    )
    lines.extend(
        f"  git push git@github.com:{repo}.git <stable-sha>:refs/heads/{ref}"
        for repo, ref in missing
    )
    return "\n".join(lines)


def _matrix(artifacts: Sequence[Artifact]) -> tuple[MatrixEntry, ...]:
    """One entry per build-bearing artifact x declared platform, in declaration order."""
    entries: list[MatrixEntry] = []
    for artifact in artifacts:
        if not artifact.build:
            continue
        for platform in artifact.platforms or (DEFAULT_PLATFORM,):
            spec = PLATFORM_MATRIX[platform]
            # In lockstep with the bundle verb's own skip: a whole-artifact
            # flag would mark legs that compose nothing, tripping the upload's
            # `if-no-files-found: error`.
            bundle_here = artifact.bundle is not None and bundle.composition(
                artifact.bundle.composition
            ).applies(spec.target)
            entries.append(
                MatrixEntry(
                    artifact=artifact.name,
                    platform=platform,
                    target=spec.target,
                    runner=spec.runner,
                    sign=artifact.sign and platform.startswith("darwin"),
                    bundle=bundle_here,
                    ext_archive=spec.ext_archive,
                    ext_bin=spec.ext_bin,
                    package_arch=spec.package_arch,
                )
            )
    return tuple(entries)


def _plan_secrets(endpoints: Sequence[str], *, sign: bool) -> tuple[str, ...]:
    """The plan-scoped required names: prepare's push, each live endpoint, and the
    sign-mac CERT PAIR when the sign stage is live.
    """
    seen: dict[str, None] = {}
    for name in secretreq.PREPARE_SECRETS:
        seen[name] = None
    for endpoint in endpoints:
        for name in secretreq.ENDPOINT_SECRETS[endpoint]:
            seen[name] = None
    if sign:
        for name in secretreq.SIGN_MAC_CERT_SECRETS:
            seen[name] = None
    return tuple(seen)
