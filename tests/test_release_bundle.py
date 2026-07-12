"""`shipit release bundle` — recorded-invocation tests over the exec seam.

The compositions (:mod:`shipit.release.bundle`) are driven against real
tmp-path trees with the ONE effectful boundary recorded (PRD Testing
Decisions): the composition-command Exec seam (``run_cmd`` — exact command
lines, exact cwds), whose fake also SIMULATES what the real tool writes
(cargo-deb's ``.deb``, uv's wheel+sdist, the bundler's ``.app``/``.dmg``
pair) so the compose functions' hard output checks are exercised both ways.
The verb tests pin the acceptance contract: declaration-order walk,
passthrough for zero-bundle artifacts, other-platform skips, the ADR-0009
barrier, and — decisively — that recorded invocations show ONLY composition
commands (no ``gh release upload``, no codesign: uploads are publish's job,
signing the signer's). Prior art: the prepare stage's recorder tests.
"""

import json
import os
from pathlib import Path

import pytest

from shipit import config
from shipit.release import ReleaseError
from shipit.release import bundle as bundle_mod
from shipit.verbs import release as release_verb

LINUX = "x86_64-unknown-linux-gnu"
MAC = "aarch64-apple-darwin"
WIN = "x86_64-pc-windows-msvc"


class RunRecorder:
    """The recorded composition-command seam: exact argv, exact cwd.

    ``effects`` maps a command head (``"cargo"``, ``"uv"``, ``"tar"``…) to a
    callable simulating that tool's writes — the compose functions verify
    their outputs on the real filesystem, so the fake must produce them (or
    deliberately not, for the hard-fail tests).
    """

    def __init__(self, effects=None):
        self.calls = []
        self.effects = dict(effects or {})

    def __call__(self, argv, cwd):
        argv = [str(a) for a in argv]
        self.calls.append((tuple(argv), Path(cwd)))
        effect = self.effects.get(argv[0])
        if effect is not None:
            effect(argv, Path(cwd))
        return None

    @property
    def heads(self):
        return [argv[0] for argv, _ in self.calls]


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"#!/bin/sh\n")
    path.chmod(path.stat().st_mode | 0o755)
    return path


def _artifacts(spec: dict) -> tuple[config.Artifact, ...]:
    return config.load_artifacts({"artifacts": spec})


def _entries(mapping: dict) -> tuple[config.ToolchainEntry, ...]:
    return config.load_toolchains({"toolchains": mapping})


def _request(tmp_path, artifact, entries, *, target=LINUX, run_cmd):
    return bundle_mod.ComposeRequest(
        artifact=artifact,
        entries=entries,
        root=tmp_path,
        out_dir=tmp_path / "dist",
        target=target,
        run_cmd=run_cmd,
    )


@pytest.fixture
def cargo_deb_on_path(monkeypatch):
    """cargo-deb present on PATH — the deb tests' default, so the recorded
    calls are the composition's alone regardless of the host machine (the
    self-provisioning path is pinned by its own test)."""
    monkeypatch.setattr(bundle_mod.shutil, "which", lambda name: f"/stub/{name}")


# --------------------------------------------------------------------------
# archive — the tarball/zip contract
# --------------------------------------------------------------------------


def test_archive_stages_binary_plus_docs_and_tars_the_dist_subdir(tmp_path):
    (artifact,) = _artifacts(
        {"lex": {"build": ["rust"], "bundle": {"composition": "archive"}}}
    )
    entries = _entries({".": "rust"})
    _executable(tmp_path / "target/release/lex")
    (tmp_path / "README.md").write_text("readme")
    (tmp_path / "LICENSE").write_text("mit")
    recorder = RunRecorder()

    composed = bundle_mod.ARCHIVE.compose(
        _request(tmp_path, artifact, entries, run_cmd=recorder)
    )

    stem = f"lex-{LINUX}"
    assert recorder.calls == [
        (("tar", "-czf", f"{stem}.tar.gz", stem), tmp_path / "dist")
    ]
    stage = tmp_path / "dist" / stem
    assert (stage / "lex").is_file()
    assert os.access(stage / "lex", os.X_OK)
    assert (stage / "README.md").is_file()
    assert (stage / "LICENSE").is_file()
    # CHANGELOG.md is absent from the repo — docs ride only WHEN PRESENT.
    assert not (stage / "CHANGELOG.md").exists()
    assert composed == bundle_mod.Composed(
        "lex", "archive", (f"{stem}.tar.gz", f"{stem}/")
    )


def test_archive_windows_target_zips_and_takes_the_exe(tmp_path):
    (artifact,) = _artifacts(
        {"lex": {"build": ["rust"], "bundle": {"composition": "archive"}}}
    )
    entries = _entries({".": "rust"})
    _executable(tmp_path / "target/release/lex.exe")
    recorder = RunRecorder()

    composed = bundle_mod.ARCHIVE.compose(
        _request(tmp_path, artifact, entries, target=WIN, run_cmd=recorder)
    )

    stem = f"lex-{WIN}"
    assert recorder.calls == [(("zip", "-r", f"{stem}.zip", stem), tmp_path / "dist")]
    assert (tmp_path / "dist" / stem / "lex.exe").is_file()
    assert composed.outputs == (f"{stem}.zip", f"{stem}/")


def test_archive_without_a_built_binary_refuses(tmp_path):
    # Bundle CONSUMES build outputs — it never builds (that is `shipit build`).
    (artifact,) = _artifacts(
        {"lex": {"build": ["rust"], "bundle": {"composition": "archive"}}}
    )
    recorder = RunRecorder()
    with pytest.raises(ReleaseError, match="no built binary"):
        bundle_mod.ARCHIVE.compose(
            _request(tmp_path, artifact, _entries({".": "rust"}), run_cmd=recorder)
        )
    assert recorder.calls == []  # nothing ran, nothing written


def _tar_writes(argv, cwd):
    # tar/zip: write the named archive so the rerun path (unlink + recreate) is
    # exercised for real.
    (cwd / argv[2]).write_bytes(b"archive")


def test_archive_rerun_rebuilds_the_stage_and_replaces_the_archive(tmp_path):
    # A rerun must reflect the CURRENT build outputs: a doc a prior run shipped
    # but that is now gone must not survive in the staging dir (zip -r would
    # otherwise re-pack it), and the stale archive is replaced, not merged.
    (artifact,) = _artifacts(
        {"lex": {"build": ["rust"], "bundle": {"composition": "archive"}}}
    )
    entries = _entries({".": "rust"})
    _executable(tmp_path / "target/release/lex")
    stem = f"lex-{LINUX}"
    stage = tmp_path / "dist" / stem
    stage.mkdir(parents=True)
    (stage / "STALE.md").write_text("a doc a prior run shipped")
    (tmp_path / "dist" / f"{stem}.tar.gz").write_bytes(b"old")
    recorder = RunRecorder({"tar": _tar_writes})

    bundle_mod.ARCHIVE.compose(_request(tmp_path, artifact, entries, run_cmd=recorder))

    assert not (stage / "STALE.md").exists()  # staging dir rebuilt from scratch
    assert (stage / "lex").is_file()
    assert (tmp_path / "dist" / f"{stem}.tar.gz").read_bytes() == b"archive"


# --------------------------------------------------------------------------
# deb — cargo-deb over the pre-built binary
# --------------------------------------------------------------------------


def _deb_effect(name="lex-cli_1.0.0-1_amd64.deb"):
    def effect(argv, cwd):
        if "--output" not in argv:
            return  # `cargo install cargo-deb` writes nothing into the tree
        out = Path(argv[argv.index("--output") + 1])
        out.mkdir(parents=True, exist_ok=True)
        (out / name).write_bytes(b"deb")

    return effect


def test_deb_invokes_cargo_deb_no_rebuild_no_strip(tmp_path, cargo_deb_on_path):
    (artifact,) = _artifacts(
        {
            "lex-cli": {
                "build": [{"toolchain": "rust", "package": "lex-cli"}],
                "bundle": {"composition": "deb"},
            }
        }
    )
    recorder = RunRecorder({"cargo": _deb_effect()})

    composed = bundle_mod.DEB.compose(
        _request(tmp_path, artifact, _entries({".": "rust"}), run_cmd=recorder)
    )

    # cargo-deb writes into a per-artifact scratch dir under the output tree;
    # bundle then moves the .deb into `dist/` (rerun-safe: an overwrite of the
    # same name is no longer misread as "produced nothing").
    assert recorder.calls == [
        (
            (
                "cargo",
                "deb",
                "--no-build",
                "--no-strip",
                "-p",
                "lex-cli",
                "--output",
                str(tmp_path / "dist" / ".tmp-lex-cli"),
            ),
            tmp_path,  # cwd at the rust leg's map path
        )
    ]
    assert composed == bundle_mod.Composed(
        "lex-cli", "deb", ("lex-cli_1.0.0-1_amd64.deb",)
    )
    assert (tmp_path / "dist" / "lex-cli_1.0.0-1_amd64.deb").is_file()
    assert not (tmp_path / "dist" / ".tmp-lex-cli").exists()


def test_deb_never_forwards_target_to_cargo_deb(tmp_path, cargo_deb_on_path):
    # Issue #784 F3: `shipit build` builds natively into target/release/ (no
    # --target plumbing), so forwarding the bundle triple would redirect
    # cargo-deb to the EMPTY target/<triple>/release/. The triple is
    # naming-only; cargo-deb derives the Debian arch from the host toolchain
    # — correct by construction on the per-arch matrix runners.
    (artifact,) = _artifacts(
        {"lex": {"build": ["rust"], "bundle": {"composition": "deb"}}}
    )
    recorder = RunRecorder({"cargo": _deb_effect()})
    bundle_mod.DEB.compose(
        _request(tmp_path, artifact, _entries({".": "rust"}), run_cmd=recorder)
    )
    ((argv, _cwd),) = recorder.calls
    assert "--target" not in argv


def test_deb_self_provisions_cargo_deb_when_missing(tmp_path, monkeypatch):
    # Issue #784 F2: the wf-build runner arrives without cargo-deb (not on
    # conda-forge, so no pixi env can carry it) — the composition installs it
    # itself, through the same recorded Exec seam, BEFORE composing.
    monkeypatch.setattr(bundle_mod.shutil, "which", lambda name: None)
    (artifact,) = _artifacts(
        {
            "lex-cli": {
                "build": [{"toolchain": "rust", "package": "lex-cli"}],
                "bundle": {"composition": "deb"},
            }
        }
    )
    recorder = RunRecorder({"cargo": _deb_effect()})

    composed = bundle_mod.DEB.compose(
        _request(tmp_path, artifact, _entries({".": "rust"}), run_cmd=recorder)
    )

    install, compose_call = recorder.calls
    assert install == (
        (
            "cargo",
            "install",
            "cargo-deb",
            "--version",
            bundle_mod.CARGO_DEB_VERSION,
            "--locked",
        ),
        tmp_path,
    )
    assert compose_call[0][:4] == ("cargo", "deb", "--no-build", "--no-strip")
    assert composed.outputs == ("lex-cli_1.0.0-1_amd64.deb",)


def test_deb_hard_fails_when_no_deb_appears(tmp_path, cargo_deb_on_path):
    # The legacy build-deb contract: a green cargo-deb run that wrote nothing
    # is a FAILURE, never a quiet pass.
    (artifact,) = _artifacts(
        {"lex": {"build": ["rust"], "bundle": {"composition": "deb"}}}
    )
    recorder = RunRecorder()  # cargo "succeeds" but writes no .deb
    with pytest.raises(ReleaseError, match=r"produced no \.deb"):
        bundle_mod.DEB.compose(
            _request(tmp_path, artifact, _entries({".": "rust"}), run_cmd=recorder)
        )


def test_deb_rerun_overwrites_an_existing_same_named_deb(tmp_path, cargo_deb_on_path):
    # The common rerun case (same version, same target): an identically-named
    # .deb already sits in the output tree. The old before/after subtraction
    # misread the overwrite as "produced no .deb"; the scratch-dir emit sees it.
    (artifact,) = _artifacts(
        {
            "lex-cli": {
                "build": [{"toolchain": "rust", "package": "lex-cli"}],
                "bundle": {"composition": "deb"},
            }
        }
    )
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "lex-cli_1.0.0-1_amd64.deb").write_bytes(b"stale")
    recorder = RunRecorder({"cargo": _deb_effect()})

    composed = bundle_mod.DEB.compose(
        _request(tmp_path, artifact, _entries({".": "rust"}), run_cmd=recorder)
    )

    assert composed.outputs == ("lex-cli_1.0.0-1_amd64.deb",)
    assert (dist / "lex-cli_1.0.0-1_amd64.deb").read_bytes() == b"deb"  # fresh
    assert not (dist / ".tmp-lex-cli").exists()


def test_deb_without_a_rust_leg_refuses(tmp_path):
    (artifact,) = _artifacts(
        {"lex": {"build": ["rust"], "bundle": {"composition": "deb"}}}
    )
    with pytest.raises(ReleaseError, match=r"needs a \[toolchains\] rust leg"):
        bundle_mod.DEB.compose(
            _request(
                tmp_path, artifact, _entries({".": "python"}), run_cmd=RunRecorder()
            )
        )


# --------------------------------------------------------------------------
# wheel — uv build into the bundle output tree
# --------------------------------------------------------------------------


def _uv_effect(names=("pkg-1.0.0-py3-none-any.whl", "pkg-1.0.0.tar.gz")):
    def effect(argv, cwd):
        out = Path(argv[argv.index("--out-dir") + 1])
        out.mkdir(parents=True, exist_ok=True)
        for name in names:
            (out / name).write_bytes(b"pkg")

    return effect


def test_wheel_builds_both_wheel_and_sdist_into_the_out_tree(tmp_path):
    (artifact,) = _artifacts(
        {"pkg": {"build": ["python"], "bundle": {"composition": "wheel"}}}
    )
    recorder = RunRecorder({"uv": _uv_effect()})

    composed = bundle_mod.WHEEL.compose(
        _request(tmp_path, artifact, _entries({".": "python"}), run_cmd=recorder)
    )

    # uv builds into a per-artifact scratch dir under the output tree; bundle
    # then moves the wheel + sdist into `dist/` (rerun-safe overwrite).
    assert recorder.calls == [
        (("uv", "build", "--out-dir", str(tmp_path / "dist" / ".tmp-pkg")), tmp_path)
    ]
    assert composed == bundle_mod.Composed(
        "pkg", "wheel", ("pkg-1.0.0-py3-none-any.whl", "pkg-1.0.0.tar.gz")
    )
    assert (tmp_path / "dist" / "pkg-1.0.0-py3-none-any.whl").is_file()
    assert (tmp_path / "dist" / "pkg-1.0.0.tar.gz").is_file()
    assert not (tmp_path / "dist" / ".tmp-pkg").exists()


@pytest.mark.parametrize(
    "produced,missing",
    [(("pkg-1.0.0.tar.gz",), "a wheel"), (("pkg-1.0.0-py3-none-any.whl",), "an sdist")],
)
def test_wheel_requires_both_halves(tmp_path, produced, missing):
    (artifact,) = _artifacts(
        {"pkg": {"build": ["python"], "bundle": {"composition": "wheel"}}}
    )
    recorder = RunRecorder({"uv": _uv_effect(produced)})
    with pytest.raises(ReleaseError, match=f"missing {missing}"):
        bundle_mod.WHEEL.compose(
            _request(tmp_path, artifact, _entries({".": "python"}), run_cmd=recorder)
        )


def test_wheel_rerun_overwrites_existing_same_named_outputs(tmp_path):
    # Same rerun hazard as deb: identically-named wheel + sdist already present.
    (artifact,) = _artifacts(
        {"pkg": {"build": ["python"], "bundle": {"composition": "wheel"}}}
    )
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "pkg-1.0.0-py3-none-any.whl").write_bytes(b"stale")
    (dist / "pkg-1.0.0.tar.gz").write_bytes(b"stale")
    recorder = RunRecorder({"uv": _uv_effect()})

    composed = bundle_mod.WHEEL.compose(
        _request(tmp_path, artifact, _entries({".": "python"}), run_cmd=recorder)
    )

    assert composed.outputs == ("pkg-1.0.0-py3-none-any.whl", "pkg-1.0.0.tar.gz")
    assert (dist / "pkg-1.0.0-py3-none-any.whl").read_bytes() == b"pkg"  # fresh
    assert not (dist / ".tmp-pkg").exists()


# --------------------------------------------------------------------------
# mac-app — the coupled unsigned pair + the reseal payload
# --------------------------------------------------------------------------

MAC_APP_SPEC = {
    "app": {
        "build": ["rust"],
        "bundle": {
            "composition": "mac-app",
            "command": ["npm", "run", "bundle"],
            "source": "src-tauri/target/release/bundle",
        },
    }
}


def _bundler_effect(root, app="Phos.app", dmg="Phos_1.0.0_aarch64.dmg"):
    """Simulate the declared bundler: the coupled .app/.dmg pair, the .app
    carrying a symlink (the thing artifact upload destroys)."""

    def effect(argv, cwd):
        source = root / "src-tauri/target/release/bundle"
        macos = source / "macos" / app / "Contents" / "MacOS"
        _executable(macos / "phos")
        (source / "macos" / app / "Contents" / "Current").symlink_to("MacOS")
        (source / "dmg").mkdir(parents=True, exist_ok=True)
        (source / "dmg" / dmg).write_bytes(b"dmg")

    return effect


def _tar_effect(argv, cwd):
    Path(argv[2]).parent.mkdir(parents=True, exist_ok=True)
    Path(argv[2]).write_bytes(b"tar")


def test_mac_app_emits_the_pair_and_the_reseal_payload(tmp_path):
    (artifact,) = _artifacts(MAC_APP_SPEC)
    recorder = RunRecorder({"npm": _bundler_effect(tmp_path), "tar": _tar_effect})

    composed = bundle_mod.MAC_APP.compose(
        _request(tmp_path, artifact, (), target=MAC, run_cmd=recorder)
    )

    source = tmp_path / "src-tauri/target/release/bundle"
    out = tmp_path / "dist"
    assert recorder.calls == [
        (("npm", "run", "bundle"), tmp_path),
        (
            (
                "tar",
                "-czf",
                str(out / "app.unsigned-app.tar.gz"),
                "-C",
                str(source / "macos"),
                "Phos.app",
            ),
            tmp_path,
        ),
    ]
    # The coupled pair landed in the out tree, the .app with its symlink
    # intact (the reseal tar — not the copy — is the transport-safe form).
    assert (out / "Phos.app/Contents/MacOS/phos").is_file()
    assert (out / "Phos.app/Contents/Current").is_symlink()
    assert (out / "Phos_1.0.0_aarch64.dmg").is_file()
    assert composed == bundle_mod.Composed(
        "app",
        "mac-app",
        ("Phos.app", "Phos_1.0.0_aarch64.dmg", "app.unsigned-app.tar.gz"),
    )


def test_mac_app_without_the_reseal_payload_is_a_bundle_failure(tmp_path):
    # workflows.lex §3.1: a missing payload must fail HERE, never surprise
    # the signer. The tar "succeeds" but writes nothing.
    (artifact,) = _artifacts(MAC_APP_SPEC)
    recorder = RunRecorder({"npm": _bundler_effect(tmp_path)})
    with pytest.raises(ReleaseError, match="no reseal payload"):
        bundle_mod.MAC_APP.compose(
            _request(tmp_path, artifact, (), target=MAC, run_cmd=recorder)
        )


def test_mac_app_requires_exactly_one_coupled_pair(tmp_path):
    (artifact,) = _artifacts(MAC_APP_SPEC)

    def two_apps(argv, cwd):
        _bundler_effect(tmp_path)(argv, cwd)
        _bundler_effect(tmp_path, app="Other.app")(argv, cwd)

    recorder = RunRecorder({"npm": two_apps, "tar": _tar_effect})
    with pytest.raises(ReleaseError, match=r"exactly one coupled \.app/\.dmg pair"):
        bundle_mod.MAC_APP.compose(
            _request(tmp_path, artifact, (), target=MAC, run_cmd=recorder)
        )


# --------------------------------------------------------------------------
# wasm-pack — build the pkg tree, npm pack the tarball (TOL02-WS12 #788)
# --------------------------------------------------------------------------

WASM_SPEC = {
    "wasm": {
        "build": ["rust"],
        "bundle": {"composition": "wasm-pack", "scope": "lex-fmt"},
    }
}


def _wasm_pack_effect(name="package.json"):
    """Simulate `wasm-pack build`: it writes an npm package tree (at least a
    package.json) under its --out-dir."""

    def effect(argv, cwd):
        out = Path(argv[argv.index("--out-dir") + 1])
        out.mkdir(parents=True, exist_ok=True)
        (out / name).write_text('{"name": "@lex-fmt/lex-wasm"}', encoding="utf-8")

    return effect


def _npm_pack_effect(tarball="lex-fmt-lex-wasm-1.2.3.tgz"):
    """Simulate `npm pack`: it writes ONE .tgz into --pack-destination."""

    def effect(argv, cwd):
        out = Path(argv[argv.index("--pack-destination") + 1])
        out.mkdir(parents=True, exist_ok=True)
        (out / tarball).write_bytes(b"tgz")

    return effect


def test_wasm_pack_builds_the_pkg_tree_and_packs_the_tarball(tmp_path):
    (artifact,) = _artifacts(WASM_SPEC)
    recorder = RunRecorder(
        {"wasm-pack": _wasm_pack_effect(), "npm": _npm_pack_effect()}
    )

    composed = bundle_mod.WASM_PACK.compose(
        _request(
            tmp_path, artifact, _entries({"crates/lex-wasm": "rust"}), run_cmd=recorder
        )
    )

    dist = tmp_path / "dist"
    crate = tmp_path / "crates/lex-wasm"
    pkg = dist / ".pkg-wasm"
    scratch = dist / ".tmp-wasm"
    # wasm-pack builds the rust crate into a fresh pkg tree (default target
    # `bundler`, the declared `--scope`); npm pack then tarballs it into a
    # scratch that bundle moves into dist/.
    assert recorder.calls == [
        (
            (
                "wasm-pack",
                "build",
                "--release",
                "--target",
                "bundler",
                "--out-dir",
                str(pkg),
                "--scope",
                "lex-fmt",
            ),
            crate,
        ),
        (("npm", "pack", "--pack-destination", str(scratch)), pkg),
    ]
    assert composed == bundle_mod.Composed(
        "wasm", "wasm-pack", ("lex-fmt-lex-wasm-1.2.3.tgz",)
    )
    assert (dist / "lex-fmt-lex-wasm-1.2.3.tgz").is_file()
    # only the tarball survives — the scratch pkg tree and npm-pack scratch go
    assert not pkg.exists()
    assert not scratch.exists()


def test_wasm_pack_defaults_the_target_and_omits_scope_when_undeclared(tmp_path):
    (artifact,) = _artifacts(
        {"wasm": {"build": ["rust"], "bundle": {"composition": "wasm-pack"}}}
    )
    recorder = RunRecorder(
        {"wasm-pack": _wasm_pack_effect(), "npm": _npm_pack_effect("wasm-1.2.3.tgz")}
    )

    bundle_mod.WASM_PACK.compose(
        _request(tmp_path, artifact, _entries({".": "rust"}), run_cmd=recorder)
    )

    wasm_argv = recorder.calls[0][0]
    assert "--scope" not in wasm_argv  # unscoped package: no --scope flag
    assert wasm_argv[: wasm_argv.index("--out-dir")] == (
        "wasm-pack",
        "build",
        "--release",
        "--target",
        "bundler",
    )


def test_wasm_pack_honors_a_declared_wasm_target(tmp_path):
    (artifact,) = _artifacts(
        {
            "wasm": {
                "build": ["rust"],
                "bundle": {"composition": "wasm-pack", "wasm-target": "web"},
            }
        }
    )
    recorder = RunRecorder(
        {"wasm-pack": _wasm_pack_effect(), "npm": _npm_pack_effect("wasm-1.2.3.tgz")}
    )

    bundle_mod.WASM_PACK.compose(
        _request(tmp_path, artifact, _entries({".": "rust"}), run_cmd=recorder)
    )

    wasm_argv = recorder.calls[0][0]
    assert wasm_argv[wasm_argv.index("--target") + 1] == "web"


def test_wasm_pack_hard_fails_when_the_build_leaves_no_package_json(tmp_path):
    (artifact,) = _artifacts(WASM_SPEC)
    # wasm-pack "runs" but writes nothing — a wrong/empty build, not a tarball.
    recorder = RunRecorder({"npm": _npm_pack_effect()})
    with pytest.raises(ReleaseError, match="left no package.json"):
        bundle_mod.WASM_PACK.compose(
            _request(
                tmp_path,
                artifact,
                _entries({"crates/lex-wasm": "rust"}),
                run_cmd=recorder,
            )
        )
    # the barrier holds: no tarball leaked into dist/, scratch cleaned
    assert not (tmp_path / "dist" / ".pkg-wasm").exists()


def test_wasm_pack_hard_fails_when_npm_pack_yields_no_tarball(tmp_path):
    (artifact,) = _artifacts(WASM_SPEC)
    # build succeeds, but npm pack produces nothing — a hard fail, never a pass.
    recorder = RunRecorder({"wasm-pack": _wasm_pack_effect()})
    with pytest.raises(ReleaseError, match=r"produced 0 \.tgz"):
        bundle_mod.WASM_PACK.compose(
            _request(
                tmp_path,
                artifact,
                _entries({"crates/lex-wasm": "rust"}),
                run_cmd=recorder,
            )
        )
    assert not (tmp_path / "dist" / ".pkg-wasm").exists()


def test_wasm_pack_needs_a_rust_leg(tmp_path):
    (artifact,) = _artifacts(WASM_SPEC)
    # no rust leg mapped -> a loud refusal naming the composition, never a skip.
    with pytest.raises(
        ReleaseError, match=r"wasm-pack composition needs a \[toolchains\] rust leg"
    ):
        bundle_mod.WASM_PACK.compose(
            _request(
                tmp_path, artifact, _entries({".": "python"}), run_cmd=RunRecorder()
            )
        )


# --------------------------------------------------------------------------
# The registry and the host-target derivation
# --------------------------------------------------------------------------


def test_registry_is_closed_and_platform_scoped():
    assert bundle_mod.names() == ("archive", "deb", "wheel", "wasm-pack", "mac-app")
    assert bundle_mod.composition("deb") is bundle_mod.DEB
    assert bundle_mod.composition("wasm-pack") is bundle_mod.WASM_PACK
    assert bundle_mod.composition("rpm") is None
    assert bundle_mod.ARCHIVE.applies(LINUX) and bundle_mod.ARCHIVE.applies(MAC)
    assert bundle_mod.DEB.applies(LINUX) and not bundle_mod.DEB.applies(MAC)
    assert bundle_mod.MAC_APP.applies(MAC) and not bundle_mod.MAC_APP.applies(LINUX)
    assert bundle_mod.WHEEL.applies(WIN)
    # wasm is platform-independent — built once, published once (no triple gate).
    assert bundle_mod.WASM_PACK.applies(LINUX) and bundle_mod.WASM_PACK.applies(MAC)


def test_registry_marks_the_signer_reopenable_compositions():
    # The signable set IS the signer's leg set (TOL02-WS08 #779): mac-app
    # (the reseal payload leg) and archive (the raw-binary tarball leg).
    # The config boundary refuses `sign = true` on anything else, so a sign
    # declaration can never route to a signer leg that does not exist.
    assert bundle_mod.signable_names() == ("archive", "mac-app")


@pytest.mark.parametrize(
    "system,machine,expected",
    [
        ("Darwin", "arm64", "aarch64-apple-darwin"),
        ("Linux", "x86_64", "x86_64-unknown-linux-gnu"),
        ("Windows", "AMD64", "x86_64-pc-windows-msvc"),
        ("SunOS", "sparc", None),
    ],
)
def test_host_target_derivation(system, machine, expected):
    assert bundle_mod.host_target(system, machine) == expected


# --------------------------------------------------------------------------
# The verb: walk, passthrough, skips, barrier, no uploads
# --------------------------------------------------------------------------

REPO_TOML = """\
[toolchains]
"." = "rust"

[artifacts.lex]
build = ["rust"]
bundle = { composition = "archive" }

[artifacts.lex-deb]
build = [{ toolchain = "rust", package = "lex" }]
bundle = { composition = "deb" }

[artifacts.plugin]
endpoints = ["gh-release"]
"""


def _repo(tmp_path, monkeypatch, toml=REPO_TOML):
    (tmp_path / ".shipit.toml").write_text(toml, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # bundle anchors config and the output tree to the checkout root (not cwd),
    # so a run from a subdirectory sees the same map; the tmp repo is not a git
    # checkout, so stub the adapter to report its root.
    monkeypatch.setattr(release_verb.git, "repo_root", lambda *, cwd: str(tmp_path))
    return tmp_path


def test_bundle_walks_the_map_composing_skipping_and_passing_through(
    tmp_path, monkeypatch, capsys
):
    root = _repo(tmp_path, monkeypatch)
    _executable(root / "target/release/lex")
    recorder = RunRecorder()

    rc = release_verb.run_bundle(target=MAC, run_cmd=recorder)

    assert rc == 0
    out = capsys.readouterr().out
    assert "bundled 1 artifact" in out
    assert "lex-deb  [deb]  skipped: not for this target" in out
    assert "plugin  passthrough: no bundle declared" in out
    # Recorded invocations show ONLY composition commands: no `gh release
    # upload` (publish's job), no codesign (the signer's).
    assert recorder.heads == ["tar"]


def test_bundle_composes_the_deb_on_its_platform(
    tmp_path, monkeypatch, capsys, cargo_deb_on_path
):
    root = _repo(tmp_path, monkeypatch)
    _executable(root / "target/release/lex")
    recorder = RunRecorder({"cargo": _deb_effect()})

    rc = release_verb.run_bundle(target=LINUX, run_cmd=recorder)

    assert rc == 0
    assert recorder.heads == ["tar", "cargo"]  # declaration order


def test_bundle_json_carries_the_typed_result(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path, monkeypatch)
    _executable(root / "target/release/lex")
    rc = release_verb.run_bundle(target=MAC, as_json=True, run_cmd=RunRecorder())
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["target"] == MAC
    assert payload["composed"] == [
        {
            "artifact": "lex",
            "composition": "archive",
            "outputs": [f"lex-{MAC}.tar.gz", f"lex-{MAC}/"],
        }
    ]
    assert payload["skipped"] == [{"artifact": "lex-deb", "composition": "deb"}]
    assert payload["passthrough"] == ["plugin"]


def test_bundle_barrier_first_failure_leaves_later_artifacts_untouched(
    tmp_path, monkeypatch, capsys
):
    # ADR-0009: a failing composition for ANY artifact exits non-zero and the
    # walk stops — the later artifact's composition is never invoked.
    _repo(tmp_path, monkeypatch)  # no built binary: archive will refuse
    recorder = RunRecorder({"cargo": _deb_effect()})

    rc = release_verb.run_bundle(target=LINUX, run_cmd=recorder)

    assert rc == 1
    assert "error: " in capsys.readouterr().err
    assert recorder.calls == []  # nothing ran — nothing half-written anywhere


def test_bundle_with_nothing_declared_is_a_clean_noop(tmp_path, monkeypatch, capsys):
    _repo(
        tmp_path,
        monkeypatch,
        '[toolchains]\n"." = "rust"\n[artifacts.plugin]\nendpoints = ["gh-release"]\n',
    )
    rc = release_verb.run_bundle(target=LINUX, run_cmd=RunRecorder())
    assert rc == 0
    assert "no bundle declared" in capsys.readouterr().out


def test_bundle_refuses_an_underivable_host_target(tmp_path, monkeypatch, capsys):
    _repo(tmp_path, monkeypatch)
    monkeypatch.setattr(release_verb.platform, "system", lambda: "SunOS")
    monkeypatch.setattr(release_verb.platform, "machine", lambda: "sparc")
    rc = release_verb.run_bundle(run_cmd=RunRecorder())
    assert rc == 1
    assert "pass --target" in capsys.readouterr().err


def test_bundle_refuses_outside_a_git_checkout(tmp_path, monkeypatch, capsys):
    # Config and the output tree anchor to the checkout root; without one the
    # stage refuses loudly instead of silently reading zero artifacts from cwd.
    _repo(tmp_path, monkeypatch)
    monkeypatch.setattr(release_verb.git, "repo_root", lambda *, cwd: None)
    rc = release_verb.run_bundle(target=LINUX, run_cmd=RunRecorder())
    assert rc == 1
    assert "not inside a git checkout" in capsys.readouterr().err


def test_bundle_artifact_narrows_the_walk_to_one_artifact(
    tmp_path, monkeypatch, capsys
):
    # The per-matrix-entry contract (wf-build passes its entry's artifact):
    # the narrowed walk composes exactly that artifact, so the cross-job
    # bundle tree never carries a sibling artifact's binary (which would
    # fail wf-publish's per-artifact assert-bundle on a multi-artifact map).
    root = _repo(tmp_path, monkeypatch)
    _executable(root / "target/release/lex")
    recorder = RunRecorder({"cargo": _deb_effect()})

    rc = release_verb.run_bundle(target=LINUX, artifact="lex", run_cmd=recorder)

    assert rc == 0
    out = capsys.readouterr().out
    assert "bundled 1 artifact" in out
    # lex-deb applies on LINUX but is outside the narrowed walk: its
    # composition never runs — only lex's archive tar.
    assert recorder.heads == ["tar"]
    assert "lex-deb" not in out


def test_bundle_artifact_unknown_name_is_a_loud_refusal(tmp_path, monkeypatch, capsys):
    _repo(tmp_path, monkeypatch)
    recorder = RunRecorder()

    rc = release_verb.run_bundle(target=LINUX, artifact="nope", run_cmd=recorder)

    assert rc == 1
    err = capsys.readouterr().err
    assert "--artifact nope" in err
    assert "lex, lex-deb, plugin" in err  # names the declared set
    assert recorder.calls == []  # refused before any composition ran
