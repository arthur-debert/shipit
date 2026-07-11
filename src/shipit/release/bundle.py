"""The bundle composition registry — build outputs → unsigned Artifacts.

``shipit release bundle`` (TOL02-WS03, workflows.lex §1) is the stage that
composes toolchain outputs into the unsigned distributables ("package" is
retired as a word — the stage is bundle). This module is its CLOSED
composition registry — the lint ``Lang`` / toolchain-registry shape, one
entry per way an artifact composes (ADR-0007: the bundle step is DECLARED
per artifact via ``[artifacts.<name>].bundle = { composition = "…" }``,
keyed off the map — never a project-Kind switch) — plus the compose
functions the entries carry:

- **archive** — the legacy ``rust-cli.yml`` "Package binaries" contract: a
  ``<name>-<target>/`` staging subdir carrying the built binary plus docs
  (README/CHANGELOG/LICENSE when present), archived as
  ``<name>-<target>.tar.gz`` (``zip`` + ``.exe`` for windows targets) — the
  exact layout the brew formulas and GH release assets already assume.
- **deb** — cargo-deb against the PRE-BUILT release binary
  (``--no-build --no-strip``: the deb wraps the same binary the tarball
  ships — the legacy ``build-deb.yml`` contract), hard-failing when no
  ``.deb`` appears. Linux targets only.
- **wheel** — ``uv build`` emitting BOTH the wheel and the sdist into the
  bundle output tree (the legacy ``python-pkg.yml`` build job: one build,
  consumed by multiple publish targets).
- **mac-app** — the coupled UNSIGNED ``.app``/``.dmg`` pair (the declared
  bundler builds the .app inside the .dmg run; they are not cleanly
  separable) PLUS the inner ``.app`` re-emitted as the reseal payload
  (``<name>.unsigned-app.tar.gz``, a tar preserving symlinks and exec
  bits): cross-job artifact upload destroys a ``.app``'s symlinks and exec
  bits, and the signer reseals the ``.dmg`` from the SIGNED ``.app``
  (workflows.lex §3.1: bundle-unsigned → sign-reopens-and-reseals, never
  sign-then-bundle). The declared ``command`` is the only consumer-specific
  part; a missing payload is a bundle-stage failure, never a signer
  surprise. Mac targets only.

Every external command runs through the injected runner — the one Exec seam
(ADR-0028); the ``cargo`` / ``uv`` / ``tar`` / ``zip`` argv literals below
are those tools' one BUNDLE-side assembly point, whitelisted in the
mechanized argv sweep (``tests/test_tool_argv_sweep.py``). Compose functions
write ONLY under the request's bundle output tree (ADR-0009's barrier: a
failing composition exits with nothing half-written outside it); uploading
anything anywhere is publish's job, signing the signer's.

The effectful shell (walking the artifact map, deciding which compositions
apply to the current target) is ``shipit release bundle``
(:mod:`shipit.verbs.release`); the sibling integrity guard is
:mod:`shipit.release.integrity` (``assert-bundle``).
"""

from __future__ import annotations

import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import config, execrun
from ..tools import e2e as e2e_mod
from . import ReleaseError

#: The docs the archive composition ships beside the binary WHEN PRESENT —
#: the legacy "Package binaries" step's set (README/CHANGELOG/LICENSE).
DOC_FILES: tuple[str, ...] = (
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "LICENSE.md",
    "LICENSE.txt",
)

#: The runner seam a composition executes through — ``(argv, cwd) ->
#: ExecResult`` with check=True semantics (a failing command raises
#: :class:`~shipit.execrun.ExecError`). The verb injects the production
#: runner; tests inject a recorder (the recorded-invocation surface).
RunCmd = Callable[[Sequence[str], Path], execrun.ExecResult | None]


@dataclass(frozen=True)
class ComposeRequest:
    """Everything one composition needs: the artifact and its repo context.

    ``out_dir`` is the ABSOLUTE bundle output tree — the only place a
    composition may write. ``target`` is the target triple naming the
    platform composed for (``<name>-<target>`` naming, windows detection);
    ``target_declared`` says whether the caller supplied it explicitly
    (``--target``), in which case cargo-deb receives it too — a derived
    host triple is naming-only and never redirects cargo's target dir.
    """

    artifact: config.Artifact
    entries: tuple[config.ToolchainEntry, ...]
    root: Path
    out_dir: Path
    target: str
    run_cmd: RunCmd
    target_declared: bool = False


@dataclass(frozen=True)
class Composed:
    """One composed artifact: what was produced, as out-tree-relative paths."""

    artifact: str
    composition: str
    outputs: tuple[str, ...]

    def to_dict(self) -> dict:
        """The ``--json`` field set — exactly the declared outputs."""
        return {
            "artifact": self.artifact,
            "composition": self.composition,
            "outputs": list(self.outputs),
        }


def _is_windows(target: str) -> bool:
    """Whether ``target`` is a windows triple (zip + ``.exe``, not tar). Pure."""
    return "windows" in target


def _leg_for(
    artifact: config.Artifact,
    entries: Sequence[config.ToolchainEntry],
    toolchain: str,
    composition: str,
) -> config.ToolchainEntry:
    """The first ``[toolchains]`` leg of ``toolchain``, or a loud refusal
    naming the composition that needed it (never a quiet skip)."""
    leg = next((entry for entry in entries if entry.toolchain == toolchain), None)
    if leg is None:
        raise ReleaseError(
            f"[artifacts.{artifact.name}] {composition} composition needs a "
            f"[toolchains] {toolchain} leg, and none is mapped"
        )
    return leg


def _compose_archive(req: ComposeRequest) -> Composed:
    """The tarball/zip contract: ``<name>-<target>/`` staging subdir (binary
    + docs), archived beside it. See the module docstring's archive entry."""
    windows = _is_windows(req.target)
    loc = e2e_mod.binary_location(req.artifact, req.entries, consumer="bundle")
    binary = req.root / loc.leg_path / (loc.relpath + (".exe" if windows else ""))
    if not binary.is_file():
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] archive composition: no built "
            f"binary at {binary} — bundle composes build outputs; run "
            f"`shipit build` first"
        )
    stem = f"{req.artifact.name}-{req.target}"
    stage = req.out_dir / stem
    stage.mkdir(parents=True, exist_ok=True)
    shutil.copy2(binary, stage / binary.name)
    for doc in DOC_FILES:
        doc_path = req.root / doc
        if doc_path.is_file():
            shutil.copy2(doc_path, stage / doc)
    if windows:
        archive = f"{stem}.zip"
        req.run_cmd(["zip", "-r", archive, stem], req.out_dir)
    else:
        archive = f"{stem}.tar.gz"
        req.run_cmd(["tar", "-czf", archive, stem], req.out_dir)
    return Composed(req.artifact.name, "archive", (archive, f"{stem}/"))


def _compose_deb(req: ComposeRequest) -> Composed:
    """cargo-deb over the pre-built release binary — no rebuild, no strip;
    a run that produces no ``.deb`` is a hard failure (legacy build-deb)."""
    leg = _leg_for(req.artifact, req.entries, "rust", "deb")
    package = next(
        (t.package for t in req.artifact.build if t.toolchain == "rust" and t.package),
        None,
    )
    req.out_dir.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in req.out_dir.glob("*.deb")}
    argv = ["cargo", "deb", "--no-build", "--no-strip"]
    if package is not None:
        argv += ["-p", package]
    if req.target_declared:
        # cargo-deb derives the Debian arch from --target (the legacy
        # build-deb contract) — passed through only when the caller declared
        # one, because it also redirects which target/ dir cargo-deb reads.
        argv += ["--target", req.target]
    argv += ["--output", str(req.out_dir)]
    req.run_cmd(argv, req.root / leg.path)
    produced = sorted({p.name for p in req.out_dir.glob("*.deb")} - before)
    if not produced:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] deb composition: cargo deb "
            f"completed but produced no .deb under {req.out_dir} — hard fail, "
            f"never a quiet pass (legacy build-deb contract)"
        )
    return Composed(req.artifact.name, "deb", tuple(produced))


def _compose_wheel(req: ComposeRequest) -> Composed:
    """``uv build`` into the bundle output tree; BOTH the wheel and the sdist
    must appear — one build, consumed by multiple publish targets."""
    leg = _leg_for(req.artifact, req.entries, "python", "wheel")
    req.out_dir.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in req.out_dir.iterdir()}
    req.run_cmd(["uv", "build", "--out-dir", str(req.out_dir)], req.root / leg.path)
    produced = {p.name for p in req.out_dir.iterdir()} - before
    wheels = sorted(name for name in produced if name.endswith(".whl"))
    sdists = sorted(name for name in produced if name.endswith(".tar.gz"))
    if not wheels or not sdists:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] wheel composition: uv build "
            f"completed but the bundle tree is missing "
            f"{'a wheel' if not wheels else 'an sdist'} under {req.out_dir}"
        )
    return Composed(req.artifact.name, "wheel", (*wheels, *sdists))


def _compose_mac_app(req: ComposeRequest) -> Composed:
    """The coupled unsigned ``.app``/``.dmg`` pair + the reseal payload.

    Runs the DECLARED bundler (the one consumer-specific part), collects the
    exactly-one pair from the declared ``source`` dir, and re-emits the inner
    ``.app`` as ``<name>.unsigned-app.tar.gz`` — the symlink/exec-bit-
    preserving tar the signer reseals from (workflows.lex §3.1). A missing
    payload is THIS stage's failure, never a signer surprise.
    """
    spec = req.artifact.bundle
    assert spec is not None and spec.command is not None and spec.source is not None
    req.run_cmd(list(spec.command), req.root)
    source = req.root / spec.source
    apps = sorted(p for p in source.rglob("*.app") if p.is_dir())
    dmgs = sorted(p for p in source.rglob("*.dmg") if p.is_file())
    if len(apps) != 1 or len(dmgs) != 1:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] mac-app composition needs "
            f"exactly one coupled .app/.dmg pair under {source}; found "
            f"{len(apps)} .app and {len(dmgs)} .dmg"
        )
    app, dmg = apps[0], dmgs[0]
    req.out_dir.mkdir(parents=True, exist_ok=True)
    app_dest = req.out_dir / app.name
    if app_dest.exists():
        # A re-run replaces the staged .app whole — copytree-merge over a
        # stale tree could carry files the fresh bundle no longer has.
        shutil.rmtree(app_dest)
    shutil.copytree(app, app_dest, symlinks=True)
    shutil.copy2(dmg, req.out_dir / dmg.name)
    payload = f"{req.artifact.name}.unsigned-app.tar.gz"
    req.run_cmd(
        ["tar", "-czf", str(req.out_dir / payload), "-C", str(app.parent), app.name],
        req.root,
    )
    if not (req.out_dir / payload).is_file():
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] mac bundle emitted no reseal "
            f"payload ({payload}) — the signer reseals the .dmg from the "
            f"SIGNED .app (workflows.lex §3.1), so a mac bundle without it is "
            f"a bundle-stage failure"
        )
    return Composed(req.artifact.name, "mac-app", (app.name, dmg.name, payload))


@dataclass(frozen=True)
class Composition:
    """One registry entry: a composition name, the compose function it runs,
    and the target platforms it applies to.

    ``platforms`` is a tuple of target-triple substrings; empty means every
    platform (archive, wheel). ``declared_command`` marks the compositions
    whose producing command is DECLARED on the artifact (mac-app's bundler)
    rather than registry-assembled — the config boundary validates the
    declaration shape against it (:func:`shipit.config._parse_bundle`).
    """

    name: str
    compose: Callable[[ComposeRequest], Composed]
    platforms: tuple[str, ...] = ()
    declared_command: bool = False

    def applies(self, target: str) -> bool:
        """Whether this composition runs for ``target`` (substring match on
        the triple; no declared platforms = every platform). Pure."""
        return not self.platforms or any(p in target for p in self.platforms)


ARCHIVE = Composition("archive", _compose_archive)
DEB = Composition("deb", _compose_deb, platforms=("linux",))
WHEEL = Composition("wheel", _compose_wheel)
MAC_APP = Composition(
    "mac-app", _compose_mac_app, platforms=("apple-darwin",), declared_command=True
)

#: The CLOSED registry, in a stable order. Adding a composition is adding an
#: entry here (the toolchain registry's mirror) — never a kind switch.
COMPOSITIONS: tuple[Composition, ...] = (ARCHIVE, DEB, WHEEL, MAC_APP)


def names() -> tuple[str, ...]:
    """The registered composition names, in registry order — for the config
    boundary's validation message (:func:`shipit.config._parse_bundle`)."""
    return tuple(c.name for c in COMPOSITIONS)


def composition(name: str) -> Composition | None:
    """The registry entry named ``name``, or ``None`` when unregistered.

    The config loader turns ``None`` into a
    :class:`~shipit.config.ConfigError` naming the known set; the bundle verb
    reaches this only through already-validated declarations.
    """
    for comp in COMPOSITIONS:
        if comp.name == name:
            return comp
    return None


#: (system, machine) → target triple, both lowercased: the host-derived
#: default when `shipit release bundle` gets no --target. Deliberately small —
#: the platforms the legacy matrices actually built; anything else must name
#: its triple explicitly.
_HOST_TARGETS: dict[tuple[str, str], str] = {
    ("darwin", "arm64"): "aarch64-apple-darwin",
    ("darwin", "x86_64"): "x86_64-apple-darwin",
    ("linux", "x86_64"): "x86_64-unknown-linux-gnu",
    ("linux", "amd64"): "x86_64-unknown-linux-gnu",
    ("linux", "aarch64"): "aarch64-unknown-linux-gnu",
    ("linux", "arm64"): "aarch64-unknown-linux-gnu",
    ("windows", "amd64"): "x86_64-pc-windows-msvc",
    ("windows", "x86_64"): "x86_64-pc-windows-msvc",
    ("windows", "arm64"): "aarch64-pc-windows-msvc",
}


def host_target(system: str, machine: str) -> str | None:
    """The target triple for a ``(platform.system(), platform.machine())``
    pair, or ``None`` when unmapped (the verb refuses and asks for
    ``--target``). Pure; case-insensitive."""
    return _HOST_TARGETS.get((system.lower(), machine.lower()))
