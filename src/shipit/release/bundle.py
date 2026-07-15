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
- **wasm-pack** — the wasm/npm leg (TOL02-WS12 #788, WS10 DECIDED #798:
  bespoke ``wasm-pack`` composition, pixi provisions ``wasm-pack`` + the
  wasm32 target, the npm tarball is the artifact). ``wasm-pack build`` the
  artifact's OWN wasm crate — resolved from the artifact's declared rust
  ``build`` target ``package`` (issue #904: mirrors the cargo/deb legs' ``-p``
  package), located via ``cargo metadata``'s ``manifest_path`` so a workspace
  whose ``[toolchains]`` rust path is the root (``"." = "rust"``) still runs
  wasm-pack in the crate dir, not against the ``[workspace]``-only root
  ``Cargo.toml``, and a repo with MULTIPLE wasm crates addresses each one; a
  wasm artifact declaring no package keeps the ``[toolchains]`` rust path
  (single-crate/root repos unchanged) — into a fresh ``pkg/`` npm package tree
  (wasm + JS glue +
  ``package.json``, the version wasm-pack reads from the crate's ``Cargo.toml``
  — bumped by ``release prepare``), then ``npm pack --ignore-scripts`` that
  tree into the ONE ``<pkg>-<version>.tgz`` npm tarball staged under the bundle
  output tree (``--ignore-scripts`` forecloses a package lifecycle script —
  ``prepare``/``prepack``/``postpack`` — running arbitrary code as a SECOND
  build path during bundle, the same guarantee the npm publish leg makes on the
  prebuilt tarball, ``release/publish.py``).
  That tarball is BOTH the gh-release asset and exactly what the npm endpoint
  publishes (``release/publish.py`` — no rebuild), and the assert-bundle npm
  tier reads its inner ``package.json`` ``name`` as the assertable identity.
  The optional ``scope`` / ``wasm-target`` declarations are the only
  consumer-specific parts (``@scope`` and wasm-pack's ``--target``, default
  ``bundler``); every other flag is registry-assembled. The scratch ``pkg/``
  tree is always removed — only the tarball survives (ADR-0009's barrier: a
  composition writes only its declared artifact under ``out_dir``).
- **vsix** — a per-target VS Code extension ``.vsix`` via ``npm exec -- vsce
  package --target <vsce-target> --out <name>-<vsce-target>.vsix`` (the legacy
  ``vscode-ext.yml@v3`` per-platform packaging: one ``.vsix`` per platform,
  each carrying that platform's prebuilt native binary). The declared platform
  triple picks the vsce target string (:data:`VSCE_TARGETS`; darwin-arm64 /
  darwin-x64 / linux-x64 / linux-arm64 / alpine-x64 / win32-x64), a triple with
  no vsce target being a loud refusal. Runs ``vsce`` through ``npm exec`` in the
  ``npm`` leg (the extension is a node package) — vsce is the consumer's
  ``@vscode/vsce`` devDependency under ``node_modules/.bin``, never a
  fleet-provisioned PATH binary, so the local package context resolves it. A
  ``.vsix`` is a zip package with no reopenable main binary, so — like wheel,
  wasm-pack, and tarball — it is NOT binary-asserting (``asserts_binary=False``,
  the scar-#2 guard is skipped). The per-target native binary the extension
  bundles (the ``lexd-lsp`` LSP, tree-sitter wasm) rides the **Artifact channel**
  (ARF01, ADR-0064/0066): it is published by its producing repo as a
  per-platform **conda package** and consumed here through an ``[artifact-deps]``
  pin, which ``shipit install`` projects into the managed pixi env so pixi
  resolves/fetches THIS leg's platform build into
  ``<root>/.pixi/envs/<env>/bin/<pkg>``. The vsix artifact names which pins to
  stage and where in the extension layout via the optional ``bundle.stage`` map
  (``{ "lexd-lsp" = "resources/lexd-lsp" }``); :func:`_stage_vsix_natives`
  COPIES each materialized binary into the leg dir before ``vsce package`` (never
  a fetch — issue #911's bespoke fetcher is superseded by the channel; a binary
  absent from the env hard-fails pointing at ``shipit install``), ``.exe``-suffixed
  on a windows target so the extension spawns a runnable binary, then REMOVES the
  staged copies after packaging (transient inputs vsce already zipped in — the
  leg dir is left clean, no per-target binary accumulating). Because pixi already
  resolved the per-platform subdir, the SOURCE binary at the path IS the right
  one for the target — no cross-target branching on the source.
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
- **tauri** — the tauri app bundler as a DECLARED-command composition, the
  mac-app shape (TOL02-WS15 #791, WS10 DECIDED #798: bespoke composition,
  "same naked-.app/.dmg sign-path argument as electron"). ONE declared
  ``tauri build`` (the consumer's own — e.g. ``npm run tauri build`` against
  the consumer's ``@tauri-apps/cli``, the one consumer-specific part, exactly
  like mac-app/electron-builder's declared bundler) leaves the platform's
  bundles under the declared ``source`` dir, and the composition collects
  whatever that platform produces. Because the bundler is DECLARED (not a
  registry-assembled argv), the tauri CLI is CONSUMER-OWNED — the consumer's
  manifest provisions it, NOT a shipit-managed pixi block: a declared command
  is the consumer's to provision, so tauri never becomes a release Exec tool
  and takes no provisioning-guard row (only registry-ASSEMBLED builders like
  wasm-pack ride pixi provisioning — docs/dev/release-tool-provisioning.md).
  Collection is NON-DESTRUCTIVE (it never deletes under the consumer's
  declared ``source`` — a config typo must never cost a file) and reads only
  the tool-CONTROLLED per-format subdirs tauri writes, never a name-blind
  recursive sweep of ``source``:

  - on **darwin** — the coupled ``.app``/``.dmg`` pair PLUS the reseal payload,
    the EXACT mac-app shape (the shared :func:`_stage_mac_pair`): the mac
    signer is consumer-agnostic and keys off the ``*.unsigned-app.tar.gz``
    payload, not the composition (workflows.lex §3.1 — "the only tauri-specific
    part is the bundler"), so a tauri darwin bundle rides the same sign path as
    electron with zero signer changes;
  - on **linux** — each format's ONE primary output from its subdir of the
    bundle dir (:data:`_TAURI_LINUX_FORMATS`: ``appimage/*.AppImage``,
    ``deb/*.deb``), staged into the output tree.

  Windows is out of scope (the legacy ``tauri-app.yml`` ships no
  ``icon.ico``, #791), so the composition is gated to darwin+linux targets and
  a windows leg is a clean skip, never a surprise. A darwin bundle missing its
  pair, or a linux bundle producing no ``.AppImage``/``.deb``, is a hard
  bundle-stage failure, never a quiet pass (ADR-0009's barrier); so is a
  format subdir holding more than one primary bundle (a stale prior output —
  never a nondeterministic pick).
- **electron** — electron-builder's per-platform distributable set (the
  darwin ``.dmg`` + ``.dmg.blockmap`` sidecar, the linux ``.AppImage``, the
  windows ``.exe`` + ``.exe.blockmap``), collected from the declared
  ``source`` output tree after the declared bundler runs. LIKE mac-app,
  electron is SIGNABLE through the standalone macOS sign stage: the bundler's
  own signing stays OFF, the darwin ``.app`` ships UNSIGNED and is re-emitted
  as the ``<name>.unsigned-app.tar.gz`` reseal payload the signer reopens →
  resigns → reseals → notarizes (ADR-0040, TOL02-WS14 #790). So a ``sign =
  true`` electron artifact derives its Apple creds through the standard
  sign-stage path, no build-time secret. The one nuance vs mac-app: electron's
  darwin ``.app`` NESTS helper ``.app`` bundles, so the TOP-LEVEL ``.app`` is
  the one staged. The reseal payload carries it across the artifact boundary
  (upload strips a ``.app``'s symlinks/exec bits); assert-bundle reads its
  ``CFBundleExecutable`` as the darwin anchor. Linux/windows legs ship unsigned.
  Mac/linux/windows targets only (the windows leg's integrity + endpoint land
  with WS11).
- **tarball** — the generated-parser ``<name>.tar.gz`` (TOL02-WS16 #792,
  legacy ``tree-sitter.yml@v3``): the tree-sitter leg's generated ``src/``
  tree plus the grammar/queries/bindings that are present. Platform-
  independent (generated C source, no per-OS variant — no ``-<target>``
  suffix), so every matrix leg composes the identical bytes.

Every external command runs through the injected runner — the one Exec seam
(ADR-0028); the ``cargo`` / ``uv`` / ``wasm-pack`` / ``npm`` / ``tar`` /
``zip`` / ``vsce`` argv literals below are those tools' one BUNDLE-side
assembly point, whitelisted in the
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

import json
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import config, execrun
from ..install import artifactdeps
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

    ``artifact_deps`` is the repo's parsed ``[artifact-deps]`` set
    (:func:`shipit.config.load_artifact_deps`) — the cross-repo Artifact-channel
    pins the vsix composition stages a native binary from (TOL03-WS03 #974): the
    compose resolves a ``bundle.stage`` package key against it to find the pin's
    pixi env and copy the materialized binary into the extension layout. ``()``
    for a repo declaring none (every composition but vsix ignores it).
    """

    artifact: config.Artifact
    entries: tuple[config.ToolchainEntry, ...]
    root: Path
    out_dir: Path
    target: str
    run_cmd: RunCmd
    build_target: str | None = None
    artifact_deps: tuple[config.ArtifactDep, ...] = ()


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


#: The generated-parser payload the ``tarball`` composition ships — tree-sitter's
#: conventional layout under the tree-sitter leg's path. ``src`` (the
#: ``tree-sitter generate`` output: ``parser.c``, the ``tree_sitter/``
#: headers, ``node-types.json``, ``grammar.json``) is the REQUIRED core — its
#: absence means the parser was never generated. The rest ride WHEN PRESENT
#: (the :data:`DOC_FILES` "when present" shape): a grammar declares queries
#: and bindings or it does not, and an absent one ships nothing rather than an
#: empty dir. This is the legacy ``tree-sitter.yml@v3`` tarball contract
#: (generated parser + grammar + queries), assembled shipit-side.
TREE_SITTER_PAYLOAD: tuple[str, ...] = (
    "src",
    "queries",
    "grammar.js",
    "package.json",
    "binding.gyp",
    "bindings",
)


def _compose_tarball(req: ComposeRequest) -> Composed:
    """The generated-parser tarball: ``<name>.tar.gz`` of the tree-sitter
    leg's :data:`TREE_SITTER_PAYLOAD` (the ``tree-sitter generate`` output
    plus the grammar/queries/bindings, when present). See the module
    docstring's tarball entry.

    A generated parser is platform-independent C source — no per-OS variant —
    so the archive carries NO ``-<target>`` suffix: every matrix leg that runs
    it composes the identical bytes under the one name (parity with legacy
    ``tree-sitter.tar.gz``). ``src`` is required — its absence is a bundle-
    stage failure (``shipit build`` runs ``tree-sitter generate`` first),
    never a quiet empty archive.
    """
    leg = _leg_for(req.artifact, req.entries, "tree-sitter", "tarball")
    leg_dir = req.root if leg.path in (".", "") else req.root / leg.path
    if not (leg_dir / "src").is_dir():
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] tarball composition: no generated "
            f"parser at {leg_dir / 'src'} — the tarball ships the "
            f"`tree-sitter generate` output; run `shipit build` first"
        )
    present = [name for name in TREE_SITTER_PAYLOAD if (leg_dir / name).exists()]
    archive = f"{req.artifact.name}.tar.gz"
    archive_path = req.out_dir / archive
    req.out_dir.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        # tar -czf truncates, but an unlink keeps the rerun's artifact exactly
        # the fresh tree (the archive/mac-app recreate-from-clean contract).
        archive_path.unlink()
    req.run_cmd(
        ["tar", "-czf", str(archive_path), "-C", str(leg_dir), *present],
        req.root,
    )
    return Composed(req.artifact.name, "tarball", (archive,))


#: wasm-pack's default output target when a wasm/npm artifact declares none —
#: the ``bundler`` target (webpack/rollup/vite consumers), wasm-pack's own
#: default. A consumer targeting ``web`` / ``nodejs`` / ``no-modules`` declares
#: it via ``bundle.wasm-target``.
WASM_PACK_DEFAULT_TARGET = "bundler"


def crate_dir_for_package(metadata: dict, package: str) -> Path | None:
    """The absolute crate directory of workspace ``package`` from parsed
    ``cargo metadata`` — the parent of its ``manifest_path`` (the crate's
    ``Cargo.toml``) — or ``None`` when no package of that name is present. Pure.

    A workspace's ``cargo metadata`` lists every member with an ABSOLUTE
    ``manifest_path``, so this resolves a declared package to its own crate dir
    regardless of where it sits in the tree (phos-core's top-level
    ``phos-color-wasm/``, lex's ``crates/lex-wasm/``), which is exactly the
    wasm-pack cwd (issue #904). It is pure over the parsed metadata so it
    unit-tests without a real cargo (the ``crates_publish_order`` pattern,
    :mod:`shipit.release.publish`)."""
    for pkg in metadata.get("packages", []):
        if pkg.get("name") == package:
            manifest = pkg.get("manifest_path")
            if manifest:
                return Path(manifest).parent
    return None


def _wasm_crate_dir(
    req: ComposeRequest, leg: config.ToolchainEntry, package: str | None
) -> Path:
    """The directory ``wasm-pack build`` runs in for this artifact.

    When the wasm artifact declares a rust ``build`` target ``package`` (issue
    #904), resolve THAT crate's dir via ``cargo metadata`` (the same
    ``manifest_path`` lookup the cargo/deb legs' ``-p`` honors): a workspace
    whose ``[toolchains]`` rust path is the root (``"." = "rust"``) would
    otherwise run wasm-pack against the ``[workspace]``-only root ``Cargo.toml``
    and hard-fail with ``missing field `package```, and a repo with multiple
    wasm crates could not address each one. When NO package is declared, keep
    the ``[toolchains]`` rust path (``req.root / leg.path``) so
    single-crate/root repos are unchanged. A declared package absent from the
    workspace metadata is a loud refusal, never a fall-back to the wrong dir."""
    if package is None:
        return req.root / leg.path
    metadata = req.run_cmd(
        ["cargo", "metadata", "--format-version", "1", "--no-deps"],
        req.root / leg.path,
    )
    if metadata is None:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] wasm-pack composition: `cargo "
            f"metadata` returned no result to resolve the declared rust build "
            f"package `{package}` against"
        )
    crate_dir = crate_dir_for_package(json.loads(metadata.stdout), package)
    if crate_dir is None:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] wasm-pack composition: the "
            f"declared rust build package `{package}` names no crate in the "
            f"`cargo metadata` workspace — nothing to run wasm-pack against"
        )
    return crate_dir


def _compose_wasm_pack(req: ComposeRequest) -> Composed:
    """``wasm-pack build`` the artifact's wasm crate → a ``pkg/`` npm tree, then
    ``npm pack`` it into the ONE npm tarball. See the module docstring's
    wasm-pack entry.

    The crate is the artifact's OWN wasm crate: the dir of its declared rust
    ``build`` target ``package`` resolved via ``cargo metadata``
    (:func:`_wasm_crate_dir` / :func:`crate_dir_for_package`, issue #904), or —
    when no package is declared — the FIRST mapped ``[toolchains]`` rust leg's
    path (the deb tier's rule, single-crate/root repos unchanged). Declaring
    the package is what lets a workspace whose ``[toolchains]`` rust path is the
    root (``"." = "rust"``) build the crate rather than the ``[workspace]``-only
    root ``Cargo.toml``, and a repo with multiple wasm crates address each one.
    ``wasm-pack build``
    writes a FRESH ``pkg/`` scratch tree under the output tree (wasm-pack
    itself clears ``--out-dir``); ``npm pack --ignore-scripts`` then produces
    the tarball, moved into ``out_dir`` (``--ignore-scripts`` keeps a generated
    ``package.json`` lifecycle script from running a second build path during
    bundle — the publish leg's ``--ignore-scripts`` guarantee, at the pack).
    The scratch ``pkg/`` is always removed — only the tarball
    is a declared artifact (ADR-0009's barrier). A build that leaves no
    ``package.json``, or a pack that yields no single ``.tgz``, is a hard
    bundle-stage failure, never a quiet pass.
    """
    leg = _leg_for(req.artifact, req.entries, "rust", "wasm-pack")
    spec = req.artifact.bundle
    assert spec is not None
    package = next(
        (t.package for t in req.artifact.build if t.toolchain == "rust" and t.package),
        None,
    )
    crate = _wasm_crate_dir(req, leg, package)
    target = spec.wasm_target or WASM_PACK_DEFAULT_TARGET
    req.out_dir.mkdir(parents=True, exist_ok=True)
    pkg = req.out_dir / f".pkg-{req.artifact.name}"
    if pkg.exists():
        # A rerun rebuilds pkg/ from scratch; wasm-pack clears its --out-dir,
        # but removing it here keeps a failed prior run from leaking a stale
        # tree into this one's npm pack.
        shutil.rmtree(pkg)
    argv = [
        "wasm-pack",
        "build",
        "--release",
        "--target",
        target,
        "--out-dir",
        str(pkg),
    ]
    if spec.scope is not None:
        argv += ["--scope", spec.scope]
    try:
        req.run_cmd(argv, crate)
        if not (pkg / "package.json").is_file():
            raise ReleaseError(
                f"[artifacts.{req.artifact.name}] wasm-pack composition: "
                f"`wasm-pack build` left no package.json under {pkg} — the npm "
                f"package tree is the artifact; a build that produces none is a "
                f"hard fail, never a quiet pass"
            )
        produced = _emit_into_out(
            req, ["npm", "pack", "--ignore-scripts"], "--pack-destination", pkg
        )
    finally:
        if pkg.exists():
            shutil.rmtree(pkg)
    tarballs = [name for name in produced if name.endswith(".tgz")]
    if len(tarballs) != 1:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] wasm-pack composition: `npm pack` "
            f"produced {len(tarballs)} .tgz under {req.out_dir} (expected exactly "
            f"one npm tarball — the artifact)"
        )
    return Composed(req.artifact.name, "wasm-pack", (tarballs[0],))


def _stage_mac_pair(req: ComposeRequest, source: Path, composition: str) -> Composed:
    """Stage the coupled unsigned ``.app``/``.dmg`` pair from ``source`` and
    re-emit the inner ``.app`` as ``<name>.unsigned-app.tar.gz`` — the
    symlink/exec-bit-preserving tar the signer reseals from (workflows.lex
    §3.1). A missing payload is a bundle-stage failure, never a signer
    surprise.

    Shared by the mac-app and tauri darwin compositions: both leave a single
    ``.app``/``.dmg`` pair a declared darwin bundler produced, and the mac
    signer is consumer-agnostic — it keys off the reseal payload, not the
    composition that made it (:func:`shipit.release.sign.detect_shape`). Zero
    or multiple pairs is a hard error (never a nondeterministic pick).

    Only the TOP-LEVEL ``.app`` is counted (:func:`_electron_top_level_apps`,
    reused rather than duplicated — #830): mac-app/tauri never nest a ``.app``
    today, but a bundler that ever nested one (electron's
    ``Contents/Frameworks/*Helper.app`` shape) would trip the exactly-one guard
    spuriously; filtering to the outer app keeps the shared helper robust as
    defense in depth.
    """
    apps = _electron_top_level_apps(source)
    dmgs = sorted(p for p in source.rglob("*.dmg") if p.is_file())
    if len(apps) != 1 or len(dmgs) != 1:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] {composition} composition needs "
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
            f"[artifacts.{req.artifact.name}] {composition} composition emitted "
            f"no reseal payload ({payload}) — the signer reseals the .dmg from "
            f"the SIGNED .app (workflows.lex §3.1), so a darwin bundle without "
            f"it is a bundle-stage failure"
        )
    return Composed(req.artifact.name, composition, (app.name, dmg.name, payload))


def _compose_mac_app(req: ComposeRequest) -> Composed:
    """The coupled unsigned ``.app``/``.dmg`` pair + the reseal payload.

    Runs the DECLARED bundler (the one consumer-specific part), then stages the
    exactly-one pair from the declared ``source`` dir via the shared
    :func:`_stage_mac_pair`. See the module docstring's mac-app entry.
    """
    spec = req.artifact.bundle
    assert spec is not None and spec.command is not None and spec.source is not None
    req.run_cmd(list(spec.command), req.root)
    return _stage_mac_pair(req, req.root / spec.source, "mac-app")


#: The tauri linux bundle layout the composition collects: each format subdir
#: ``tauri build`` writes under the declared bundle dir → the glob for its ONE
#: primary output (``bundle/appimage/*.AppImage``, ``bundle/deb/*.deb``). This
#: is the tool-CONTROLLED layout tauri owns and the release pipeline resolves
#: the same way (arthur-debert/release ``resolve-tauri-bundles.sh``). Collecting
#: from the named subdirs — never a recursive sweep of the consumer's declared
#: ``source`` — is what keeps a stray file elsewhere under ``source`` out of the
#: release. Windows is out of scope (the legacy ``tauri-app.yml`` ships no
#: ``icon.ico``, #791), so the composition is darwin+linux-gated and never
#: looks for a ``.msi``/``.exe``.
_TAURI_LINUX_FORMATS: tuple[tuple[str, str], ...] = (
    ("appimage", "*.AppImage"),
    ("deb", "*.deb"),
)


def _compose_tauri(req: ComposeRequest) -> Composed:
    """``tauri build`` the app, collect the current platform's bundles.

    Runs the DECLARED ``tauri build`` (the consumer's own bundler — the one
    consumer-specific part), then, on a darwin target, stages the coupled
    ``.app``/``.dmg`` pair + reseal payload (the shared :func:`_stage_mac_pair`
    — the same sign path as mac-app/electron); on a linux target collects each
    format's ONE primary output from its tool-controlled subdir of the declared
    bundle dir (:data:`_TAURI_LINUX_FORMATS`). See the module docstring's tauri
    entry. The composition is gated to darwin+linux (:data:`TAURI`), so a
    windows leg never reaches here.

    Collection is NON-DESTRUCTIVE — it never deletes under the consumer's
    declared ``source`` (a config typo must never cost a file). Instead of a
    name-blind recursive sweep, it reads only the named per-format subdirs and
    requires EXACTLY ONE primary file in each present one: a second, stale
    differently-named bundle a prior build left there is a HARD FAIL, never a
    silent stale-artifact release (the same "exactly one, never a
    nondeterministic pick" contract :func:`_stage_mac_pair` holds for the
    darwin pair). A rerun overwrites its one file in place, so it stays one.
    """
    spec = req.artifact.bundle
    assert spec is not None and spec.command is not None and spec.source is not None
    req.run_cmd(list(spec.command), req.root)
    source = req.root / spec.source
    if "apple-darwin" in req.target:
        return _stage_mac_pair(req, source, "tauri")
    req.out_dir.mkdir(parents=True, exist_ok=True)
    produced: list[str] = []
    for subdir, pattern in _TAURI_LINUX_FORMATS:
        fmt_dir = source / subdir
        if not fmt_dir.is_dir():
            continue  # a format the consumer's tauri.conf did not request
        matches = sorted(p for p in fmt_dir.glob(pattern) if p.is_file())
        if len(matches) > 1:
            raise ReleaseError(
                f"[artifacts.{req.artifact.name}] tauri composition: {fmt_dir} "
                f"holds {len(matches)} {pattern} files, expected exactly one — "
                f"a stale bundle from a prior build is still there; clean it "
                f"and rebuild (never a nondeterministic pick or a silent stale "
                f"release)"
            )
        for path in matches:
            dest = req.out_dir / path.name
            if dest.exists():
                dest.unlink()
            shutil.copy2(path, dest)
            produced.append(path.name)
    if not produced:
        globs = "/".join(pattern for _, pattern in _TAURI_LINUX_FORMATS)
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] tauri composition: `tauri build` "
            f"left no {globs} bundle under {source} — a linux tauri build that "
            f"produces none is a hard fail, never a quiet pass"
        )
    return Composed(req.artifact.name, "tauri", tuple(sorted(produced)))


#: The electron-builder distributable set per platform: a target-triple
#: substring, the PRIMARY distributable suffix (hard-required — a leg that
#: emits none is a bundle-stage failure, never a quiet pass), and the
#: companion suffixes shipped beside it WHEN PRESENT (electron-builder's
#: incremental-update ``.blockmap`` sidecars). electron-builder targets
#: exactly this OS set; the composition gates on ``target`` and reads only the
#: matching platform's set, so a darwin leg never scoops a stray linux
#: ``.AppImage`` a shared source tree might carry.
_ELECTRON_TARGETS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("apple-darwin", ".dmg", (".dmg.blockmap",)),
    ("linux", ".AppImage", (".AppImage.blockmap",)),
    ("windows", ".exe", (".exe.blockmap",)),
)


def _electron_target(target: str) -> tuple[str, tuple[str, ...]]:
    """The ``(primary_suffix, sidecar_suffixes)`` electron-builder emits for
    ``target`` — the first :data:`_ELECTRON_TARGETS` whose substring matches.
    Raises when no platform matches (the ``platforms`` gate keeps the verb
    from reaching here for one, but the compose function refuses loudly rather
    than compose an empty set). Pure."""
    for needle, primary, sidecars in _ELECTRON_TARGETS:
        if needle in target:
            return primary, sidecars
    raise ReleaseError(
        f"electron composition: target `{target}` is not a darwin/linux/"
        f"windows triple — electron-builder emits no distributable for it"
    )


def _compose_electron(req: ComposeRequest) -> Composed:
    """electron-builder's declared bundle → the platform distributable set.

    Runs the DECLARED bundler (``electron-builder`` — the one consumer-
    specific part, like mac-app's) and collects the platform-appropriate
    DISTRIBUTABLES from the declared ``source`` output tree: the darwin
    ``.dmg`` (plus its ``.dmg.blockmap`` sidecar), the linux ``.AppImage``, or
    the windows ``.exe`` — each PRIMARY distributable hard-required (a leg
    producing none is a bundle-stage failure), its ``.blockmap`` sidecar
    shipped when present. The darwin leg additionally requires EXACTLY ONE
    ``.dmg`` (the signer reseals one, from the signed ``.app`` — several is a
    stale/multi-arch leftover, failed here not at the signer); the linux/
    windows sets stay open (no reseal step gates their count).

    Like mac-app, electron is SIGNABLE through the standalone macOS sign stage
    (:mod:`shipit.release.sign`, ADR-0040): electron-builder does NOT sign at
    build (its own CSC/notarize stay OFF), the darwin ``.app`` ships UNSIGNED,
    and the composition re-emits it as the ``<name>.unsigned-app.tar.gz``
    reseal payload the signer reopens — the same bundle(unsigned) →
    sign-reopens-and-reseals model as a tauri ``.app`` (workflows.lex §3.1).
    The signer codesigns the ``.app`` inner-first and reseals the ``.dmg`` from
    it. The ONE electron nuance vs mac-app: the darwin ``.app`` NESTS helper
    ``.app`` bundles (the Electron Framework, the GPU/Renderer/Plugin helpers)
    under ``Contents/Frameworks``, so the composition selects the TOP-LEVEL
    ``.app`` (:func:`_stage_electron_reseal_payload`) rather than mac-app's
    sole-``.app`` assumption; the reseal payload carries the whole tree and the
    signer's inner-first walk (:func:`shipit.release.sign.nested_signable`)
    reaches the helpers.

    The naked ``.app`` rides the bundle tree, but the reseal payload is what
    crosses the artifact boundary (upload strips a ``.app``'s symlinks/exec
    bits), so assert-bundle reads the payload's ``CFBundleExecutable`` as the
    darwin main-binary anchor (:mod:`shipit.release.integrity`), the opaque
    ``.dmg``/``.AppImage`` NAME tiers being the fallback. Linux/windows legs
    ship unsigned (not signable). Mac/linux/windows targets only; the windows
    leg's integrity + endpoint land with WS11 (issue #790 acceptance).
    """
    spec = req.artifact.bundle
    assert spec is not None and spec.command is not None and spec.source is not None
    req.run_cmd(list(spec.command), req.root)
    source = req.root / spec.source
    if source.resolve() == req.out_dir.resolve():
        # The composition copies the bundler's distributables OUT of `source`
        # INTO the bundle tree, so the two must differ — a `source` that
        # resolves to the output dir would copy a file onto itself (a cryptic
        # shutil.SameFileError); refuse it up front with the fix.
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] electron composition: bundle "
            f"`source` ({spec.source}) resolves to the bundle output tree — "
            f"point `source` at electron-builder's own output dir, distinct "
            f"from the bundle tree the composition copies its distributables into"
        )
    primary, sidecars = _electron_target(req.target)
    dists = sorted(p for p in source.rglob(f"*{primary}") if p.is_file())
    if not dists:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] electron composition: the "
            f"bundler produced no {primary} under {source} — hard fail, never "
            f"a quiet pass (an electron leg must emit its distributable)"
        )
    # The darwin .dmg is what the standalone signer reseals from the signed
    # .app (workflows.lex §3.1) — the signer reseals exactly one, so a darwin
    # tree with several .dmg (a stale or multi-arch leftover in a shared source
    # tree) is an ambiguity resolved loudly HERE, never a signer surprise — the
    # same exactly-one contract mac-app enforces on its coupled pair. Linux
    # .AppImage / windows .exe carry no such reseal step, so their set is open.
    if "apple-darwin" in req.target and len(dists) != 1:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] electron composition: the darwin "
            f"leg needs exactly one {primary} to reseal under {source}; found "
            f"{len(dists)} — electron-builder emits one per arch lane, so several "
            f"is a stale/multi-arch leftover, resolved here, never at the signer"
        )
    req.out_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[str] = []
    for dist in dists:
        dest = req.out_dir / dist.name
        if dest.exists():
            dest.unlink()
        shutil.copy2(dist, dest)
        outputs.append(dist.name)
    # The .blockmap sidecars (electron-builder's incremental-update maps), each
    # collected ONLY when its primary distributable was: a `<primary>.blockmap`
    # whose `<primary>` is not among the copied distributables is a stale
    # leftover in the source tree, never scooped into the output set (copilot).
    collected = set(outputs)
    for sidecar in sidecars:
        for side in sorted(source.rglob(f"*{sidecar}")):
            if side.is_file() and side.name.removesuffix(".blockmap") in collected:
                dest = req.out_dir / side.name
                if dest.exists():
                    dest.unlink()
                shutil.copy2(side, dest)
                outputs.append(side.name)
    # The darwin leg additionally stages the unsigned .app + reseal payload the
    # standalone mac signer reopens (electron routes through the sign stage, it
    # does not self-sign). Linux/windows legs ship the distributable alone.
    if "apple-darwin" in req.target:
        outputs.extend(_stage_electron_reseal_payload(req, source))
    return Composed(req.artifact.name, "electron", tuple(sorted(outputs)))


def _electron_top_level_apps(source: Path) -> list[Path]:
    """The TOP-LEVEL ``.app`` bundles under ``source`` — a ``.app`` not itself
    nested inside another ``.app``. electron-builder's darwin output nests
    helper ``.app`` bundles (the GPU/Renderer/Plugin helpers) under a main
    app's ``Contents/Frameworks``, so a bare ``rglob('*.app')`` (mac-app's
    sole-app assumption) would scoop every helper; this keeps only the outer
    app the signer reopens. Pure."""
    return [
        p
        for p in sorted(source.rglob("*.app"))
        if p.is_dir()
        and not any(part.endswith(".app") for part in p.relative_to(source).parts[:-1])
    ]


def _stage_electron_reseal_payload(req: ComposeRequest, source: Path) -> list[str]:
    """Stage the darwin ``.app`` + the ``<name>.unsigned-app.tar.gz`` reseal
    payload the standalone mac signer reopens (:mod:`shipit.release.sign`).

    electron ships its darwin ``.app`` UNSIGNED through the ADR-0040 sign seam
    (like mac-app): copy the ``.app`` into the bundle tree (symlinks preserved)
    and re-emit it as the tar the signer reopens — artifact upload strips a
    ``.app``'s symlinks/exec bits, so the payload, not the raw ``.app``, is
    what crosses jobs. EXACTLY one top-level ``.app`` is required
    (:func:`_electron_top_level_apps`); a missing or ambiguous one is a
    bundle-stage failure (the bundler must leave the naked ``.app`` with its
    own signing OFF), never a signer surprise.
    """
    apps = _electron_top_level_apps(source)
    if len(apps) != 1:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] electron composition: the darwin "
            f"leg needs exactly one top-level .app to sign under {source}; found "
            f"{len(apps)} — electron-builder must leave the naked .app (its own "
            f"signing OFF) so the standalone signer reopens it"
        )
    app = apps[0]
    app_dest = req.out_dir / app.name
    if app_dest.exists():
        shutil.rmtree(app_dest)
    shutil.copytree(app, app_dest, symlinks=True)
    payload = f"{req.artifact.name}.unsigned-app.tar.gz"
    req.run_cmd(
        ["tar", "-czf", str(req.out_dir / payload), "-C", str(app.parent), app.name],
        req.root,
    )
    if not (req.out_dir / payload).is_file():
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] electron darwin leg emitted no "
            f"reseal payload ({payload}) — the signer reopens the unsigned .app "
            f"from it (workflows.lex §3.1), so its absence is a bundle-stage "
            f"failure"
        )
    return [app.name, payload]


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


def _staged_dest(leg_dir: Path, dest: str, *, windows: bool) -> Path:
    """The on-disk path a ``bundle.stage`` entry copies its binary to, under the
    extension leg dir — target-aware in the SAME way the source path is.

    On a windows target the executable carries a ``.exe`` suffix (conda's
    ``Scripts/<pkg>.exe`` PATH layout, mirrored on the destination so the
    extension spawns a runnable ``.exe`` — a bare ``lexd-lsp`` on windows is not
    executable). The consumer declares ONE ``dest`` for all platforms
    (``resources/lexd-lsp``); ``.exe`` is appended per-target here, never
    doubly (a ``dest`` already ending ``.exe`` is left as-is). Pure."""
    if windows and not dest.lower().endswith(".exe"):
        dest = f"{dest}.exe"
    return leg_dir / dest


def _stage_vsix_natives(req: ComposeRequest, leg_dir: Path) -> list[Path]:
    """Copy each declared ``bundle.stage`` native binary into the extension
    layout BEFORE ``vsce package`` (TOL03-WS03 #974) — the Artifact-channel
    staging that turns a hollow ``.vsix`` into a real extension. Returns the
    staged destination paths so the caller can remove them after packaging (they
    are transient build inputs, never left in the extension source tree).

    For each ``(package, dest)`` the vsix artifact declares
    (:attr:`shipit.config.BundleSpec.stage`), the ``package`` is resolved against
    the repo's parsed ``[artifact-deps]`` (``req.artifact_deps``): a key naming no
    declared pin is a loud refusal (staging a binary the channel was never told
    to publish is a config mistake, not a silent skip). The pin's materialized
    binary — ``<root>/.pixi/envs/<env>/bin/<package>`` on unix,
    ``…/Scripts/<package>.exe`` on a ``win32-x64`` leg (the target-aware conda
    PATH layout, :func:`shipit.install.artifactdeps.materialized_bin_path`), put
    there by ``shipit install`` projecting the pin so pixi resolves/fetches the
    RIGHT per-platform conda package (ADR-0064/0066) — is copied to
    ``<leg_dir>/<dest>`` (``.exe``-suffixed on windows, executable bit preserved),
    inside the extension package so ``vsce package`` includes it.

    This is a COPY off the already-materialized env, never a fetch: if the binary
    is absent, the composition hard-fails pointing at ``shipit install`` and the
    Artifact channel rather than reaching across repos itself (issue #911's
    bespoke fetcher is deliberately NOT reintroduced — the channel already put
    the binary in the build env). Per-target correctness needs no branching on the
    SOURCE: pixi resolved this leg's own platform subdir, so the binary at the
    path IS the right one for ``req.target``.
    """
    stage = req.artifact.bundle.stage if req.artifact.bundle is not None else ()
    if not stage:
        return []
    windows = _is_windows(req.target)
    deps = {dep.package: dep for dep in req.artifact_deps}
    staged: list[Path] = []
    for package, dest in stage:
        dep = deps.get(package)
        if dep is None:
            declared = ", ".join(sorted(deps)) or "none declared"
            raise ReleaseError(
                f"[artifacts.{req.artifact.name}] vsix stage names `{package}`, "
                f"but no [artifact-deps.{package}] pin is declared — the native "
                f"binary rides the Artifact channel as a conda package "
                f"(ADR-0064); declare the pin so `shipit install` materializes "
                f"it, never a bespoke fetch (declared artifact-deps: {declared})"
            )
        src = artifactdeps.materialized_bin_path(req.root, dep, target=req.target)
        if not src.is_file():
            raise ReleaseError(
                f"[artifacts.{req.artifact.name}] vsix stage: the `{package}` "
                f"binary is not materialized at {src} — run `shipit install` so "
                f"the Artifact channel projects the `{dep.repo}` pin into the "
                f"pixi env (ADR-0064/0066); the vsix bundle STAGES the binary, it "
                f"never fetches it"
            )
        dst = _staged_dest(leg_dir, dest, windows=windows)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        # Keep the exec bit: the LSP the extension spawns must stay runnable
        # (copy2 preserves mode, but a channel binary staged read-only would
        # ship a non-executable LSP — make the intent explicit). A no-op on
        # windows, where the `.exe` suffix (not a mode bit) is what runs.
        dst.chmod(dst.stat().st_mode | 0o755)
        staged.append(dst)
    return staged


def _compose_vsix(req: ComposeRequest) -> Composed:
    """Package the per-target ``.vsix`` via ``npm exec -- vsce package
    --target``, after staging any declared native binaries. See the module
    docstring's vsix entry.

    Runs ``vsce`` through ``npm exec`` in the ``npm`` leg (the extension
    package): vsce is the consumer's ``@vscode/vsce`` devDependency under
    ``node_modules/.bin``, so ``npm exec`` resolves it from the local package
    context — a bare ``vsce`` would not be on ``PATH`` under ``pixi run
    ./bin/shipit``. Writes the single ``<name>-<vsce-target>.vsix`` straight
    into the bundle output tree; the ``vsce`` output path is stated so a rerun
    overwrites in place (vsce replaces, never appends). The per-target native
    binary the extension bundles (the ``lexd-lsp`` LSP, tree-sitter wasm) is
    staged FIRST (:func:`_stage_vsix_natives`) by copying it out of the pixi env
    the Artifact channel materialized it into (``[artifact-deps]`` conda packages,
    TOL03-WS03 #974) — so the ``.vsix`` ships a real extension, not a hollow one.
    The staged binaries are TRANSIENT build inputs: ``vsce`` copies them into the
    ``.vsix`` zip, then this function removes them from the extension source tree
    (a ``finally``, so a failing pack cleans up too) — the composition leaves only
    its declared ``.vsix`` under ``out_dir``, never a stray per-target binary
    (a unix build and a win ``.exe`` accumulating in the shared leg dir).
    A run that leaves no ``.vsix`` is a hard failure, never a quiet pass (the
    legacy ``vscode-ext.yml@v3`` per-target contract).
    """
    leg = _leg_for(req.artifact, req.entries, "npm", "vsix")
    vt = vsce_target(req.target)
    leg_dir = req.root / leg.path
    staged = _stage_vsix_natives(req, leg_dir)
    out_name = f"{req.artifact.name}-{vt}.vsix"
    req.out_dir.mkdir(parents=True, exist_ok=True)
    out_path = req.out_dir / out_name
    if out_path.exists():
        out_path.unlink()
    try:
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
            leg_dir,
        )
        if not out_path.is_file():
            raise ReleaseError(
                f"[artifacts.{req.artifact.name}] vsix composition: vsce package "
                f"completed but produced no {out_name} under {req.out_dir} — hard "
                f"fail, never a quiet pass (legacy vscode-ext per-target contract)"
            )
    finally:
        # The staged natives are transient inputs vsce already copied into the
        # .vsix — remove them so the extension source tree is left as we found it
        # (no per-target binary lingering in the shared leg dir), success or not.
        for path in staged:
            path.unlink(missing_ok=True)
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
    never route to a signer leg that does not exist. ``asserts_binary`` marks
    the compositions whose output carries a MAIN BINARY the scar-#2 integrity
    guard checks (:mod:`shipit.release.integrity`, workflows.lex §3.2) —
    archive/deb/mac-app. A source/package composition (wheel's sdist+wheel,
    tarball's generated C, wasm-pack's npm tgz) has no binary to name, so the
    preflight planner omits the ``assert-bundle`` stage for it
    (:func:`shipit.release.preflight.plan`): running the guard over a source
    ``.tar.gz`` would hard-fail with "no main binary" (the deb tier's #784-F4
    lesson, inverted — nothing to assert).
    ``platform_independent`` marks the compositions whose output carries NO
    ``-<target>`` qualifier — the tarball's generated C source is identical on
    every OS, so it emits one unqualified ``<name>.tar.gz`` (parity with legacy
    ``tree-sitter.tar.gz``); the wasm-pack npm ``<name>-<version>.tgz`` is the
    sibling case (#828) — ``npm pack`` version-qualifies the filename but never
    target-qualifies it. Because ``wf-publish.yml``
    merges every leg's
    ``dist/`` into one flat tree (``merge-multiple``), a name without the
    ``-<target>`` qualifier
    built on more than one leg would COLLIDE (last writer wins, and tar bytes
    are not guaranteed identical across runners — mtimes/uid/gid), so the
    config boundary refuses such a composition declared with >1 ``platforms``
    (:func:`shipit.config._parse_artifact`): it must build on exactly one leg.
    ``option_keys`` are the
    EXTRA optional declaration keys a registry-assembled composition accepts
    (wasm-pack's ``scope`` / ``wasm-target`` — the ``@scope`` and wasm-pack
    ``--target``; vsix's ``stage`` — the Artifact-channel native-binary staging
    map, #974 — each composition's only consumer-specific parts); the config
    boundary accepts them ONLY for the composition that names them and rejects
    them everywhere else (:func:`shipit.config._parse_bundle`). ``provisions_signal``
    names a toolchain SIGNAL a declared composition needs beyond its own leg —
    wasm-pack's ``npm pack`` needs the node runtime (``npm`` rides ``nodejs``),
    but wasm-pack rides the RUST signal and a rust-only wasm crate's npm
    ``package.json`` is GENERATED into ``pkg/``, never tracked, so the node
    manifest signal is absent (issue #788 review). ``shipit install`` unions
    this signal into the detected toolchains off the declared composition
    (:func:`shipit.verbs.install._declared_signals`), delivering the
    node-deps block wherever the composition is declared; ``None`` (every
    composition but wasm-pack) adds nothing.
    """

    name: str
    compose: Callable[[ComposeRequest], Composed]
    platforms: tuple[str, ...] = ()
    declared_command: bool = False
    signable: bool = False
    asserts_binary: bool = True
    platform_independent: bool = False
    option_keys: tuple[str, ...] = ()
    provisions_signal: str | None = None

    def applies(self, target: str) -> bool:
        """Whether this composition runs for ``target`` (substring match on
        the triple; no declared platforms = every platform). Pure."""
        return not self.platforms or any(p in target for p in self.platforms)


ARCHIVE = Composition("archive", _compose_archive, signable=True)
DEB = Composition("deb", _compose_deb, platforms=("linux",))
#: wheel: a python sdist+wheel — no native binary, so the scar-#2 guard has
#: nothing to assert (its sdist IS a ``.tar.gz``, which the guard would
#: otherwise misread as a binary archive and hard-fail).
WHEEL = Composition("wheel", _compose_wheel, asserts_binary=False)
#: wasm-pack: an npm ``.tgz`` (wasm/JS package) — like the wheel sdist and the
#: tree-sitter tarball it carries no main binary, so the scar-#2 guard is
#: skipped for it (``asserts_binary=False``); a source package built via
#: ``npm pack`` has nothing for the integrity guard to assert. It is also
#: ``platform_independent`` (sibling to the tarball guard, #828): ``npm pack``
#: emits one version-qualified but NOT target-qualified ``<name>-<version>.tgz``
#: — the wasm/JS bytes carry no per-OS variant, so the config boundary refuses a
#: >1 ``platforms`` declaration (a name without the ``-<target>`` qualifier would
#: collide, last-writer-wins, in ``wf-publish.yml``'s merged ``dist/`` and tar
#: bytes are not
#: identical across runners); it must build on exactly one leg.
WASM_PACK = Composition(
    "wasm-pack",
    _compose_wasm_pack,
    asserts_binary=False,
    platform_independent=True,
    option_keys=("scope", "wasm-target"),
    # `npm pack` at bundle needs the node runtime (npm); wasm-pack rides the
    # rust signal and the crate's npm package.json is generated, never tracked,
    # so install unions the node signal off this declaration (issue #788).
    provisions_signal="node",
)
#: vsix: a per-target VS Code extension ``.vsix`` (a zip package) — no
#: reopenable main binary, so like wheel/wasm-pack/tarball the scar-#2 guard is
#: skipped (``asserts_binary=False``): preflight never routes the vsix leg
#: through assert-bundle, which would hard-fail "no main binary" on a tree
#: carrying only the per-target ``<name>-<vsce-target>.vsix``. Its optional
#: ``stage`` option key (#974) carries the Artifact-channel native-binary staging
#: map — the one vsix-specific declaration, gated to this composition.
VSIX = Composition(
    "vsix",
    _compose_vsix,
    platforms=("apple-darwin", "linux", "windows"),
    asserts_binary=False,
    # `stage`: the optional Artifact-channel native-binary staging map (#974) —
    # `{ "<artifact-dep pkg>" = "<dest-in-extension>" }`, the only vsix-specific
    # declaration. Accepted ONLY here (the config boundary rejects it on every
    # other composition via this option_keys gate), like wasm-pack's scope.
    option_keys=("stage",),
)
MAC_APP = Composition(
    "mac-app",
    _compose_mac_app,
    platforms=("apple-darwin",),
    declared_command=True,
    signable=True,
)
TAURI = Composition(
    "tauri",
    _compose_tauri,
    # darwin (.app/.dmg + reseal payload) AND linux (.AppImage/.deb); windows
    # is out of scope (no icon.ico, #791) so it is NOT listed — a windows leg
    # is a clean skip.
    platforms=("apple-darwin", "linux"),
    declared_command=True,
    # The darwin leg emits the same reseal payload as mac-app, so `sign = true`
    # over a tauri app routes to the mac signer's existing mac-app leg.
    signable=True,
)
ELECTRON = Composition(
    "electron",
    _compose_electron,
    platforms=("apple-darwin", "linux", "windows"),
    declared_command=True,
    # SIGNABLE, like mac-app: electron-builder does NOT sign at build; the
    # darwin `.app` ships unsigned as the `<name>.unsigned-app.tar.gz` reseal
    # payload, and the standalone mac sign stage (ADR-0040) reopens → resigns →
    # reseals → notarizes it. So a `sign = true` electron artifact derives the
    # Apple cert/notary requirement through the STANDARD sign-stage path — no
    # composition-keyed build-time secret (TOL02-WS14 #790).
    signable=True,
)
#: tree-sitter's generated-parser tarball (TOL02-WS16 #792) — platform-
#: independent (the same generated C source on every leg, emitted as one
#: unqualified ``<name>.tar.gz``), NOT signable (a source tarball has no binary
#: the mac signer reopens), and NOT binary-asserting (a source ``.tar.gz`` has
#: no main binary — the scar-#2 guard is skipped for it, like the wheel's
#: sdist). ``platform_independent`` makes the config boundary refuse it with
#: >1 ``platforms`` (the unqualified name would collide across legs).
TARBALL = Composition(
    "tarball", _compose_tarball, asserts_binary=False, platform_independent=True
)

#: The CLOSED registry, in a stable order. Adding a composition is adding an
#: entry here (the toolchain registry's mirror) — never a kind switch.
COMPOSITIONS: tuple[Composition, ...] = (
    ARCHIVE,
    DEB,
    WHEEL,
    WASM_PACK,
    VSIX,
    MAC_APP,
    TAURI,
    ELECTRON,
    TARBALL,
)


def names() -> tuple[str, ...]:
    """The registered composition names, in registry order — for the config
    boundary's validation message (:func:`shipit.config._parse_bundle`)."""
    return tuple(c.name for c in COMPOSITIONS)


def signable_names() -> tuple[str, ...]:
    """The composition names the mac signer can reopen, registry order — for
    the config boundary's ``sign = true`` refusal message
    (:func:`shipit.config._parse_artifact`)."""
    return tuple(c.name for c in COMPOSITIONS if c.signable)


def platform_independent_names() -> tuple[str, ...]:
    """The composition names whose output is unqualified (no ``-<target>``
    suffix), registry order — for the config boundary's >1-``platforms``
    refusal message (:func:`shipit.config._parse_artifact`). An unqualified
    archive built on more than one leg would collide in the merged ``dist/``."""
    return tuple(c.name for c in COMPOSITIONS if c.platform_independent)


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
