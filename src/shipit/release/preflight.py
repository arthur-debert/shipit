"""The release preflight planner — pure core (TOL02-WS02, PRD stories 27-29).

A pure function from (artifact map, resolved version, event) to the
machine-readable release plan the composed workflow consumes as job outputs —
the lane planner's release twin (:mod:`shipit.tools.lanes`). YAML never
re-derives a decision (ADR-0040: the invariants live in the blocks, the chain
carries zero logic); preflight decides once and everything downstream shares
the answer — the legacy tauri-app ``should-sign`` lesson: one definition, no
disagreeing checks. Preflight runs before any toolchain exists and before
prepare writes history — cheapest checks first (ADR-0009): a missing token
can never strand a half-released tag.

The plan's five fields (story 27):

- ``artifacts`` — the declared artifact names.
- ``matrix`` — the OS×arch entries (one per build-bearing artifact ×
  declared platform; an artifact declaring no platforms builds on the
  ordinary linux lane): target triple, runner label, per-entry sign flag,
  archive/binary extensions, packaging arch — the legacy ``setup-matrix``
  vocabulary, driven by declarations instead of workflow inputs. Opting out
  of a platform drops its entry, never leaves a dead job.
- ``stages`` — the live stage subset of ``preflight → prepare → bundle →
  assert-bundle → sign → publish``: bundle (and its assert) only where a
  bundle step exists, sign only when declared signing meets a darwin entry
  (and ``--unsigned`` did not flip it).
- ``endpoints`` — the post-RC-guard endpoint set: a ``-release-rc`` version
  plans GH-release-only with every external endpoint dropped from the plan,
  not filtered later in YAML (story 33's guard consumed as plan shape;
  enforcement stays central in the publish verb, WS05).
- ``secrets`` — the required secret names for THIS plan, from the same
  requirement registries gh-setup syncs (:mod:`shipit.release.secretreq`),
  scoped to the plan's live endpoints and stages: no sign stage, no Apple
  names (so an unsigned or non-darwin run checks only what it uses).

``unsigned=True`` is the explicit break-glass (story 29, CONTEXT.md:
visible, recorded, never ambient): it flips the plan to the unsigned path
and is REFUSED (:class:`~shipit.release.ReleaseError`) when the signed plan
would carry no sign stage — nothing to break-glass. The verb shell logs
every use as a recorded ``release.unsigned`` event.

Presence validation (story 28) is :func:`missing_secrets` over an injected
environment mapping: the workflow's caller injects each GitHub secret as a
same-named env var, so presence-at-preflight proves publish will not starve.
The verb hard-fails on a non-empty result — declared signing with missing
signing secrets can never silently ship unsigned.

Pure module: no I/O; the effectful shell is ``shipit release preflight``
(:mod:`shipit.verbs.release`), rendering text or ``--json`` (ADR-0030).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from ..config import ENDPOINTS, PLATFORMS, Artifact
from . import ReleaseError, bundle, secretreq
from .version import ResolvedVersion

#: The release pipeline's stage vocabulary, pipeline order (PRD story 19).
STAGES: tuple[str, ...] = (
    "preflight",
    "prepare",
    "bundle",
    "assert-bundle",
    "sign",
    "publish",
)

#: The events preflight plans for — the composed workflow's dispatch run and
#: the laptop cut (the PRD's release triggers; both plan identically today,
#: recorded in the plan so routing and the flow log can tell them apart).
EVENTS: tuple[str, ...] = ("dispatch", "local")

#: The platform an artifact builds on when it declares none — the fleet's
#: ordinary linux runner (the lane planner's default-runner twin): a
#: python wheel or npm tarball builds once, anywhere.
DEFAULT_PLATFORM: str = "linux-x86_64"


@dataclass(frozen=True)
class PlatformSpec:
    """One platform's release-lane attributes — the legacy ``setup-matrix``
    per-entry vocabulary, declared once here instead of transcribed per
    workflow. ``target`` is the rust-style target triple (cross-compiling
    toolchains consume it; single-artifact toolchains ignore it), ``runner``
    the GitHub ``runs-on`` label, ``ext_archive``/``ext_bin`` the platform's
    archive and binary suffixes, ``package_arch`` the packaging (deb/pkg)
    arch word."""

    target: str
    runner: str
    ext_archive: str
    ext_bin: str
    package_arch: str


#: The closed platform-attribute table, keyed by exactly
#: :data:`shipit.config.PLATFORMS` (drift-guarded by test). Darwin x86_64
#: cross-compiles on the arm64 mac runner (rustup target, the legacy path);
#: linux-arm64 gets GitHub's native arm runner.
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

# The declaration vocabulary (config) and the attribute table (here) are two
# halves of one registry — import dies loudly if they ever drift. An explicit
# raise, not `assert`: the guard must survive `python -O` (which strips asserts).
if tuple(PLATFORM_MATRIX) != PLATFORMS:
    raise RuntimeError(
        f"PLATFORM_MATRIX keys {tuple(PLATFORM_MATRIX)} drifted from the closed "
        f"PLATFORMS registry {PLATFORMS} — the two halves of the platform "
        f"registry must stay in lockstep"
    )


@dataclass(frozen=True)
class MatrixEntry:
    """One emitted OS×arch matrix entry: an artifact's build on one platform.

    ``sign`` is THE per-entry signing decision (resolved once, referenced
    everywhere downstream): the artifact declares signing, the platform is
    darwin, and no ``--unsigned`` break-glass flipped the plan. ``bundle`` is
    the parallel per-entry bundle decision — whether THIS entry's artifact
    declares a composition THAT APPLIES TO THIS PLATFORM
    (:meth:`shipit.release.bundle.Composition.applies`, the same predicate the
    bundle verb skips on: a platform-specific composition — deb on linux,
    mac-app on darwin — bundles only its matching legs). The ``bundle`` stage
    is a plan-wide flag (live when ANY artifact bundles), but the fan includes
    every build-bearing artifact whether or not it bundles, so the per-entry
    flag is what gates the block work: wf-build bundles/uploads only ``bundle``
    entries, and the unsigned assert projection (:func:`plan`'s consumers)
    narrows to them — a build-only artifact (or a leg the composition does not
    apply to) beside a bundled one would otherwise stage nothing yet trip
    ``if-no-files-found: error`` and a phantom assert download."""

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
    """The machine-readable release plan (story 27) — frozen, typed
    (ADR-0030), consumed by the composed workflow as job outputs.

    ``version``/``tag``/``prerelease``/``tag_only`` restate the resolver's
    verdict the plan was shaped around (``tag_only`` IS the ``-release-rc``
    live-fire cut whose endpoint set collapsed to GH-release-only);
    ``unsigned`` marks the break-glass plan so the record travels with the
    plan, not just the log."""

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

    def to_dict(self) -> dict:
        """The ``--json`` projection — exactly the plan's declared fields."""
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
        }


def plan(
    artifacts: Sequence[Artifact],
    resolved: ResolvedVersion,
    *,
    event: str = "dispatch",
    unsigned: bool = False,
) -> ReleasePlan:
    """The release plan for ``artifacts`` at ``resolved`` under ``event``. Pure.

    Refusals are :class:`~shipit.release.ReleaseError` (the shared CLI error
    shell renders them, exit 1): a zero-endpoint artifact map (the legacy
    python-pkg "phantom release" — nothing would publish), and
    ``unsigned=True`` when the signed plan would carry no sign stage (nothing
    to break-glass, story 29). An ``event`` outside :data:`EVENTS` is a
    caller bug (``ValueError``): the verb's click boundary admits only the
    closed choices.
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

    bundle_live = any(artifact.bundle is not None for artifact in artifacts)
    live = {"preflight", "prepare", "publish"}
    if bundle_live:
        live |= {"bundle", "assert-bundle"}
    if sign_live and not unsigned:
        live.add("sign")
    stages = tuple(stage for stage in STAGES if stage in live)

    # Story 33 consumed as plan shape: a -release-rc cut publishes the GH
    # release only; every external endpoint is absent from the plan. The
    # canonical order is the closed registry's (gh-release first, brew — the
    # derived endpoint — last: the publish stage's release-before-derived
    # ordering).
    endpoints = (
        ("gh-release",)
        if resolved.tag_only
        else tuple(e for e in ENDPOINTS if e in declared)
    )

    secrets = _plan_secrets(endpoints, sign="sign" in stages)
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
    )


def missing_secrets(
    release_plan: ReleasePlan, env: Mapping[str, str]
) -> tuple[str, ...]:
    """The plan's required secret names absent (or empty) in ``env`` — story
    28's hard-fail set, checked over exactly what the plan uses (a
    non-signing plan never checks the Apple names). Pure; the shell injects
    the real environment."""
    return tuple(name for name in release_plan.secrets if not env.get(name))


def _matrix(artifacts: Sequence[Artifact]) -> tuple[MatrixEntry, ...]:
    """The signed-path matrix: one entry per build-bearing artifact ×
    declared platform (none declared → :data:`DEFAULT_PLATFORM`), in
    declaration order. An artifact with no build targets emits no entries
    (the tag is its release — nothing fans out)."""
    entries: list[MatrixEntry] = []
    for artifact in artifacts:
        if not artifact.build:
            continue
        for platform in artifact.platforms or (DEFAULT_PLATFORM,):
            spec = PLATFORM_MATRIX[platform]
            # Per-entry bundle decision, in lockstep with the bundle verb's own
            # skip (shipit.verbs.release: `comp.applies(target)`): a
            # platform-specific composition (deb on linux, mac-app on darwin)
            # bundles only its matching legs. A whole-artifact flag would mark
            # every leg of a multi-platform artifact bundle-bearing, then the
            # non-applicable legs run `release bundle`, compose NOTHING (the
            # verb skips), and trip the upload's `if-no-files-found: error`.
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
    """The plan-scoped required names, from the SAME requirement registries
    gh-setup syncs (:mod:`shipit.release.secretreq` — one definition of every
    name): prepare's push, each live endpoint's declaration, and the sign-mac
    names only when the sign stage is live."""
    seen: dict[str, None] = {}
    for name in secretreq.PREPARE_SECRETS:
        seen[name] = None
    for endpoint in endpoints:
        for name in secretreq.ENDPOINT_SECRETS[endpoint]:
            seen[name] = None
    if sign:
        for name in secretreq.SIGN_MAC_SECRETS:
            seen[name] = None
    return tuple(seen)
