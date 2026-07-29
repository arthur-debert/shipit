"""``tools/artifact_source`` — (artifact declaration) → resolved binary path.

Effectful only through the injected step runner; it assembles no argv itself.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from .. import config, execrun
from . import build as build_mod
from . import e2e as e2e_mod
from . import legs as legs_mod


class ArtifactSourceError(Exception):
    """The source could not produce the binary; the message is the whole diagnosis."""


@runtime_checkable
class ArtifactSource(Protocol):
    """``resolve`` returns the artifact's executable binary as an ABSOLUTE path."""

    def resolve(self, artifact: config.Artifact) -> Path: ...  # pragma: no cover


StepRunner = Callable[[Sequence[str], Path, Mapping[str, str]], execrun.ExecResult]


class LocalBuildSource:
    """Produces the binary by running the SAME build join ``shipit build`` runs."""

    def __init__(
        self,
        *,
        root: Path,
        entries: Sequence[config.ToolchainEntry],
        run_step: StepRunner,
        echo: Callable[[str], None] = print,
    ) -> None:
        self.root = root
        self.entries = tuple(entries)
        self.run_step = run_step
        self.echo = echo

    def resolve(self, artifact: config.Artifact) -> Path:
        # Both gates run BEFORE narrowing, on the whole build map: the `wanted`
        # filter below would otherwise quietly drop an orphan target, and an
        # ambiguous toolchain would build in a leg `binary_location` never checks.
        build_mod.check_targets_mapped([artifact], self.entries)
        location = e2e_mod.binary_location(artifact, self.entries)
        build_legs = legs_mod.plan_legs(self.entries, tool="build")
        build_mod.check_targets_unambiguous([artifact], build_legs)
        wanted = {target.toolchain for target in artifact.build}
        legs = [leg for leg in build_legs if leg.toolchain in wanted]
        steps = build_mod.plan_build(legs, [artifact])
        for step in steps:
            command = shlex.join(step.argv)
            self.echo(f"e2e: build {step.label}: {command}")
            try:
                result = self.run_step(
                    step.argv, self.root / step.leg.path, dict(step.env)
                )
            except execrun.ExecError as exc:
                if exc.cause == execrun.CAUSE_MISSING_BINARY:
                    detail = f"{step.argv[0]}: not found on PATH (provision it)"
                else:
                    detail = f"{step.argv[0]}: could not run: {exc}"
                raise ArtifactSourceError(
                    f"local build of artifact {artifact.name} could not run "
                    f"{step.label} ({command}): {detail}"
                ) from exc
            output = result.stdout + result.stderr
            if output:
                # `echo` is line-oriented, so drop only a single trailing newline.
                self.echo(output[:-1] if output.endswith("\n") else output)
            if result.rc != 0:
                raise ArtifactSourceError(
                    f"local build of artifact {artifact.name} failed: "
                    f"{step.label} ({command}) exited {result.rc}"
                )
        path = (self.root / location.leg_path / location.relpath).resolve()
        if not path.is_file():
            raise ArtifactSourceError(
                f"artifact {artifact.name} built green but its binary is not "
                f"at {path} — the declared build target and the actual build "
                f"output disagree"
            )
        if not os.access(path, os.X_OK):
            raise ArtifactSourceError(
                f"artifact {artifact.name} binary at {path} is not executable"
            )
        return path
