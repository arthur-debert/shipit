"""``tools/build`` — pure planner: (legs, artifacts, version) → build steps.

See docs/adr/0007-artifacts-are-a-map.md.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from .. import config
from . import legs as legs_mod

GO_BUILD_ENV: tuple[tuple[str, str], ...] = (("CGO_ENABLED", "0"),)

#: go replaces (not merges) a repeated ``-ldflags``, so ``-X`` must ride the value.
_LDFLAGS = "-ldflags"

#: Redirects a cargo build to ``target/<triple>/release/``.
_CARGO_TARGET = "--target"

#: A narrowed build supersedes it: go discards binaries when several packages build.
_GO_ALL_PACKAGES = "./..."


@dataclass(frozen=True)
class BuildStep:
    """``argv`` runs with cwd at ``leg.path``; ``artifact`` is ``None`` for a whole leg."""

    leg: legs_mod.Leg
    argv: tuple[str, ...]
    artifact: str | None = None
    env: tuple[tuple[str, str], ...] = ()

    @property
    def label(self) -> str:
        if self.artifact is None:
            return self.leg.label
        return f"{self.leg.label} [{self.artifact}]"


def _inject_version(argv: tuple[str, ...], var: str, version: str) -> tuple[str, ...]:
    injection = f"-X {var}={version}"
    joined = f"{_LDFLAGS}="
    out = list(argv)
    for i in range(len(out) - 1, -1, -1):
        arg = out[i]
        if arg.startswith(joined):  # joined form: -ldflags=<value>
            out[i] = f"{arg} {injection}"
            return tuple(out)
        if arg == _LDFLAGS and i + 1 < len(out):  # split form: -ldflags <value>
            out[i + 1] = f"{out[i + 1]} {injection}"
            return tuple(out)
    return (*out, _LDFLAGS, injection)


def _narrow(
    leg: legs_mod.Leg, target: config.BuildTarget, version: str | None
) -> tuple[str, ...]:
    argv = leg.argv
    if leg.toolchain == "go":
        if version is not None and target.version_var is not None:
            argv = _inject_version(argv, target.version_var, version)
        argv = tuple(arg for arg in argv if arg != _GO_ALL_PACKAGES)
        if target.package is not None:
            argv = (*argv, target.package)
    elif leg.toolchain == "rust" and target.package is not None:
        argv = (*argv, "-p", target.package)
    elif leg.toolchain == "npm" and target.package is not None:
        argv = (*argv, "--workspace", target.package)
    return argv


def _whole_leg_argv(leg: legs_mod.Leg) -> tuple[str, ...]:
    """The whole-leg argv, verbatim except that go's ``./...`` is forced last."""
    if leg.toolchain != "go" or _GO_ALL_PACKAGES not in leg.argv:
        return leg.argv
    rest = tuple(arg for arg in leg.argv if arg != _GO_ALL_PACKAGES)
    return (*rest, _GO_ALL_PACKAGES)


def _env(leg: legs_mod.Leg) -> tuple[tuple[str, str], ...]:
    return GO_BUILD_ENV if leg.toolchain == "go" else ()


def check_targets_mapped(
    artifacts: Sequence[config.Artifact],
    entries: Sequence[config.ToolchainEntry],
) -> None:
    """Refuse a build target with no leg, against the WHOLE (unselected) toolchain map."""
    mapped = {entry.toolchain for entry in entries}
    orphaned = sorted(
        {
            f"{artifact.name} -> {target.toolchain}"
            for artifact in artifacts
            for target in artifact.build
            if target.toolchain not in mapped
        }
    )
    if orphaned:
        raise config.ConfigError(
            "[artifacts] build targets name toolchains with no [toolchains] "
            f"leg: {'; '.join(orphaned)}"
        )


def check_targets_unambiguous(
    artifacts: Sequence[config.Artifact], planned: Sequence[legs_mod.Leg]
) -> None:
    targeted = {target.toolchain for artifact in artifacts for target in artifact.build}
    counts = Counter(leg.toolchain for leg in planned)
    ambiguous = sorted(
        f"{toolchain} ({counts[toolchain]} paths)"
        for toolchain in targeted
        if counts[toolchain] > 1
    )
    if ambiguous:
        raise config.ConfigError(
            "[artifacts] build targets name a toolchain mapped to multiple "
            f"selected [toolchains] paths, so the producing path is ambiguous: "
            f"{'; '.join(ambiguous)}. A target names a toolchain, not a path "
            "(ADR-0007) — declare one build-bearing path per toolchain, or "
            "select a single leg (e.g. `shipit build <path>`)."
        )


def _cross(
    argv: tuple[str, ...], leg: legs_mod.Leg, target: str | None
) -> tuple[str, ...]:
    if target is None or leg.toolchain != "rust":
        return argv
    return (*argv, _CARGO_TARGET, target)


def plan_build(
    legs: Sequence[legs_mod.Leg],
    artifacts: Sequence[config.Artifact],
    *,
    version: str | None = None,
    target: str | None = None,
) -> tuple[BuildStep, ...]:
    """The ordered build steps for ``legs``, joined with ``artifacts`` on toolchain.

    A leg no target names runs once, un-narrowed. Assumes
    :func:`check_targets_unambiguous` has already passed.
    """
    steps: list[BuildStep] = []
    for leg in legs:
        matched = [
            (artifact.name, build_target)
            for artifact in artifacts
            for build_target in artifact.build
            if build_target.toolchain == leg.toolchain
        ]
        if not leg.argv:
            # A buildless toolchain: skip it when untargeted, refuse when targeted.
            if matched:
                orphaned = ", ".join(sorted(name for name, _ in matched))
                raise config.ConfigError(
                    f"[artifacts] build target(s) name the buildless toolchain "
                    f"{leg.toolchain!r}, which declares no build command (a "
                    f"{leg.toolchain} leg has no compile step — e.g. a Neovim "
                    f"plugin is interpreted source): {orphaned}. Remove the "
                    f"build target, or point it at a toolchain that builds."
                )
            continue
        if not matched:
            steps.append(
                BuildStep(
                    leg=leg,
                    argv=_cross(_whole_leg_argv(leg), leg, target),
                    env=_env(leg),
                )
            )
            continue
        for name, build_target in matched:
            steps.append(
                BuildStep(
                    leg=leg,
                    argv=_cross(_narrow(leg, build_target, version), leg, target),
                    artifact=name,
                    env=_env(leg),
                )
            )
    return tuple(steps)
