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
  ``.deb`` appears. cargo-deb is SELF-PROVISIONED (``cargo install
  cargo-deb --version <pin> --locked``, pinned for a reproducible build)
  when absent from PATH: it is not on conda-forge, so no pixi env can carry
  it, and the wf-build runner arrives without it (issue #784 F2). cargo-deb
  receives ``--target <triple>`` EXACTLY when the build was cross-compiled
  (``ComposeRequest.build_target`` set — TOL02-WS11): a native build writes
  ``target/release/`` and cargo-deb reads it with no ``--target``, a cross
  build writes ``target/<triple>/release/`` and cargo-deb is pointed there by
  the SAME triple (which also derives the Debian arch). The triple-dir
  contract's one owner is that threaded target — build, archive, and deb all
  read where the build actually wrote (issue #785 deferral, resolved by #787;
  :func:`shipit.tools.e2e.binary_location` shares the derivation). Linux
  targets only.
- **wheel** — ``uv build`` emitting BOTH the wheel and the sdist into the
  bundle output tree (the legacy ``python-pkg.yml`` build job: one build,
  consumed by multiple publish targets).
- **vsix** — a per-target VS Code extension ``.vsix`` via ``npm exec -- vsce
  package --target <vsce-target> --out <name>-<vsce-target>.vsix`` (the legacy
  ``vscode-ext.yml@v3`` per-platform packaging: one ``.vsix`` per platform,
  each carrying that platform's prebuilt native binary). The declared platform
  triple picks the vsce target string (:data:`VSCE_TARGETS`; darwin-arm64 /
  darwin-x64 / linux-x64 / linux-arm64 / alpine-x64 / win32-x64), a triple with
  no vsce target being a loud refusal. Runs ``vsce`` through ``npm exec`` in the
  ``npm`` leg (the extension is a node package) — vsce is the consumer's
  ``@vscode/vsce`` devDependency under ``node_modules/.bin``, never a
  fleet-provisioned PATH binary, so the local package context resolves it. The
  per-target binary it bundles is produced by the build
  stage — for ``win32-x64`` that binary comes from the cross-target build
  (TOL02-WS11 #787, the windows leg's stated dependency, not hidden), which
  writes ``target/<triple>/release/`` for the extension's prepackage step to
  stage.
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
(ADR-0028); the ``cargo`` / ``uv`` / ``tar`` / ``zip`` / ``vsce`` argv literals
below are those tools' one BUNDLE-side assembly point, whitelisted in the
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

#: The pinned cargo-deb version the deb composition self-provisions (issue
#: #784 F2). A floating ``cargo install cargo-deb`` resolves the latest crate
#: at compose time — irreproducible builds and a supply-chain window — so the
#: version is PINNED, the same shape as lexd's ``--tag``-pinned self-provision
#: (:mod:`shipit.provision.lexd`). Bump deliberately, in its own change.
CARGO_DEB_VERSION = "3.7.0"

#: Target triple → VS Code ``vsce``/``ovsx`` target string (the vsix
#: composition's per-platform ``--target``). vsce names platforms in its own
#: ``<os>-<arch>`` vocabulary — distinct from the rust triples the rest of the
#: release lane speaks — so the composition maps once, here (the four the issue
#: ships plus the two neighbours a rust triple already covers). A triple with
#: no entry is a loud refusal (:func:`vsce_target`): the vsix leg never guesses
#: a marketplace platform. windows-x86_64's binary rides the cross-target build
#: (TOL02-WS11 #787) — the win32-x64 leg's stated dependency.
VSCE_TARGETS: dict[str, str] = {
    "aarch64-apple-darwin": "darwin-arm64",
    "x86_64-apple-darwin": "darwin-x64",
    "x86_64-unknown-linux-gnu": "linux-x64",
    "aarch64-unknown-linux-gnu": "linux-arm64",
    "x86_64-unknown-linux-musl": "alpine-x64",
    "x86_64-pc-windows-msvc": "win32-x64",
}

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
    platform composed for — used for ``<name>-<target>`` naming, windows
    detection, and platform gating. ``build_target`` is the cross triple a
    ``shipit build --target <triple>`` redirected the build to (TOL02-WS11):
    when set, the built binary lives under ``target/<triple>/release/`` and the
    compositions that read a build output (archive, deb) look there; ``None``
    keeps the native ``target/release/`` (issue #784 F3's native contract).
    The bundle verb sets ``build_target`` from an EXPLICIT ``--target`` (the
    cross fan wf-build drives) and leaves it ``None`` for the host-derived
    default (a native local bundle) — so build and bundle agree on the dir by
    being handed the SAME triple, never a native/cross guess (the triple-dir
    contract's single owner, issue #785 deferral resolved by #787).
    """

    artifact: config.Artifact
    entries: tuple[config.ToolchainEntry, ...]
    root: Path
    out_dir: Path
    target: str
    run_cmd: RunCmd
    build_target: str | None = None


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
    loc = e2e_mod.binary_location(
        req.artifact, req.entries, consumer="bundle", target_triple=req.build_target
    )
    binary = req.root / loc.leg_path / (loc.relpath + (".exe" if windows else ""))
    if not binary.is_file():
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] archive composition: no built "
            f"binary at {binary} — bundle composes build outputs; run "
            f"`shipit build` first"
        )
    stem = f"{req.artifact.name}-{req.target}"
    stage = req.out_dir / stem
    if stage.exists():
        # A rerun rebuilds the staging subdir from scratch: reusing it
        # (exist_ok=True) would leave files a PRIOR build shipped but the
        # current one no longer has, and the archiver would re-pack them —
        # `zip -r` UPDATES an existing archive in place rather than replacing
        # it, so stale payload would survive into the artifact (mac-app
        # already replaces its staged tree for the same reason).
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    shutil.copy2(binary, stage / binary.name)
    for doc in DOC_FILES:
        doc_path = req.root / doc
        if doc_path.is_file():
            shutil.copy2(doc_path, stage / doc)
    archive = f"{stem}.zip" if windows else f"{stem}.tar.gz"
    archive_path = req.out_dir / archive
    if archive_path.exists():
        # `zip -r` merges into an existing archive; even for tar we recreate
        # from a clean slate so a rerun's artifact is exactly the fresh tree.
        archive_path.unlink()
    if windows:
        req.run_cmd(["zip", "-r", archive, stem], req.out_dir)
    else:
        req.run_cmd(["tar", "-czf", archive, stem], req.out_dir)
    return Composed(req.artifact.name, "archive", (archive, f"{stem}/"))


def _emit_into_out(
    req: ComposeRequest, argv: Sequence[str], out_flag: str, cwd: Path
) -> list[str]:
    """Run ``argv`` with ``out_flag`` pointing at a FRESH scratch dir under
    the output tree, then move whatever it wrote into ``out_dir`` (overwriting
    stale same-named files) and return the produced names, sorted.

    Isolating the tool's writes in a per-artifact scratch dir makes the
    produced set exactly what THIS run emitted: the old before/after
    subtraction over the shared ``out_dir`` misread a rerun that OVERWROTE an
    identically-named artifact (the common case — same version, same target)
    as "produced nothing" and hard-failed. The scratch dir is always removed,
    including on a composition-command failure.
    """
    req.out_dir.mkdir(parents=True, exist_ok=True)
    scratch = req.out_dir / f".tmp-{req.artifact.name}"
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    try:
        req.run_cmd([*argv, out_flag, str(scratch)], cwd)
        produced = sorted(p.name for p in scratch.iterdir())
        for name in produced:
            dest = req.out_dir / name
            if dest.exists():
                dest.unlink()
            shutil.move(str(scratch / name), str(dest))
    finally:
        if scratch.exists():
            shutil.rmtree(scratch)
    return produced


def _compose_deb(req: ComposeRequest) -> Composed:
    """cargo-deb over the pre-built release binary — no rebuild, no strip;
    a run that produces no ``.deb`` is a hard failure (legacy build-deb).
    Self-provisions cargo-deb (a pinned version) when missing. Passes
    ``--target <triple>`` ONLY on a cross build (``build_target`` set): cargo
    then reads ``target/<triple>/release/`` where ``shipit build --target``
    wrote the binary and derives the Debian arch from the triple; a native
    build passes no ``--target`` and cargo-deb reads ``target/release/`` and
    derives the arch from the host toolchain (TOL02-WS11). See the module
    docstring's deb entry."""
    leg = _leg_for(req.artifact, req.entries, "rust", "deb")
    package = next(
        (t.package for t in req.artifact.build if t.toolchain == "rust" and t.package),
        None,
    )
    if shutil.which("cargo-deb") is None:
        # Self-provision (issue #784 F2): cargo-deb is not on conda-forge, so
        # the consumer's pixi env cannot carry it, and the wf-build runner
        # arrives without it — the leg would otherwise fail by construction.
        # cargo itself is guaranteed present (it built the binary this deb
        # wraps); a failing install raises through run_cmd, aborting the
        # stage loudly (ADR-0009's barrier), never a quiet skip. The version
        # is pinned (CARGO_DEB_VERSION) for a reproducible build.
        #
        # No post-install PATH re-check: cargo resolves a custom subcommand
        # (`cargo deb`) by searching $CARGO_HOME/bin ITSELF, independent of the
        # process PATH, so the just-installed cargo-deb is found even in an
        # isolated env (pixi) where $CARGO_HOME/bin is off PATH — a shutil.which
        # gate here would spuriously abort exactly that case (issue #784 F2).
        req.run_cmd(
            [
                "cargo",
                "install",
                "cargo-deb",
                "--version",
                CARGO_DEB_VERSION,
                "--locked",
            ],
            req.root,
        )
    argv = ["cargo", "deb", "--no-build", "--no-strip"]
    if package is not None:
        argv += ["-p", package]
    if req.build_target is not None:
        # A cross build (`shipit build --target <triple>`) wrote the binary to
        # target/<triple>/release/, so cargo-deb must read the SAME dir —
        # `--target <triple>` points it there (and derives the Debian arch from
        # the triple). The one owner of the triple-dir contract is the target
        # threaded from build to here (issue #785 deferral, resolved by #787):
        # native builds pass no --target and cargo-deb reads target/release/.
        argv += ["--target", req.build_target]
    emitted = _emit_into_out(req, argv, "--output", req.root / leg.path)
    produced = [name for name in emitted if name.endswith(".deb")]
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
    produced = _emit_into_out(req, ["uv", "build"], "--out-dir", req.root / leg.path)
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


def vsce_target(target: str) -> str:
    """The VS Code marketplace target string for a rust target triple
    (:data:`VSCE_TARGETS`), or a loud :class:`ReleaseError` naming the mapped
    set. Pure. The vsix leg never packages an unmapped platform — a triple with
    no vsce target is a declaration the marketplace cannot ship."""
    vt = VSCE_TARGETS.get(target)
    if vt is None:
        known = ", ".join(sorted(VSCE_TARGETS))
        raise ReleaseError(
            f"vsix composition: target triple `{target}` has no VS Code "
            f"marketplace target — the mapped rust triples are: {known}"
        )
    return vt


def _compose_vsix(req: ComposeRequest) -> Composed:
    """Package the per-target ``.vsix`` via ``npm exec -- vsce package
    --target``. See the module docstring's vsix entry.

    Runs ``vsce`` through ``npm exec`` in the ``npm`` leg (the extension
    package): vsce is the consumer's ``@vscode/vsce`` devDependency under
    ``node_modules/.bin``, so ``npm exec`` resolves it from the local package
    context — a bare ``vsce`` would not be on ``PATH`` under ``pixi run
    ./bin/shipit``. Writes the single ``<name>-<vsce-target>.vsix`` straight
    into the bundle output tree; the ``vsce`` output path is stated so a rerun
    overwrites in place (vsce replaces, never appends). The native binary vsce
    bundles for this target is the build stage's output — for ``win32-x64`` the
    cross-target build's (TOL02-WS11 #787). A run that leaves no ``.vsix`` is a
    hard failure, never a quiet pass (the legacy ``vscode-ext.yml@v3``
    per-target contract).
    """
    leg = _leg_for(req.artifact, req.entries, "npm", "vsix")
    vt = vsce_target(req.target)
    out_name = f"{req.artifact.name}-{vt}.vsix"
    req.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = req.out_dir / out_name
    if out_path.exists():
        out_path.unlink()
    req.run_cmd(
        [
            "npm",
            "exec",
            "--",
            "vsce",
            "package",
            "--target",
            vt,
            "--out",
            str(out_path),
        ],
        req.root / leg.path,
    )
    if not out_path.is_file():
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] vsix composition: vsce package "
            f"completed but produced no {out_name} under {req.out_dir} — hard "
            f"fail, never a quiet pass (legacy vscode-ext per-target contract)"
        )
    return Composed(req.artifact.name, "vsix", (out_name,))


@dataclass(frozen=True)
class Composition:
    """One registry entry: a composition name, the compose function it runs,
    and the target platforms it applies to.

    ``platforms`` is a tuple of target-triple substrings; empty means every
    platform (archive, wheel). ``declared_command`` marks the compositions
    whose producing command is DECLARED on the artifact (mac-app's bundler)
    rather than registry-assembled — the config boundary validates the
    declaration shape against it (:func:`shipit.config._parse_bundle`).
    ``signable`` marks the compositions the mac signer can reopen
    (:mod:`shipit.release.sign` — the mac-app leg's reseal payload, the
    archive leg's tarball, TOL02-WS08 #779): the config boundary refuses
    ``sign = true`` on any other composition, so a sign declaration can
    never route to a signer leg that does not exist.
    """

    name: str
    compose: Callable[[ComposeRequest], Composed]
    platforms: tuple[str, ...] = ()
    declared_command: bool = False
    signable: bool = False

    def applies(self, target: str) -> bool:
        """Whether this composition runs for ``target`` (substring match on
        the triple; no declared platforms = every platform). Pure."""
        return not self.platforms or any(p in target for p in self.platforms)


ARCHIVE = Composition("archive", _compose_archive, signable=True)
DEB = Composition("deb", _compose_deb, platforms=("linux",))
WHEEL = Composition("wheel", _compose_wheel)
VSIX = Composition(
    "vsix",
    _compose_vsix,
    platforms=("apple-darwin", "linux", "windows"),
)
MAC_APP = Composition(
    "mac-app",
    _compose_mac_app,
    platforms=("apple-darwin",),
    declared_command=True,
    signable=True,
)

#: The CLOSED registry, in a stable order. Adding a composition is adding an
#: entry here (the toolchain registry's mirror) — never a kind switch.
COMPOSITIONS: tuple[Composition, ...] = (ARCHIVE, DEB, WHEEL, VSIX, MAC_APP)


def names() -> tuple[str, ...]:
    """The registered composition names, in registry order — for the config
    boundary's validation message (:func:`shipit.config._parse_bundle`)."""
    return tuple(c.name for c in COMPOSITIONS)


def signable_names() -> tuple[str, ...]:
    """The composition names the mac signer can reopen, registry order — for
    the config boundary's ``sign = true`` refusal message
    (:func:`shipit.config._parse_artifact`)."""
    return tuple(c.name for c in COMPOSITIONS if c.signable)


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
