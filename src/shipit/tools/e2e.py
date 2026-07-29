"""``tools/e2e`` — the pure planner: (artifacts, selector, passthrough) → e2e jobs.

See docs/adr/0039-tools-as-verbs.md.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from .. import config

#: The toolchains whose build emits the executable the e2e harness consumes.
BINARY_TOOLCHAINS: tuple[str, ...] = ("rust", "go")

#: ASCII lowercase uppercased and ``-`` → ``_``; every other character is left alone.
_LEGACY_TR = str.maketrans("abcdefghijklmnopqrstuvwxyz-", "ABCDEFGHIJKLMNOPQRSTUVWXYZ_")


def bin_env_var(name: str) -> str:
    return name.translate(_LEGACY_TR) + "_BIN"


class E2ePlanError(Exception):
    """The invocation cannot be planned; the message is the whole user-facing diagnosis."""


@dataclass(frozen=True)
class Harness:
    name: str
    argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...] = ()


BATS = Harness("bats", argv=("bin/check-e2e",))

#: The always-on ``E2E_*`` trio every GUI harness launches under.
_GUI_E2E_ENV: tuple[tuple[str, str], ...] = (
    ("E2E", "1"),
    ("E2E_HIDE_WINDOW", "1"),
    ("E2E_DISABLE_PERSISTENCE", "1"),
)

ELECTRON = Harness(
    "electron", argv=("npm", "exec", "--", "playwright", "test"), env=_GUI_E2E_ENV
)

TAURI = Harness(
    "tauri",
    argv=("npm", "exec", "--", "wdio", "run", "wdio.conf.ts"),
    env=_GUI_E2E_ENV,
)

#: The CLOSED harness registry: a new harness is an entry here.
HARNESSES: tuple[Harness, ...] = (BATS, ELECTRON, TAURI)


def _index_by_name(harnesses: tuple[Harness, ...]) -> dict[str, Harness]:
    """The registry indexed by name; a duplicate name raises rather than shadowing."""
    index: dict[str, Harness] = {}
    for harness in harnesses:
        if harness.name in index:
            raise config.ConfigError(
                f"duplicate e2e harness name {harness.name!r} in HARNESSES — "
                f"each registry entry's name must be unique (it is the "
                f"selection key for a named `e2e.harness` declaration)"
            )
        index[harness.name] = harness
    return index


HARNESS_BY_NAME: dict[str, Harness] = _index_by_name(HARNESSES)

DEFAULT_HARNESS = BATS


@dataclass(frozen=True)
class E2eJob:
    """One planned e2e run; ``harness`` is COMPLETE argv, passthrough already appended."""

    artifact: config.Artifact
    harness: tuple[str, ...]
    env_var: str
    env: tuple[tuple[str, str], ...] = ()

    @property
    def label(self) -> str:
        return self.artifact.name


def _jobs_list(jobs: Sequence[E2eJob]) -> str:
    return ", ".join(job.label for job in jobs)


def _resolve_harness(
    spec: config.E2eSpec,
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """The ``(argv, env)`` a declaration resolves to; a raw argv override injects NO env."""
    if spec.harness is not None:
        return spec.harness, ()
    if spec.harness_name is not None:
        harness = HARNESS_BY_NAME.get(spec.harness_name)
        if harness is None:
            raise config.ConfigError(
                f"unknown e2e harness {spec.harness_name!r} — the registered "
                f"harnesses are {', '.join(sorted(HARNESS_BY_NAME))}; or declare "
                f'a raw argv list (e.g. ["bats", "tests/e2e.bats"])'
            )
        return harness.argv, harness.env
    return DEFAULT_HARNESS.argv, DEFAULT_HARNESS.env


def plan_e2e(
    artifacts: Sequence[config.Artifact],
    *,
    selector: str | None = None,
    passthrough: Sequence[str] = (),
) -> tuple[E2eJob, ...]:
    """The ordered e2e jobs; a BARE invocation with nothing declared returns ``()``."""
    jobs = []
    for artifact in artifacts:
        if artifact.e2e is None:
            continue
        argv, env = _resolve_harness(artifact.e2e)
        jobs.append(
            E2eJob(
                artifact=artifact,
                harness=argv,
                env=env,
                env_var=bin_env_var(artifact.name),
            )
        )
    if selector is None:
        if not jobs:
            if passthrough:
                raise E2ePlanError(
                    f"passthrough args need exactly one e2e artifact, but this "
                    f"repo declares no e2e — no artifact to receive "
                    f"{list(passthrough)}"
                )
            return ()
        selected = jobs
    else:
        selected = [job for job in jobs if job.artifact.name == selector]
        if not selected:
            available = (
                f"this repo's declared e2e artifacts: {_jobs_list(jobs)}"
                if jobs
                else "no artifact in this repo declares an e2e table"
            )
            raise E2ePlanError(f"unknown e2e artifact {selector!r} — {available}")

    if passthrough and len(selected) > 1:
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
    """Where an artifact's built binary lands: ``leg_path`` plus ``relpath`` within it."""

    leg_path: str
    relpath: str


def binary_location(
    artifact: config.Artifact,
    entries: Sequence[config.ToolchainEntry],
    *,
    consumer: str = "e2e",
    target_triple: str | None = None,
) -> BinaryLocation:
    """Where ``artifact``'s built binary is expected, from the declaration alone.

    ``target_triple`` is the cross triple a ``--target`` build redirected rust to.
    """
    target = next((t for t in artifact.build if t.toolchain in BINARY_TOOLCHAINS), None)
    if target is None:
        raise config.ConfigError(
            f"[artifacts].{artifact.name} declares {consumer} but no "
            f"binary-producing build target ({' / '.join(BINARY_TOOLCHAINS)}) "
            f"— {consumer} consumes a built binary, so the artifact must "
            f"declare where one comes from"
        )
    leg_path = next(
        (entry.path for entry in entries if entry.toolchain == target.toolchain),
        None,
    )
    if leg_path is None:
        raise config.ConfigError(
            f"[artifacts].{artifact.name} {consumer} needs a [toolchains] "
            f"{target.toolchain} leg to build its binary, and none is mapped"
        )
    if target.package is not None and target.package_basename is None:
        hint = (
            f"declare a real package path like './cmd/{artifact.name}', or drop "
            f"`package` to build the module root as {artifact.name}"
            if target.toolchain == "go"
            else f"declare a real crate name, or drop `package` to build "
            f"{artifact.name}"
        )
        raise config.ConfigError(
            f"[artifacts].{artifact.name} {target.toolchain} build target "
            f"package {target.package!r} has no binary name — {hint}"
        )
    if target.toolchain == "rust":
        release_dir = (
            f"target/{target_triple}/release" if target_triple else "target/release"
        )
        relpath = f"{release_dir}/{target.package or artifact.name}"
    elif target.package is None:  # go, module root -> named by the artifact
        relpath = artifact.name
    else:  # go, an explicit package: the built binary is its basename
        relpath = target.package_basename
    return BinaryLocation(leg_path=leg_path, relpath=relpath)
