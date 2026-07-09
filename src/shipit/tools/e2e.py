"""The e2e planner — pure: (artifacts, selector, passthrough) → e2e jobs.

**e2e** is the artifact-consuming Tool (CONTEXT.md): where ``test`` takes the
tree as input, ``e2e`` takes a built **Artifact** — the verb resolves the
artifact's binary through the artifact-source seam
(:mod:`shipit.tools.artifact_source`), injects its absolute path into the
consumer-declared harness as ``<NAME>_BIN``, and runs the harness. This
module is the pure half of that tool, in one place:

- **opt-in is declaration** (PRD story 11): only artifacts whose
  ``[artifacts.<name>].e2e`` table exists (:class:`shipit.config.E2eSpec`)
  have an e2e job; a repo with no ``e2e`` key has NO e2e lane — the verb
  reports "no e2e declared" and exits 0. Opting out is the absence of
  config, never a flag.
- **the harness registry** mirrors the toolchain registry: a
  :class:`Harness` names what runs; the CLOSED :data:`HARNESSES` set has one
  entry today, :data:`BATS` — the bats-run of the repo's ``bin/check-e2e``
  (the PRD's registry default, the legacy ``bats-e2e.yml`` runner script).
  A declared ``e2e.harness`` argv replaces the default for that artifact; a
  future non-bats harness is an ENTRY here, never a fork of the tool.
- **``<NAME>_BIN`` derivation** (:func:`bin_env_var`) is a pure function —
  uppercase, ``-`` → ``_``, ``_BIN`` suffix — kept byte-for-byte compatible
  with the legacy fleet's ``tr '[:lower:]-' '[:upper:]_'`` derivation
  (padz → ``PADZ_BIN``, dodot → ``DODOT_BIN``), deliberately (PRD).
- **the binary's expected location** (:func:`binary_location`) is derived
  from the artifact's FIRST binary-producing build target
  (:data:`BINARY_TOOLCHAINS`): rust → ``target/release/<package or name>``
  under its leg's path, go → the built package's basename (``./cmd/padz`` →
  ``padz``) under its leg's path. An e2e artifact with no binary-producing
  target, or one whose toolchain has no ``[toolchains]`` leg, is a loud
  :class:`~shipit.config.ConfigError` — never a quiet skip.
- **selector/passthrough** follow the ADR-0039 tool rules on the ARTIFACT
  axis (e2e's leg equivalent): a bare invocation fans out over every
  declared e2e artifact; a selector names one artifact; passthrough args
  append verbatim to exactly ONE selected job's harness argv — several jobs
  selected is a hard :class:`E2ePlanError`, never a broadcast.

Pure (no I/O, no Exec) — fully fixture-testable, the same split as
:mod:`shipit.tools.legs` / :mod:`shipit.tools.build`. The effectful shell is
:mod:`shipit.verbs.e2e`; the effectful artifact resolution is
:mod:`shipit.tools.artifact_source`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from .. import config

#: The toolchains whose build emits the executable the e2e harness consumes.
#: python and npm build targets produce packages/bundles, not the injectable
#: binary — a Tauri artifact's npm target builds the frontend, but its
#: ``<NAME>_BIN`` is the rust side's binary.
BINARY_TOOLCHAINS: tuple[str, ...] = ("rust", "go")

#: The legacy fleet's env-var derivation, pinned: ``tr '[:lower:]-'
#: '[:upper:]_'`` — ASCII lowercase uppercased, ``-`` → ``_``, every other
#: character untouched. :func:`bin_env_var` must keep matching it (the
#: consumer suites — padz's ``PADZ_BIN``, dodot's ``DODOT_BIN`` — predate
#: shipit and must keep working unchanged).
_LEGACY_TR = str.maketrans("abcdefghijklmnopqrstuvwxyz-", "ABCDEFGHIJKLMNOPQRSTUVWXYZ_")


def bin_env_var(name: str) -> str:
    """The ``<NAME>_BIN`` env var the harness receives for artifact ``name``.

    Pure; byte-for-byte the legacy ``tr '[:lower:]-' '[:upper:]_'`` + ``_BIN``
    contract: ``padz`` → ``PADZ_BIN``, ``lex-cli`` → ``LEX_CLI_BIN``.
    """
    return name.translate(_LEGACY_TR) + "_BIN"


class E2ePlanError(Exception):
    """The invocation cannot be planned — a USAGE error (exit 2, ADR-0030).

    Raised for an unknown artifact selector, and for passthrough args that
    would reach more than one harness. The message is the whole user-facing
    diagnosis (it names the declared e2e artifacts), so the verb prints it
    verbatim. The :class:`shipit.tools.legs.LegPlanError` mirror, on the
    artifact axis.
    """


@dataclass(frozen=True)
class Harness:
    """One harness-registry entry: a name and the argv it runs (from the repo
    root, through the one exec seam — ADR-0028)."""

    name: str
    argv: tuple[str, ...]


#: The bats harness: the repo's own ``bin/check-e2e`` runner script — the
#: legacy ``bats-e2e.yml`` / ``rust-ci`` e2e contract, under which the
#: consumer suites (padz, dodot) keep working unchanged. This literal is the
#: script head's ONE assembly point (the argv-sweep pins it here).
BATS = Harness("bats", argv=("bin/check-e2e",))

#: The CLOSED harness registry, the toolchain registry's mirror: a future
#: non-bats harness (a WebDriver runner, …) is an entry here, not a fork of
#: the e2e tool. Today the declaration side is binary — a declared
#: ``e2e.harness`` argv, or the default — so the registry carries only the
#: default; a named-harness declaration would select an entry by name.
HARNESSES: tuple[Harness, ...] = (BATS,)

#: The registry default when an artifact declares ``e2e = {}`` with no
#: ``harness`` argv (PRD: "registry default: bats-run check-e2e").
DEFAULT_HARNESS = BATS


@dataclass(frozen=True)
class E2eJob:
    """One planned e2e run: an e2e-declaring artifact, the COMPLETE harness
    argv (declared or registry default, passthrough already appended), and
    the ``<NAME>_BIN`` env var the resolved binary is injected under."""

    artifact: config.Artifact
    harness: tuple[str, ...]
    env_var: str

    @property
    def label(self) -> str:
        """The job's display name — the artifact's — used by every listing."""
        return self.artifact.name


def _jobs_list(jobs: Sequence[E2eJob]) -> str:
    return ", ".join(job.label for job in jobs)


def plan_e2e(
    artifacts: Sequence[config.Artifact],
    *,
    selector: str | None = None,
    passthrough: Sequence[str] = (),
) -> tuple[E2eJob, ...]:
    """The ordered e2e jobs an invocation runs, per the ADR-0039 rules on the
    artifact axis.

    ``artifacts`` is the typed artifact map in DECLARATION order
    (:func:`shipit.config.load_artifacts`); only e2e-DECLARING artifacts
    (``artifact.e2e is not None``) yield a job — ``()`` when none declares,
    the verb's "no e2e declared" outcome, NOT an error. ``selector`` names
    one artifact; ``passthrough`` is appended verbatim to the (single)
    selected job's harness argv. Raises :class:`E2ePlanError` on an unknown
    selector or passthrough selecting several jobs.
    """
    jobs = [
        E2eJob(
            artifact=artifact,
            harness=(
                artifact.e2e.harness
                if artifact.e2e.harness is not None
                else DEFAULT_HARNESS.argv
            ),
            env_var=bin_env_var(artifact.name),
        )
        for artifact in artifacts
        if artifact.e2e is not None
    ]
    if not jobs:
        return ()

    selected = jobs
    if selector is not None:
        selected = [job for job in jobs if job.artifact.name == selector]
        if not selected:
            raise E2ePlanError(
                f"unknown e2e artifact {selector!r} — this repo's declared "
                f"e2e artifacts: {_jobs_list(jobs)}"
            )

    if passthrough and len(selected) > 1:
        # Never a broadcast: args meant for one harness would break another.
        raise E2ePlanError(
            f"passthrough args need exactly one e2e artifact, but "
            f"{len(selected)} are selected: {_jobs_list(selected)} — "
            f"e.g. `shipit e2e {selected[0].label} -- …`"
        )
    if passthrough:
        job = selected[0]
        selected = [replace(job, harness=(*job.harness, *passthrough))]
    return tuple(selected)


@dataclass(frozen=True)
class BinaryLocation:
    """Where an artifact's built binary lands, relative to the repo root:
    the producing leg's map ``leg_path`` plus the build's ``relpath`` within
    it (rust's ``target/release/<bin>``, go's package basename)."""

    leg_path: str
    relpath: str


def binary_location(
    artifact: config.Artifact, entries: Sequence[config.ToolchainEntry]
) -> BinaryLocation:
    """The expected location of ``artifact``'s built binary — pure, derived
    from the declaration alone (the local-build source verifies the file
    exists after building; this function never touches the filesystem).

    The binary comes from the artifact's FIRST :data:`BINARY_TOOLCHAINS`
    build target, hosted on the FIRST ``[toolchains]`` leg of that
    toolchain: rust → ``target/release/<package or artifact name>`` (cargo's
    release profile output under the leg's workspace), go →
    ``<basename(package)>`` (``go build ./cmd/padz`` writes ``padz`` in its
    cwd), or the artifact name when the target declares no package. Raises
    :class:`~shipit.config.ConfigError` when the artifact declares e2e but
    no binary-producing target, or when the target's toolchain has no map
    leg to build on — config inconsistencies, refused loudly.
    """
    target = next((t for t in artifact.build if t.toolchain in BINARY_TOOLCHAINS), None)
    if target is None:
        raise config.ConfigError(
            f"[artifacts].{artifact.name} declares e2e but no binary-producing "
            f"build target ({' / '.join(BINARY_TOOLCHAINS)}) — e2e injects a "
            f"built binary as <NAME>_BIN, so the artifact must declare where "
            f"one comes from"
        )
    leg_path = next(
        (entry.path for entry in entries if entry.toolchain == target.toolchain),
        None,
    )
    if leg_path is None:
        raise config.ConfigError(
            f"[artifacts].{artifact.name} e2e needs a [toolchains] "
            f"{target.toolchain} leg to build its binary, and none is mapped"
        )
    if target.toolchain == "rust":
        relpath = f"target/release/{target.package or artifact.name}"
    else:  # go
        relpath = (
            PurePosixPath(target.package).name if target.package else artifact.name
        )
    return BinaryLocation(leg_path=leg_path, relpath=relpath)
