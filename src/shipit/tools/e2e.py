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
  :class:`Harness` names what runs (its argv) and the canonical ``E2E_*``
  environment it launches under; the CLOSED :data:`HARNESSES` set has three
  entries — :data:`BATS` (the PRD's default, the bats-run of the repo's
  ``bin/check-e2e``, no injected env) and the GUI harnesses :data:`ELECTRON`
  and :data:`TAURI` (TOL03-WS04): the Playwright / WebdriverIO runners that
  honor the ``window.__e2e`` runtime contract and the shared ``E2E_*`` launch
  env (the ``electron-e2e-testing`` / ``tauri-e2e-testing`` skills). A
  declaration selects an entry BY NAME (``e2e = { harness = "electron" }`` →
  :data:`HARNESS_BY_NAME`) — resolving both its argv and its ``E2E_*`` env —
  or gives a raw argv (``e2e.harness = [...]``) that replaces the default for
  that artifact (with no injected env); a future harness is an ENTRY here,
  never a fork of the tool.
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
    """One harness-registry entry: a name, the argv it runs (from the repo
    root, through the one exec seam — ADR-0028), and the canonical ``E2E_*``
    environment it launches under.

    ``env`` is a tuple of ``(VAR, value)`` pairs the effectful shell
    (:mod:`shipit.verbs.e2e`) merges into the harness environment ALONGSIDE
    the per-artifact ``<NAME>_BIN`` injection. The GUI harnesses (electron,
    tauri) carry the shared ``E2E_*`` contract the desktop e2e skills
    prescribe (:data:`_GUI_E2E_ENV`); the bats default carries none — its
    legacy consumers set their own env, and a raw-argv override likewise runs
    with no injected ``E2E_*`` env."""

    name: str
    argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...] = ()


#: The bats harness: the repo's own ``bin/check-e2e`` runner script — the
#: legacy ``bats-e2e.yml`` / ``rust-ci`` e2e contract, under which the
#: consumer suites (padz, dodot) keep working unchanged. This literal is the
#: script head's ONE assembly point (the argv-sweep pins it here).
BATS = Harness("bats", argv=("bin/check-e2e",))

#: The canonical ``E2E_*`` launch environment the GUI harnesses inject, shared
#: by electron and tauri — the ``window.__e2e`` / ``E2E_*`` contract the
#: ``electron-e2e-testing`` / ``tauri-e2e-testing`` skills prescribe: ``E2E``
#: is the top-level "running under tests" signal (always set),
#: ``E2E_HIDE_WINDOW`` suppresses the window/dock on launch (headless CI), and
#: ``E2E_DISABLE_PERSISTENCE`` gives each run clean state. The situational
#: ``E2E_*`` vars (``E2E_USE_BUILD``, ``E2E_SKIP_BUILD``, ``E2E_USER_DATA_DIR``,
#: …) stay the consumer lane's to set — these three are the always-on trio
#: shipit guarantees every GUI harness launches under.
_GUI_E2E_ENV: tuple[tuple[str, str], ...] = (
    ("E2E", "1"),
    ("E2E_HIDE_WINDOW", "1"),
    ("E2E_DISABLE_PERSISTENCE", "1"),
)

#: The electron harness (TOL03-WS04): the consumer's Playwright suite, run
#: through its local devDependency as ``npm exec -- playwright test`` — the
#: node-tool head shipit already provisions (the vsce/ovsx precedent), not a
#: bare-PATH ``npx``. Playwright launches the electron app — its config reads
#: ``<NAME>_BIN`` (the artifact's built binary — the rust/go companion the app
#: drives) — honoring the ``window.__e2e`` runtime contract; shipit injects the
#: shared ``E2E_*`` env (:data:`_GUI_E2E_ENV`). A consumer whose runner differs
#: overrides with a raw ``e2e.harness`` argv.
ELECTRON = Harness(
    "electron", argv=("npm", "exec", "--", "playwright", "test"), env=_GUI_E2E_ENV
)

#: The tauri harness (TOL03-WS04): the SAME ``window.__e2e`` / ``E2E_*``
#: contract, driven over WebDriver instead of Playwright's electron launcher
#: (tauri-driver + WebdriverIO — the sister skill's different launch, one shared
#: runtime contract). ``npm exec -- wdio run wdio.conf.ts`` runs the consumer's
#: WebdriverIO suite (its config spawns tauri-driver against the ``<NAME>_BIN``
#: rust binary); the ``wdio.conf.ts`` path is the canonical default, overridable
#: with a raw ``e2e.harness`` argv.
TAURI = Harness(
    "tauri",
    argv=("npm", "exec", "--", "wdio", "run", "wdio.conf.ts"),
    env=_GUI_E2E_ENV,
)

#: The CLOSED harness registry, the toolchain registry's mirror: a future
#: harness (another WebDriver runner, …) is an entry here, not a fork of the
#: e2e tool. A declaration selects an entry BY NAME
#: (:data:`HARNESS_BY_NAME` — ``e2e = { harness = "electron" }``), resolving
#: both its argv and its ``E2E_*`` env; a raw ``e2e.harness`` argv overrides the
#: argv for one artifact (with no injected env).
HARNESSES: tuple[Harness, ...] = (BATS, ELECTRON, TAURI)

#: The registry indexed by name — the resolution point for a named-harness
#: declaration. Adding a harness is one entry in :data:`HARNESSES`; this index
#: and the planner pick it up. Names are unique (asserted at import).
HARNESS_BY_NAME: dict[str, Harness] = {h.name: h for h in HARNESSES}
assert len(HARNESS_BY_NAME) == len(HARNESSES), "duplicate harness name in HARNESSES"

#: The registry default when an artifact declares ``e2e = {}`` with no
#: ``harness`` (PRD: "registry default: bats-run check-e2e").
DEFAULT_HARNESS = BATS


@dataclass(frozen=True)
class E2eJob:
    """One planned e2e run: an e2e-declaring artifact, the COMPLETE harness
    argv (declared or registry default, passthrough already appended), the
    harness's contributed ``E2E_*`` environment (``env`` — empty for bats and
    for a raw-argv override, the shared GUI trio for electron/tauri), and the
    ``<NAME>_BIN`` env var the resolved binary is injected under (the effectful
    shell merges ``env`` and this injection into the harness environment)."""

    artifact: config.Artifact
    harness: tuple[str, ...]
    env_var: str
    env: tuple[tuple[str, str], ...] = ()

    @property
    def label(self) -> str:
        """The job's display name — the artifact's — used by every listing."""
        return self.artifact.name


def _jobs_list(jobs: Sequence[E2eJob]) -> str:
    return ", ".join(job.label for job in jobs)


def _resolve_harness(
    spec: config.E2eSpec,
) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """The ``(argv, env)`` an e2e declaration resolves to.

    A raw ``harness`` argv override runs with NO injected ``E2E_*`` env; a
    ``harness_name`` resolves to that registry entry's argv AND its canonical
    ``E2E_*`` env (:data:`HARNESS_BY_NAME`); an absent harness is the bats
    default. An unknown name is a loud :class:`~shipit.config.ConfigError`
    naming the registered harnesses — validated HERE (the registry lives in
    this module, not at config's parse boundary), on the same footing as
    :func:`binary_location`'s declaration-consistency refusals.
    """
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
    """The ordered e2e jobs an invocation runs, per the ADR-0039 rules on the
    artifact axis.

    ``artifacts`` is the typed artifact map in DECLARATION order
    (:func:`shipit.config.load_artifacts`); only e2e-DECLARING artifacts
    (``artifact.e2e is not None``) yield a job. A BARE invocation over a repo
    where none declares e2e returns ``()`` — the verb's "no e2e declared",
    exit-0 outcome, NOT an error. An EXPLICIT ``selector`` naming a
    non-declaring (or unknown) artifact is a usage error, never that clean
    no-op: it raises :class:`E2ePlanError` whether other artifacts declare
    e2e or none does. ``passthrough`` is appended verbatim to the (single)
    selected job's harness argv; it is a usage claim that EXACTLY ONE artifact
    receives it, so passthrough selecting several jobs raises
    :class:`E2ePlanError` — and so does passthrough over a repo that declares
    no e2e at all (zero jobs), which is a usage error, never the clean no-op.

    Each declaring artifact's harness is resolved through
    :func:`_resolve_harness`: a named harness (``e2e = { harness = "electron" }``)
    that names no registry entry is a :class:`~shipit.config.ConfigError` — a
    declaration inconsistency (rc 1), NOT a usage error, mirroring
    :func:`binary_location`.
    """
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
        # The bare invocation over a repo with no e2e lane is the ONLY clean
        # empty exit ("no e2e declared", exit 0) — never an error. That no-op
        # is BARE only: passthrough args are a usage claim that exactly one
        # artifact receives them, so passthrough over a repo that declares no
        # e2e is exit-2 usage, never a green no-op (same doctrine as the
        # passthrough-over-several guard below) — otherwise a misconfigured CI
        # lane hides as a green no-op.
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
        # An EXPLICIT selector is a usage claim: the named artifact must be an
        # e2e-declaring one. A miss is exit-2 usage, never a green no-op —
        # whether other artifacts declare e2e (unknown selector) or none does
        # (the named artifact simply forgot its `e2e` table).
        selected = [job for job in jobs if job.artifact.name == selector]
        if not selected:
            available = (
                f"this repo's declared e2e artifacts: {_jobs_list(jobs)}"
                if jobs
                else "no artifact in this repo declares an e2e table"
            )
            raise E2ePlanError(f"unknown e2e artifact {selector!r} — {available}")

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
    artifact: config.Artifact,
    entries: Sequence[config.ToolchainEntry],
    *,
    consumer: str = "e2e",
    target_triple: str | None = None,
) -> BinaryLocation:
    """The expected location of ``artifact``'s built binary — pure, derived
    from the declaration alone (the local-build source verifies the file
    exists after building; this function never touches the filesystem).

    The binary comes from the artifact's FIRST :data:`BINARY_TOOLCHAINS`
    build target, hosted on the FIRST ``[toolchains]`` leg of that
    toolchain: rust → ``target/release/<package or artifact name>`` (cargo's
    release profile output under the leg's workspace), go →
    ``<basename(package)>`` (``go build ./cmd/padz`` writes ``padz`` in its
    cwd), or the artifact name when the target declares no package.
    ``target_triple`` is the cross triple a ``shipit build --target <triple>``
    redirected the rust build to (TOL02-WS11): given, the rust relpath is
    ``target/<triple>/release/<bin>`` — the exact dir cargo wrote — so the
    bundle consumer that cross-built reads where the binary really is; ``None``
    keeps the native ``target/release/`` path. Only the rust branch honours it
    (go/others do not cross-compile by ``--target``). ``consumer`` names the
    binary-consuming stage in the refusal messages —
    ``"e2e"`` (this module's tool) or ``"bundle"`` (the release stage's
    archive composition, TOL02-WS03), which share exactly this derivation.
    Raises :class:`~shipit.config.ConfigError` when the artifact needs a
    binary but declares no binary-producing target, when the target's
    toolchain has no map leg to build on, or when a target's package is a bare
    path-navigation token (``.``, ``./``, ``..``, ``/`` — no basename to name
    the binary, for rust or go alike) — config inconsistencies, refused loudly.
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
        # A path-navigation package (`.`, `./`, `..`, `/`) names no binary —
        # refuse it here with the real diagnosis (shared by rust and go), never
        # let it degrade downstream into a nonsense binary path (rust's
        # `target/release/.`) or a misleading "built green but no binary at
        # <dir>".
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
        # A cross build (`--target <triple>`) redirects cargo to
        # target/<triple>/release/; a native build keeps target/release/. The
        # SAME triple threads from `shipit build` to here, so this reads
        # exactly where the build wrote — never a native/cross guess.
        release_dir = (
            f"target/{target_triple}/release" if target_triple else "target/release"
        )
        relpath = f"{release_dir}/{target.package or artifact.name}"
    elif target.package is None:  # go, module root -> named by the artifact
        relpath = artifact.name
    else:  # go, an explicit package: the built binary is its basename
        relpath = target.package_basename
    return BinaryLocation(leg_path=leg_path, relpath=relpath)
