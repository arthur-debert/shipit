"""The closed bundle-composition registry: build outputs -> unsigned Artifacts.

One entry per ``[artifacts.<name>].bundle.composition``; each writes only in ``out_dir``.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .. import config, execrun
from ..fspath import first_link_component
from ..install import artifactdeps
from ..tools import e2e as e2e_mod
from . import ReleaseError

#: Pinned: bump deliberately, in its own change.
CARGO_DEB_VERSION = "3.7.0"

#: Target triple → the vsix composition's ``--target``; vsce names platforms in
#: its own vocabulary.
VSCE_TARGETS: dict[str, str] = {
    "aarch64-apple-darwin": "darwin-arm64",
    "x86_64-apple-darwin": "darwin-x64",
    "x86_64-unknown-linux-gnu": "linux-x64",
    "aarch64-unknown-linux-gnu": "linux-arm64",
    "x86_64-unknown-linux-musl": "alpine-x64",
    "x86_64-pc-windows-msvc": "win32-x64",
}

#: Shipped beside the binary WHEN PRESENT.
DOC_FILES: tuple[str, ...] = (
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "LICENSE.md",
    "LICENSE.txt",
)

#: The runner seam a composition executes through; a failing command raises.
RunCmd = Callable[[Sequence[str], Path], execrun.ExecResult | None]


@dataclass(frozen=True)
class ComposeRequest:
    """Everything one composition needs; ``out_dir`` is the only place it may write."""

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
        return {
            "artifact": self.artifact,
            "composition": self.composition,
            "outputs": list(self.outputs),
        }


def _is_windows(target: str) -> bool:
    return "windows" in target


def _leg_for(
    artifact: config.Artifact,
    entries: Sequence[config.ToolchainEntry],
    toolchain: str,
    composition: str,
) -> config.ToolchainEntry:
    """The first ``[toolchains]`` leg of ``toolchain``, or a refusal naming the need."""
    leg = next((entry for entry in entries if entry.toolchain == toolchain), None)
    if leg is None:
        raise ReleaseError(
            f"[artifacts.{artifact.name}] {composition} composition needs a "
            f"[toolchains] {toolchain} leg, and none is mapped"
        )
    return leg


def _compose_archive(req: ComposeRequest) -> Composed:
    """A ``<name>-<target>/`` subdir (binary + docs), archived beside it."""
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
        # `zip -r` UPDATES an archive, so a reused subdir would let a prior
        # build's files survive into this artifact.
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
        archive_path.unlink()
    if windows:
        req.run_cmd(["zip", "-r", archive, stem], req.out_dir)
    else:
        req.run_cmd(["tar", "-czf", archive, stem], req.out_dir)
    return Composed(req.artifact.name, "archive", (archive, f"{stem}/"))


def _emit_into_out(
    req: ComposeRequest, argv: Sequence[str], out_flag: str, cwd: Path
) -> list[str]:
    """Run ``argv`` into a fresh scratch dir, move its output into ``out_dir``."""
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
    """cargo-deb over the pre-built release binary — no rebuild, no strip."""
    leg = _leg_for(req.artifact, req.entries, "rust", "deb")
    package = next(
        (t.package for t in req.artifact.build if t.toolchain == "rust" and t.package),
        None,
    )
    if shutil.which("cargo-deb") is None:
        # cargo-deb is not on conda-forge, so no pixi env can carry it. No PATH
        # re-check after: cargo finds a subcommand via $CARGO_HOME/bin itself.
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
        # A cross build wrote to target/<triple>/release/; a native one to
        # target/release/, which cargo-deb reads with no --target.
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
    """``uv build``; BOTH the wheel and the sdist must appear."""
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


def _payload_operands(
    artifact_name: str, spec: config.BundleSpec, root: Path, leg_rel: str
) -> tuple[Path, list[str]]:
    """The proven-real leg dir and the declared payload's present entries, in order.

    Both are producer-declared, so the refuse-links walk starts at the CHECKOUT
    ROOT and runs through the leg then each entry: a link anywhere is REFUSED,
    never followed — the escape is not in the spelling.
    """
    where = f"[artifacts.{artifact_name}] {spec.composition} composition"
    root_res = root.resolve()
    # The leg is an anchor, not yet a base: refuse a link in it before resolving.
    leg_parts = PurePosixPath(leg_rel).parts if leg_rel not in (".", "") else ()
    leg_link = first_link_component(root_res, leg_parts)
    if leg_link is not None:
        raise ReleaseError(
            f"{where}: the `{spec.leg}` leg path {leg_rel!r} traverses a symlink "
            f"or junction at {leg_link} — the bundle refuses to FOLLOW links out "
            f"of the checkout; a `[toolchains]` leg must be a real directory, so "
            f"a committed redirect can never make an out-of-tree dir the base the "
            f"payload is collected from"
        )
    leg_dir_res = root_res.joinpath(*leg_parts)
    if leg_dir_res != root_res and not leg_dir_res.is_relative_to(root_res):
        raise ReleaseError(
            f"{where}: the `{spec.leg}` leg {leg_dir_res} resolves outside the "
            f"checkout ({root_res}); a leg is a real directory inside the tree"
        )
    for entry in spec.payload:
        if config.path_escapes(entry.path):
            raise ReleaseError(
                f"{where}: payload path {entry.path!r} is not a leg-relative "
                f"path — no leading '/', no '\\', no drive letter, no '..' "
                f"segment; the payload lives inside the `{spec.leg}` leg"
            )
        parts = PurePosixPath(entry.path).parts
        offender = first_link_component(leg_dir_res, parts)
        if offender is not None:
            raise ReleaseError(
                f"{where}: payload path {entry.path!r} traverses a symlink or "
                f"junction at {offender} — the bundle refuses to FOLLOW links "
                f"out of the `{spec.leg}` leg; it archives only real files and "
                f"real directories, so a redirect can never steer the archive "
                f"at a file outside the checkout"
            )
        candidate = leg_dir_res.joinpath(*parts)
        if candidate == leg_dir_res or not candidate.is_relative_to(leg_dir_res):
            raise ReleaseError(
                f"{where}: payload path {entry.path!r} resolves to {candidate}, "
                f"outside the `{spec.leg}` leg ({leg_dir_res}); every payload "
                f"member is a strict descendant of its leg"
            )
    missing = [
        entry.path
        for entry in spec.payload
        if entry.required and not leg_dir_res.joinpath(entry.path).exists()
    ]
    if missing:
        raise ReleaseError(
            f"{where}: required payload missing under {leg_dir_res} — "
            f"{', '.join(missing)}; the bundle ships exactly the declared "
            f"`bundle.payload` (run `shipit build` first if these entries are build outputs), "
            f"never a quiet archive missing its required core"
        )
    present = [
        entry.path
        for entry in spec.payload
        if leg_dir_res.joinpath(entry.path).exists()
    ]
    return leg_dir_res, present


def _compose_declared_payload(req: ComposeRequest) -> Composed:
    """``<name>.tar.gz`` of the declared ``bundle.payload``, under its ``bundle.leg``."""
    spec = req.artifact.bundle
    if spec is None or spec.leg is None:
        raise ReleaseError(
            f"[artifacts.{req.artifact.name}] declared-payload composition "
            f"reached with no `bundle.leg`/`bundle.payload` declaration"
        )
    leg = _leg_for(req.artifact, req.entries, spec.leg, spec.composition)
    leg_dir, present = _payload_operands(req.artifact.name, spec, req.root, leg.path)
    archive = f"{req.artifact.name}.tar.gz"
    archive_path = req.out_dir / archive
    req.out_dir.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        archive_path.unlink()
    req.run_cmd(
        # `--` ends the option list: the operands are producer-declared DATA, and
        # without it GNU tar would run a declared `--checkpoint-action=exec=…`.
        ["tar", "-czf", str(archive_path), "-C", str(leg_dir), "--", *present],
        req.root,
    )
    return Composed(req.artifact.name, spec.composition, (archive,))


#: wasm-pack's own default output target, when the artifact declares none.
WASM_PACK_DEFAULT_TARGET = "bundler"


def crate_dir_for_package(metadata: dict, package: str) -> Path | None:
    """The absolute crate dir of workspace ``package``, or ``None`` when absent."""
    for pkg in metadata.get("packages", []):
        if pkg.get("name") == package:
            manifest = pkg.get("manifest_path")
            if manifest:
                return Path(manifest).parent
    return None


def _wasm_crate_dir(
    req: ComposeRequest, leg: config.ToolchainEntry, package: str | None
) -> Path:
    """The declared build package's crate dir, else the rust leg path."""
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
    """``wasm-pack build`` the artifact's wasm crate, then ``npm pack`` the one tarball."""
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
        # Keeps a failed prior run from leaking a stale tree into this npm pack.
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
    """Stage the unsigned ``.app``/``.dmg`` pair, re-emitting the ``.app`` as a tar
    preserving the symlinks and exec bits cross-job upload destroys.
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
    """Run the declared bundler, then stage the exactly-one ``.app``/``.dmg`` pair."""
    spec = req.artifact.bundle
    assert spec is not None and spec.command is not None and spec.source is not None
    req.run_cmd(list(spec.command), req.root)
    return _stage_mac_pair(req, req.root / spec.source, "mac-app")


#: Each tool-CONTROLLED subdir ``tauri build`` writes → its ONE primary output.
_TAURI_LINUX_FORMATS: tuple[tuple[str, str], ...] = (
    ("appimage", "*.AppImage"),
    ("deb", "*.deb"),
)


def _compose_tauri(req: ComposeRequest) -> Composed:
    """Run the declared ``tauri build``, then collect this platform's bundles from
    its tool-controlled subdirs; it never deletes under the declared ``source``.
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


#: Per platform: a triple substring, the required PRIMARY distributable suffix,
#: and the sidecars shipped beside it WHEN PRESENT.
_ELECTRON_TARGETS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("apple-darwin", ".dmg", (".dmg.blockmap",)),
    ("linux", ".AppImage", (".AppImage.blockmap",)),
    ("windows", ".exe", (".exe.blockmap",)),
)


def _electron_target(target: str) -> tuple[str, tuple[str, ...]]:
    """The ``(primary_suffix, sidecars)`` electron-builder emits for ``target``."""
    for needle, primary, sidecars in _ELECTRON_TARGETS:
        if needle in target:
            return primary, sidecars
    raise ReleaseError(
        f"electron composition: target `{target}` is not a darwin/linux/"
        f"windows triple — electron-builder emits no distributable for it"
    )


def _compose_electron(req: ComposeRequest) -> Composed:
    """Run the declared electron-builder and collect this platform's distributables;
    the darwin ``.app`` ships UNSIGNED for the mac signer to reopen.
    """
    spec = req.artifact.bundle
    assert spec is not None and spec.command is not None and spec.source is not None
    req.run_cmd(list(spec.command), req.root)
    source = req.root / spec.source
    if source.resolve() == req.out_dir.resolve():
        # Equal dirs would copy a file onto itself (a cryptic SameFileError).
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
    # The signer reseals exactly one .dmg; linux/windows have no reseal step.
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
    # A sidecar rides ONLY when its primary did; the rest are stale leftovers.
    collected = set(outputs)
    for sidecar in sidecars:
        for side in sorted(source.rglob(f"*{sidecar}")):
            if side.is_file() and side.name.removesuffix(".blockmap") in collected:
                dest = req.out_dir / side.name
                if dest.exists():
                    dest.unlink()
                shutil.copy2(side, dest)
                outputs.append(side.name)
    if "apple-darwin" in req.target:
        outputs.extend(_stage_electron_reseal_payload(req, source))
    return Composed(req.artifact.name, "electron", tuple(sorted(outputs)))


def _electron_top_level_apps(source: Path) -> list[Path]:
    """The ``.app`` bundles under ``source`` not nested inside another ``.app``."""
    return [
        p
        for p in sorted(source.rglob("*.app"))
        if p.is_dir()
        and not any(part.endswith(".app") for part in p.relative_to(source).parts[:-1])
    ]


def _stage_electron_reseal_payload(req: ComposeRequest, source: Path) -> list[str]:
    """Stage the darwin ``.app`` plus the ``.unsigned-app.tar.gz`` the signer reopens."""
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
    """The VS Code target for a rust triple, or a refusal naming the mapped set."""
    vt = VSCE_TARGETS.get(target)
    if vt is None:
        known = ", ".join(sorted(VSCE_TARGETS))
        raise ReleaseError(
            f"vsix composition: target triple `{target}` has no VS Code "
            f"marketplace target — the mapped rust triples are: {known}"
        )
    return vt


def _staged_dest(leg_dir: Path, dest: str, *, windows: bool) -> Path:
    """Where a ``bundle.stage`` entry copies to, ``.exe``-suffixed on a windows target."""
    if windows and not dest.lower().endswith(".exe"):
        dest = f"{dest}.exe"
    return leg_dir / dest


def _dirs_staging_will_create(leg_dir: Path, parent: Path) -> list[Path]:
    """The not-yet-existing ancestors of ``parent`` under ``leg_dir``, deepest-first."""
    to_create: list[Path] = []
    current = parent
    while current != leg_dir and leg_dir in current.parents and not current.exists():
        to_create.append(current)
        current = current.parent
    return to_create


def _unstage_vsix_natives(staged: list[Path], created_dirs: list[Path]) -> None:
    """Remove every staged binary, then every dir staging created, if now empty.

    Runs in a ``finally``, so a failed ``rmdir`` never masks the original failure.
    """
    for path in staged:
        path.unlink(missing_ok=True)
    # Deepest-first GLOBALLY so a nested `a/b` is emptied before its parent `a`.
    for directory in sorted(
        set(created_dirs), key=lambda p: len(p.parts), reverse=True
    ):
        try:
            directory.rmdir()
        except OSError:
            pass  # non-empty (holds other content) or already gone — leave it


def _stage_vsix_natives(
    req: ComposeRequest,
    leg_dir: Path,
    staged: list[Path],
    created_dirs: list[Path],
) -> None:
    """Copy each declared ``bundle.stage`` native binary into the extension layout,
    appending to the caller-owned accumulators so a PARTIAL stage still cleans up.
    """
    stage = req.artifact.bundle.stage if req.artifact.bundle is not None else ()
    if not stage:
        return
    windows = _is_windows(req.target)
    leg_root = leg_dir.resolve()
    deps = {dep.package: dep for dep in req.artifact_deps}
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
        # lexists (not exists) so a DANGLING symlink is also a collision.
        if os.path.lexists(dst):
            raise ReleaseError(
                f"[artifacts.{req.artifact.name}] vsix stage: destination {dst} "
                f"already exists — `bundle.stage` must target a FRESH path the "
                f"extension does not commit (staging overwrites nothing tracked, "
                f"and cleanup then removes only what staging created). Point "
                f"`{package}` at a build-only path, not a checked-in file/dir/link"
            )
        # A symlinked PARENT would let the copy write beyond the leg dir.
        if not dst.resolve().is_relative_to(leg_root):
            raise ReleaseError(
                f"[artifacts.{req.artifact.name}] vsix stage: destination {dst} "
                f"resolves outside the extension leg dir ({leg_root}) — a "
                f"symlinked parent must not steer staging beyond the tree; point "
                f"`{package}` at a real path inside the extension"
            )
        # Record BEFORE the copy, so a copy2 dying mid-write is still cleaned up.
        created_dirs.extend(_dirs_staging_will_create(leg_dir, dst.parent))
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ReleaseError(
                f"[artifacts.{req.artifact.name}] vsix stage: cannot create the "
                f"destination directory for `{package}` at {dst.parent} — an "
                f"intermediate path component is a file, not a directory; point "
                f"`{package}` at a path whose parents are dirs the extension owns"
            ) from exc
        staged.append(dst)
        shutil.copy2(src, dst)
        # Keep the exec bit: the LSP the extension spawns must stay runnable.
        dst.chmod(dst.stat().st_mode | 0o755)


def _compose_vsix(req: ComposeRequest) -> Composed:
    """Package the per-target ``.vsix`` via ``vsce``, staging declared natives first."""
    leg = _leg_for(req.artifact, req.entries, "npm", "vsix")
    vt = vsce_target(req.target)
    leg_dir = req.root / leg.path
    out_name = f"{req.artifact.name}-{vt}.vsix"
    out_path = req.out_dir / out_name
    staged: list[Path] = []
    created_dirs: list[Path] = []
    try:
        _stage_vsix_natives(req, leg_dir, staged, created_dirs)
        req.out_dir.mkdir(parents=True, exist_ok=True)
        if out_path.exists():
            out_path.unlink()
        req.run_cmd(
            [
                "npm",
                "exec",
                "--",
                "vsce",
                "package",
                # These extensions are esbuild-BUNDLED, so vsce's `npm ls` walk
                # adds only failure surface: a hollow .vsix, or a dead pack.
                "--no-dependencies",
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
        _unstage_vsix_natives(staged, created_dirs)
    return Composed(req.artifact.name, "vsix", (out_name,))


@dataclass(frozen=True)
class Composition:
    """One registry entry: a name, its compose function, and the platforms it applies to.

    ``platforms`` holds target-triple substrings; empty means every platform.
    ``platform_independent`` marks an output with no ``-<target>`` qualifier, which
    must build on exactly one leg or the merged ``dist/`` collides.
    """

    name: str
    compose: Callable[[ComposeRequest], Composed]
    platforms: tuple[str, ...] = ()
    declared_command: bool = False
    declared_payload: bool = False
    signable: bool = False
    asserts_binary: bool = True
    platform_independent: bool = False
    option_keys: tuple[str, ...] = ()
    provisions_signal: str | None = None

    def applies(self, target: str) -> bool:
        return not self.platforms or any(p in target for p in self.platforms)


ARCHIVE = Composition("archive", _compose_archive, signable=True)
DEB = Composition("deb", _compose_deb, platforms=("linux",))
#: A python sdist+wheel carries no native binary for the integrity guard.
WHEEL = Composition("wheel", _compose_wheel, asserts_binary=False)
#: ``npm pack`` names its ``.tgz`` version- but not target-qualified.
WASM_PACK = Composition(
    "wasm-pack",
    _compose_wasm_pack,
    asserts_binary=False,
    platform_independent=True,
    option_keys=("scope", "wasm-target"),
    # `npm pack` needs node, but the crate's package.json is generated.
    provisions_signal="node",
)
#: A ``.vsix`` is a zip package with no reopenable main binary to assert.
VSIX = Composition(
    "vsix",
    _compose_vsix,
    platforms=("apple-darwin", "linux", "windows"),
    asserts_binary=False,
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
    # Windows is out of scope (no icon.ico), so a windows leg is a clean skip.
    platforms=("apple-darwin", "linux"),
    declared_command=True,
    signable=True,
)
ELECTRON = Composition(
    "electron",
    _compose_electron,
    platforms=("apple-darwin", "linux", "windows"),
    declared_command=True,
    # electron-builder does NOT sign at build: the darwin `.app` ships unsigned
    # and the standalone mac sign stage reopens it.
    signable=True,
)
#: The artifact's own ``bundle.payload``, as one unqualified ``<name>.tar.gz``.
TARBALL = Composition(
    "tarball",
    _compose_declared_payload,
    asserts_binary=False,
    platform_independent=True,
    declared_payload=True,
)
#: ``tarball``, kept under its own name so the ``zed`` endpoint pairs with it.
ZED = Composition(
    "zed",
    _compose_declared_payload,
    asserts_binary=False,
    platform_independent=True,
    declared_payload=True,
)

#: The CLOSED registry, in a stable order. Adding a composition is adding an
#: entry here — never a kind switch.
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
    ZED,
)


def names() -> tuple[str, ...]:
    return tuple(c.name for c in COMPOSITIONS)


def signable_names() -> tuple[str, ...]:
    return tuple(c.name for c in COMPOSITIONS if c.signable)


def declared_payload_names() -> tuple[str, ...]:
    return tuple(c.name for c in COMPOSITIONS if c.declared_payload)


def platform_independent_names() -> tuple[str, ...]:
    return tuple(c.name for c in COMPOSITIONS if c.platform_independent)


def composition(name: str) -> Composition | None:
    for comp in COMPOSITIONS:
        if comp.name == name:
            return comp
    return None


#: (system, machine) → target triple, both lowercased; the default with no
#: ``--target``.
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
    """The target triple for a ``(system, machine)`` pair, or ``None`` when unmapped."""
    return _HOST_TARGETS.get((system.lower(), machine.lower()))
