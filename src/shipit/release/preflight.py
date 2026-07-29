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

STAGES: tuple[str, ...] = (
    "preflight",
    "prepare",
    "bundle",
    "assert-bundle",
    "sign",
    "publish",
)

#: Both plan identically; the plan records which, for routing and the log.
EVENTS: tuple[str, ...] = ("dispatch", "local")

DEFAULT_PLATFORM: str = "linux-x86_64"


@dataclass(frozen=True)
class PlatformSpec:
    target: str
    runner: str
    ext_archive: str
    ext_bin: str
    package_arch: str


#: The closed attribute table, keyed by exactly :data:`shipit.config.PLATFORMS`.
#: Darwin x86_64 cross-compiles on the arm64 mac runner.
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

# Two halves of one registry. An explicit raise, not `assert`, survives -O.
if tuple(PLATFORM_MATRIX) != PLATFORMS:
    raise RuntimeError(
        f"PLATFORM_MATRIX keys {tuple(PLATFORM_MATRIX)} drifted from the closed "
        f"PLATFORMS registry {PLATFORMS} — the two halves of the platform "
        f"registry must stay in lockstep"
    )


@dataclass(frozen=True)
class MatrixEntry:
    """One matrix entry: an artifact's build on one platform, with its per-entry flags."""

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
    """The machine-readable release plan, consumed as workflow job outputs."""

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
    """The release plan for ``artifacts`` at ``resolved`` under ``event``. Pure."""
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

    # Live iff some MATRIX ENTRY bundles: the stage and the matrix must agree.
    bundling = {entry.artifact for entry in matrix if entry.bundle}
    # A composition matching NONE of its platforms produces no bundle at all.
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

    # Over a source `.tar.gz` the guard would fail "no main binary".
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

    # The RC guard as plan shape; the order is release-before-derived.
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
    """The plan's required secret names absent or empty in ``env``."""
    missing = [
        name
        for name in release_plan.secrets
        if name not in secretreq.EMPTY_VALID_SECRETS and not env.get(name)
    ]
    # Empty-valid names count as present for alternative sets too.
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
    """The refusal text for reusable-workflow ``@vN`` pins that do not resolve."""
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
    entries: list[MatrixEntry] = []
    for artifact in artifacts:
        if not artifact.build:
            continue
        for platform in artifact.platforms or (DEFAULT_PLATFORM,):
            spec = PLATFORM_MATRIX[platform]
            # In lockstep with the bundle verb's own skip: a whole-artifact
            # flag would mark legs that compose nothing.
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
