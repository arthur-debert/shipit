"""The artifact-source seam — (artifact declaration) → resolved binary path.

``shipit e2e`` consumes a BUILT artifact. WHERE the binary comes from is
this seam's whole concern, and the seam is deliberately narrow: a source is
anything with ``resolve(artifact) -> Path`` (the :class:`ArtifactSource`
protocol), returning the ABSOLUTE path of the artifact's executable binary
or raising :class:`ArtifactSourceError`.

**This interface is the WF02 boundary** (PRD story 12). Three sources are
planned behind it — local build, CI-artifact download, and the content-key
store — and the seam's signature must NOT change when the later two arrive:
they are new implementations of ``resolve``, slotted in without touching the
e2e tool's interface. TOL01 ships exactly ONE source,
:class:`LocalBuildSource`; source selection stays OUT of the verb's CLI
surface until there is more than one (WF02's call, not this module's).

Unlike its pure siblings (:mod:`.legs`, :mod:`.build`, :mod:`.e2e`), this
module's source is EFFECTFUL — a local build runs real builders — but only
through the injected step runner (the one exec seam, ADR-0028): the module
itself assembles no argv (the WS02 build planner does) and execs nothing
directly, so recorded-invocation tests drive it exactly like the verbs.
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
    """The source could not produce the artifact's binary — a RUNTIME
    failure (the build failed, the binary is missing/not executable after a
    green build), rendered by the e2e verb as the job's hard failure. The
    message is the whole user-facing diagnosis."""


@runtime_checkable
class ArtifactSource(Protocol):
    """The seam: one method, one signature — frozen at the WF02 boundary.

    ``resolve`` returns the absolute path of ``artifact``'s executable
    binary, or raises :class:`ArtifactSourceError` (could not produce) /
    :class:`~shipit.config.ConfigError` (the declaration itself is
    inconsistent). Future sources (CI-artifact download, the content-key
    store) implement exactly this — nothing about the e2e tool changes when
    they arrive.
    """

    def resolve(self, artifact: config.Artifact) -> Path: ...  # pragma: no cover


#: The step-runner seam a :class:`LocalBuildSource` executes through —
#: ``(argv, cwd, env) -> ExecResult`` — the same boundary
#: :mod:`shipit.verbs.build` injects for its steps (its ``_run_step`` is the
#: production implementation; tests inject a recorder).
StepRunner = Callable[[Sequence[str], Path, Mapping[str, str]], execrun.ExecResult]


class LocalBuildSource:
    """The local-build source: produce the binary via the WS02 build path.

    ``resolve`` plans the artifact's declared build targets against the
    repo's ``[toolchains]`` legs (:func:`shipit.tools.build.plan_build` —
    the SAME join ``shipit build`` runs, so one artifact's e2e build is
    byte-for-byte its ``shipit build`` steps), runs every step through the
    injected ``run_step``, then returns the built binary's absolute path
    (:func:`shipit.tools.e2e.binary_location`), verified to exist and be
    executable. Any failing step — nonzero rc, a missing builder — is an
    :class:`ArtifactSourceError` naming the step; a green build whose
    expected binary is absent is one naming the path.

    ``echo`` receives the source's progress lines (each step's command and
    verbatim builder output) — the verb passes ``print``; tests capture.
    No version is supplied to the build (ADR-0041: e2e exercises the
    working tree's binary; a supplied release version is the release
    pipeline's concern).
    """

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
        """See :class:`ArtifactSource`; the docstring above is the contract."""
        # The SAME orphan-target gate `shipit build` runs, BEFORE narrowing:
        # a declared target whose toolchain has no [toolchains] leg must fail
        # loudly, never be quietly dropped by the `wanted` filter below — that
        # would build a partial artifact and still run the harness, breaking
        # the "same join `shipit build` runs" contract this source promises.
        build_mod.check_targets_mapped([artifact], self.entries)
        location = e2e_mod.binary_location(artifact, self.entries)
        # The SAME ambiguity guard `shipit build` runs before `plan_build`: a
        # toolchain this artifact targets that maps to MORE THAN ONE build leg
        # has no single producing path, so the join would build in every such
        # leg's cwd while `binary_location` verifies only the first — a
        # wrong-cwd build reporting a binary from a different leg. Refuse it
        # here (on the whole planned build map, before narrowing) so e2e's
        # build really is the join `shipit build` runs, `plan_build`'s stated
        # precondition and all.
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
                # A builder missing from PATH (or any launch failure) is the
                # HARD-fail signal, never a silent skip (ADR-0028).
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
                # The builder's output is echoed VERBATIM (the build sibling's
                # contract): `echo` is line-oriented (the verb's `print` adds a
                # newline), so drop only a single trailing newline the builder
                # emitted — never any other content — so `echo` neither doubles
                # a present final newline nor swallows the builder's own text.
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
