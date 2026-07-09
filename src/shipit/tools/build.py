"""The build-step planner — pure: (legs, artifacts, version) → build steps.

Where :func:`shipit.tools.legs.plan_legs` decides WHICH legs a tool
invocation runs, this module decides what each **build** leg actually
executes: the join between the path→toolchain map (the leg axis) and the
``[artifacts]`` map (the artifact axis, :func:`shipit.config.load_artifacts`)
— many-to-many per ADR-0007. The rules, in one place:

- a leg with NO artifact build targets runs its base build command once —
  the whole-leg build (a repo needs no artifact map to ``shipit build``);
- a leg WITH matching targets runs once per target, the base command
  narrowed to that artifact's unit: rust appends ``-p <package>``, go
  appends the package path (last — after every flag), npm appends
  ``--workspace <package>``, python takes no narrowing (``uv build`` builds
  the project whole);
- go legs get ``CGO_ENABLED=0`` in the env — the legacy static-by-default
  contract (cgo was opt-in and warned against);
- a SUPPLIED version (ADR-0041: supplied, never computed) is injected into a
  go target's declared ``version_var`` by extending the ``-ldflags`` value
  with ``-X <var>=<version>``; with no version supplied — or no declared var
  — the binary keeps its embedded default (the legacy empty-version-package
  contract). No other toolchain sees the version at build: theirs is a
  manifest projection bumped at prepare.

Pixi is NEVER the build backend (PRD story 9): every step's argv heads the
real builder (cargo / go / uv / npm), taken from the leg (registry default or
per-path override) — never a ``pixi run`` wrapper.

Pure (no I/O, no Exec) — fully fixture-testable, the same split as the leg
planner. The effectful shell that runs the planned steps is
:mod:`shipit.verbs.build`.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from .. import config
from . import legs as legs_mod

#: The go build environment: static-by-default, the legacy go-cli contract
#: (cgo blocked cross-compilation and was explicit opt-in; a repo needing cgo
#: overrides the whole build command per path).
GO_BUILD_ENV: tuple[tuple[str, str], ...] = (("CGO_ENABLED", "0"),)

#: The ldflags flag whose VALUE version injection extends — go replaces (not
#: merges) a repeated ``-ldflags``, so ``-X`` must ride the existing value.
_LDFLAGS = "-ldflags"


@dataclass(frozen=True)
class BuildStep:
    """One planned builder invocation: a build leg, narrowed to one artifact
    target when the artifact map declares one.

    ``argv`` is the COMPLETE builder command (base + target narrowing +
    version injection), run with cwd at ``leg.path``; ``env`` is the extra
    environment merged over the parent's (go's ``CGO_ENABLED=0``), a frozen
    pair tuple so the step stays a hashable value. ``artifact`` names the
    artifact this step produces, ``None`` for a whole-leg build.
    """

    leg: legs_mod.Leg
    argv: tuple[str, ...]
    artifact: str | None = None
    env: tuple[tuple[str, str], ...] = ()

    @property
    def label(self) -> str:
        """The step's display name — ``rust (.) [lex-cli]``, or the bare leg
        label for a whole-leg build — used by every listing."""
        if self.artifact is None:
            return self.leg.label
        return f"{self.leg.label} [{self.artifact}]"


def _inject_version(argv: tuple[str, ...], var: str, version: str) -> tuple[str, ...]:
    """``argv`` with ``-X <var>=<version>`` riding the ``-ldflags`` value.

    Extends the value of the LAST ``-ldflags`` because go takes the last one —
    extending an earlier flag (or appending a second) would let go's own
    last-wins rule silently drop the injected ``-X`` (and the registry
    default's ``-s -w``). So the argv is scanned from the END: whichever
    spelling appears last wins — the split ``-ldflags <value>`` two-token form
    (the registry default) or the joined ``-ldflags=<value>`` single-token
    form (a common per-path override or passthrough). Appends a fresh
    ``-ldflags`` only when the (overridden) command carries none.
    """
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
    """The leg's argv narrowed to one artifact ``target`` (see the module
    docstring's per-toolchain rules). The package lands AFTER any passthrough
    already in ``leg.argv`` — for go the package path must be last anyway,
    and cargo/npm accept their flag anywhere."""
    argv = leg.argv
    if leg.toolchain == "go":
        if version is not None and target.version_var is not None:
            argv = _inject_version(argv, target.version_var, version)
        if target.package is not None:
            argv = (*argv, target.package)
    elif leg.toolchain == "rust" and target.package is not None:
        argv = (*argv, "-p", target.package)
    elif leg.toolchain == "npm" and target.package is not None:
        argv = (*argv, "--workspace", target.package)
    return argv


def _env(leg: legs_mod.Leg) -> tuple[tuple[str, str], ...]:
    return GO_BUILD_ENV if leg.toolchain == "go" else ()


def check_targets_mapped(
    artifacts: Sequence[config.Artifact],
    entries: Sequence[config.ToolchainEntry],
) -> None:
    """Refuse an ``[artifacts]`` build target whose toolchain has NO
    ``[toolchains]`` leg — it would silently never build.

    Checked against the WHOLE toolchain map (never a selector-narrowed
    subset), BEFORE any leg selection or step planning, so a selector can
    neither mask nor fake the inconsistency. The single gate every path that
    plans a build shares: the ``shipit build`` verb and the e2e local-build
    source alike (so one artifact's e2e build is the SAME join its
    ``shipit build`` runs — orphaned targets and all). Pure — a
    :class:`~shipit.config.ConfigError` on inconsistency, nothing otherwise.
    """
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
    """Refuse a build target whose toolchain resolves to MORE THAN ONE of the
    ``planned`` legs — the producing path would be ambiguous.

    A build target names a toolchain, not a path (ADR-0007). When the planned
    legs carry more than one leg of a toolchain some artifact targets, the
    join would build that target in every such leg's cwd (the wrong one for
    all but one, e.g. ``cargo build -p pkg`` in a workspace without ``pkg``).
    Refuse loudly rather than run wrong-cwd builds; declaring one build-bearing
    path per toolchain (or selecting a single leg) resolves it. Checked on the
    PLANNED legs, so a path selector that narrows to one leg is a clean,
    unambiguous build. The single guard every path that plans a build shares —
    the ``shipit build`` verb and the e2e local-build source alike — so
    :func:`plan_build` is only ever reached once its stated precondition (each
    artifact-targeted toolchain resolves to a single leg) holds. Pure — a
    :class:`~shipit.config.ConfigError` on ambiguity, nothing otherwise.
    """
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


def plan_build(
    legs: Sequence[legs_mod.Leg],
    artifacts: Sequence[config.Artifact],
    *,
    version: str | None = None,
) -> tuple[BuildStep, ...]:
    """The ordered build steps for the planned ``legs`` (already selected and
    passthrough-shaped by :func:`~shipit.tools.legs.plan_legs`), joined with
    the ``artifacts`` map's build targets.

    Leg order is the outer order (map declaration order); within a leg, steps
    follow artifact declaration order. ``version`` is the caller-SUPPLIED
    release version (ADR-0041), consumed only by go version injection. A leg
    no artifact target names runs once, un-narrowed — including when an
    artifact map IS present but declares nothing for this leg's toolchain: the
    leg axis is orthogonal to the artifact axis (ADR-0007), so declaring one
    artifact never suppresses the other legs' whole-leg builds.

    The join keys on ``toolchain`` alone — a target names a toolchain, not a
    path (ADR-0007). Two selected legs sharing a toolchain would make the
    producing path of that toolchain's targets ambiguous (each target would
    build in every such leg's cwd — the wrong one for all but one), so every
    caller REFUSES that combination up front
    (:func:`check_targets_unambiguous`); this planner is reached only once
    every artifact-targeted toolchain resolves to a single selected leg. Binding a target to a specific path is a future
    artifact-model extension if a repo ever needs same-toolchain multi-path
    builds; here one build-bearing path per toolchain is assumed (the shape of
    every real consumer: lex is one rust path, a Tauri app is one rust + one
    npm + one mkdocs path).
    """
    steps: list[BuildStep] = []
    for leg in legs:
        matched = [
            (artifact.name, target)
            for artifact in artifacts
            for target in artifact.build
            if target.toolchain == leg.toolchain
        ]
        if not matched:
            steps.append(BuildStep(leg=leg, argv=leg.argv, env=_env(leg)))
            continue
        for name, target in matched:
            steps.append(
                BuildStep(
                    leg=leg,
                    argv=_narrow(leg, target, version),
                    artifact=name,
                    env=_env(leg),
                )
            )
    return tuple(steps)
