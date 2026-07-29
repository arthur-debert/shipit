import json
import os
import tarfile
from pathlib import Path, PurePosixPath

import pytest

from shipit import config, execrun
from shipit.install import artifactdeps
from shipit.release import ReleaseError
from shipit.release import bundle as bundle_mod
from shipit.verbs import release as release_verb

LINUX = "x86_64-unknown-linux-gnu"
MAC = "aarch64-apple-darwin"
WIN = "x86_64-pc-windows-msvc"


class RunRecorder:
    def __init__(self, effects=None):
        self.calls = []
        self.effects = dict(effects or {})

    def __call__(self, argv, cwd):
        argv = [str(a) for a in argv]
        self.calls.append((tuple(argv), Path(cwd)))
        effect = self.effects.get(argv[0])
        return effect(argv, Path(cwd)) if effect is not None else None

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


def _request(
    tmp_path,
    artifact,
    entries,
    *,
    target=LINUX,
    build_target=None,
    run_cmd,
    artifact_deps=(),
):
    return bundle_mod.ComposeRequest(
        artifact=artifact,
        entries=entries,
        root=tmp_path,
        out_dir=tmp_path / "dist",
        target=target,
        run_cmd=run_cmd,
        build_target=build_target,
        artifact_deps=artifact_deps,
    )


@pytest.fixture
def cargo_deb_on_path(monkeypatch):
    monkeypatch.setattr(bundle_mod.shutil, "which", lambda name: f"/stub/{name}")


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


def test_archive_cross_build_reads_the_triple_release_dir(tmp_path):
    (artifact,) = _artifacts(
        {"lex": {"build": ["rust"], "bundle": {"composition": "archive"}}}
    )
    entries = _entries({".": "rust"})
    musl = "x86_64-unknown-linux-musl"
    _executable(tmp_path / f"target/{musl}/release/lex")
    recorder = RunRecorder()

    composed = bundle_mod.ARCHIVE.compose(
        _request(
            tmp_path,
            artifact,
            entries,
            target=musl,
            build_target=musl,
            run_cmd=recorder,
        )
    )

    stem = f"lex-{musl}"
    assert (tmp_path / "dist" / stem / "lex").is_file()
    assert composed.outputs == (f"{stem}.tar.gz", f"{stem}/")


def test_archive_without_a_built_binary_refuses(tmp_path):
    (artifact,) = _artifacts(
        {"lex": {"build": ["rust"], "bundle": {"composition": "archive"}}}
    )
    recorder = RunRecorder()
    with pytest.raises(ReleaseError, match="no built binary"):
        bundle_mod.ARCHIVE.compose(
            _request(tmp_path, artifact, _entries({".": "rust"}), run_cmd=recorder)
        )
    assert recorder.calls == []


def _tar_writes(argv, cwd):
    (cwd / argv[2]).write_bytes(b"archive")


def test_archive_rerun_rebuilds_the_stage_and_replaces_the_archive(tmp_path):
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

    assert not (stage / "STALE.md").exists()
    assert (stage / "lex").is_file()
    assert (tmp_path / "dist" / f"{stem}.tar.gz").read_bytes() == b"archive"


def _deb_effect(name="lex-cli_1.0.0-1_amd64.deb"):
    def effect(argv, cwd):
        if "--output" not in argv:
            return
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
            tmp_path,
        )
    ]
    assert composed == bundle_mod.Composed(
        "lex-cli", "deb", ("lex-cli_1.0.0-1_amd64.deb",)
    )
    assert (tmp_path / "dist" / "lex-cli_1.0.0-1_amd64.deb").is_file()
    assert not (tmp_path / "dist" / ".tmp-lex-cli").exists()


def test_deb_native_build_omits_target(tmp_path, cargo_deb_on_path):
    (artifact,) = _artifacts(
        {"lex": {"build": ["rust"], "bundle": {"composition": "deb"}}}
    )
    recorder = RunRecorder({"cargo": _deb_effect()})
    bundle_mod.DEB.compose(
        _request(tmp_path, artifact, _entries({".": "rust"}), run_cmd=recorder)
    )
    ((argv, _cwd),) = recorder.calls
    assert "--target" not in argv


def test_deb_cross_build_forwards_the_triple_to_cargo_deb(tmp_path, cargo_deb_on_path):
    (artifact,) = _artifacts(
        {"lex": {"build": ["rust"], "bundle": {"composition": "deb"}}}
    )
    musl = "x86_64-unknown-linux-musl"
    recorder = RunRecorder({"cargo": _deb_effect()})
    bundle_mod.DEB.compose(
        _request(
            tmp_path,
            artifact,
            _entries({".": "rust"}),
            target=musl,
            build_target=musl,
            run_cmd=recorder,
        )
    )
    ((argv, _cwd),) = recorder.calls
    assert argv[argv.index("--target") + 1] == musl


def test_deb_self_provisions_cargo_deb_when_missing(tmp_path, monkeypatch):
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
    (artifact,) = _artifacts(
        {"lex": {"build": ["rust"], "bundle": {"composition": "deb"}}}
    )
    recorder = RunRecorder()
    with pytest.raises(ReleaseError, match=r"produced no \.deb"):
        bundle_mod.DEB.compose(
            _request(tmp_path, artifact, _entries({".": "rust"}), run_cmd=recorder)
        )


def test_deb_rerun_overwrites_an_existing_same_named_deb(tmp_path, cargo_deb_on_path):
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
    assert (dist / "lex-cli_1.0.0-1_amd64.deb").read_bytes() == b"deb"
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
    assert (dist / "pkg-1.0.0-py3-none-any.whl").read_bytes() == b"pkg"
    assert not (dist / ".tmp-pkg").exists()


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
    assert (out / "Phos.app/Contents/MacOS/phos").is_file()
    assert (out / "Phos.app/Contents/Current").is_symlink()
    assert (out / "Phos_1.0.0_aarch64.dmg").is_file()
    assert composed == bundle_mod.Composed(
        "app",
        "mac-app",
        ("Phos.app", "Phos_1.0.0_aarch64.dmg", "app.unsigned-app.tar.gz"),
    )


def test_mac_app_without_the_reseal_payload_is_a_bundle_failure(tmp_path):
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


def test_mac_app_counts_only_the_top_level_app(tmp_path):
    (artifact,) = _artifacts(MAC_APP_SPEC)

    def nested_helper(argv, cwd):
        _bundler_effect(tmp_path)(argv, cwd)
        source = tmp_path / "src-tauri/target/release/bundle"
        helper = (
            source
            / "macos"
            / "Phos.app"
            / "Contents"
            / "Frameworks"
            / "Phos Helper.app"
            / "Contents"
            / "MacOS"
        )
        _executable(helper / "Phos Helper")

    recorder = RunRecorder({"npm": nested_helper, "tar": _tar_effect})
    composed = bundle_mod.MAC_APP.compose(
        _request(tmp_path, artifact, (), target=MAC, run_cmd=recorder)
    )
    assert composed == bundle_mod.Composed(
        "app",
        "mac-app",
        ("Phos.app", "Phos_1.0.0_aarch64.dmg", "app.unsigned-app.tar.gz"),
    )
    assert (tmp_path / "dist" / "Phos.app/Contents/Frameworks/Phos Helper.app").is_dir()


TAURI_SPEC = {
    "app": {
        "build": ["rust", "npm"],
        "bundle": {
            "composition": "tauri",
            "command": ["npm", "run", "tauri", "build"],
            "source": "src-tauri/target/release/bundle",
        },
    }
}


def _tauri_linux_effect(
    root, appimage="Phos_1.0.0_amd64.AppImage", deb="Phos_1.0.0_amd64.deb"
):

    def effect(argv, cwd):
        source = root / "src-tauri/target/release/bundle"
        (source / "appimage").mkdir(parents=True, exist_ok=True)
        (source / "appimage" / appimage).write_bytes(b"appimage")
        (source / "deb").mkdir(parents=True, exist_ok=True)
        (source / "deb" / deb).write_bytes(b"deb")

    return effect


def test_tauri_darwin_emits_the_pair_and_the_reseal_payload(tmp_path):
    (artifact,) = _artifacts(TAURI_SPEC)
    recorder = RunRecorder({"npm": _bundler_effect(tmp_path), "tar": _tar_effect})

    composed = bundle_mod.TAURI.compose(
        _request(tmp_path, artifact, (), target=MAC, run_cmd=recorder)
    )

    source = tmp_path / "src-tauri/target/release/bundle"
    out = tmp_path / "dist"
    assert recorder.calls == [
        (("npm", "run", "tauri", "build"), tmp_path),
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
    assert (out / "Phos.app/Contents/MacOS/phos").is_file()
    assert (out / "Phos.app/Contents/Current").is_symlink()
    assert (out / "Phos_1.0.0_aarch64.dmg").is_file()
    assert composed == bundle_mod.Composed(
        "app",
        "tauri",
        ("Phos.app", "Phos_1.0.0_aarch64.dmg", "app.unsigned-app.tar.gz"),
    )


def test_tauri_linux_collects_the_appimage_and_deb(tmp_path):
    (artifact,) = _artifacts(TAURI_SPEC)
    recorder = RunRecorder({"npm": _tauri_linux_effect(tmp_path)})

    composed = bundle_mod.TAURI.compose(
        _request(tmp_path, artifact, (), target=LINUX, run_cmd=recorder)
    )

    out = tmp_path / "dist"
    assert recorder.calls == [(("npm", "run", "tauri", "build"), tmp_path)]
    assert (out / "Phos_1.0.0_amd64.AppImage").is_file()
    assert (out / "Phos_1.0.0_amd64.deb").is_file()
    assert composed == bundle_mod.Composed(
        "app",
        "tauri",
        ("Phos_1.0.0_amd64.AppImage", "Phos_1.0.0_amd64.deb"),
    )


def test_tauri_linux_hard_fails_when_no_bundle_appears(tmp_path):
    (artifact,) = _artifacts(TAURI_SPEC)
    recorder = RunRecorder()
    with pytest.raises(ReleaseError, match=r"left no \*\.AppImage/\*\.deb bundle"):
        bundle_mod.TAURI.compose(
            _request(tmp_path, artifact, (), target=LINUX, run_cmd=recorder)
        )


def test_tauri_linux_rerun_overwrites_existing_same_named_bundles(tmp_path):
    (artifact,) = _artifacts(TAURI_SPEC)
    recorder = RunRecorder({"npm": _tauri_linux_effect(tmp_path)})
    for _ in range(2):
        composed = bundle_mod.TAURI.compose(
            _request(tmp_path, artifact, (), target=LINUX, run_cmd=recorder)
        )
    assert composed.outputs == (
        "Phos_1.0.0_amd64.AppImage",
        "Phos_1.0.0_amd64.deb",
    )


def test_tauri_linux_does_not_delete_under_source_and_ignores_stray_files(tmp_path):
    (artifact,) = _artifacts(TAURI_SPEC)
    source = tmp_path / "src-tauri/target/release/bundle"
    (source / "src").mkdir(parents=True)
    stray = source / "src" / "keep_me.AppImage"
    stray.write_bytes(b"not a bundle")

    recorder = RunRecorder({"npm": _tauri_linux_effect(tmp_path)})
    composed = bundle_mod.TAURI.compose(
        _request(tmp_path, artifact, (), target=LINUX, run_cmd=recorder)
    )

    out = tmp_path / "dist"
    assert composed.outputs == (
        "Phos_1.0.0_amd64.AppImage",
        "Phos_1.0.0_amd64.deb",
    )
    assert stray.read_bytes() == b"not a bundle"
    assert not (out / "keep_me.AppImage").exists()


def test_tauri_linux_hard_fails_on_a_stale_prior_bundle(tmp_path):
    (artifact,) = _artifacts(TAURI_SPEC)
    source = tmp_path / "src-tauri/target/release/bundle"

    def stale_then_fresh(argv, cwd):
        _tauri_linux_effect(tmp_path)(argv, cwd)
        (source / "appimage" / "Phos_0.9.0_amd64.AppImage").write_bytes(b"stale")

    recorder = RunRecorder({"npm": stale_then_fresh})
    with pytest.raises(ReleaseError, match=r"expected exactly one"):
        bundle_mod.TAURI.compose(
            _request(tmp_path, artifact, (), target=LINUX, run_cmd=recorder)
        )
    assert (source / "appimage" / "Phos_0.9.0_amd64.AppImage").exists()
    assert (source / "appimage" / "Phos_1.0.0_amd64.AppImage").exists()


def test_tauri_darwin_hard_fails_on_a_stale_prior_pair(tmp_path):
    (artifact,) = _artifacts(TAURI_SPEC)
    source = tmp_path / "src-tauri/target/release/bundle"

    def stale_then_fresh(argv, cwd):
        _bundler_effect(tmp_path)(argv, cwd)
        (source / "macos" / "Stale.app").mkdir(parents=True)
        (source / "dmg" / "Stale_0.9.0_aarch64.dmg").write_bytes(b"stale")

    recorder = RunRecorder({"npm": stale_then_fresh, "tar": _tar_effect})
    with pytest.raises(ReleaseError, match=r"exactly one coupled \.app/\.dmg pair"):
        bundle_mod.TAURI.compose(
            _request(tmp_path, artifact, (), target=MAC, run_cmd=recorder)
        )
    assert (source / "macos" / "Stale.app").exists()


def test_tauri_darwin_requires_exactly_one_coupled_pair(tmp_path):
    (artifact,) = _artifacts(TAURI_SPEC)

    def two_apps(argv, cwd):
        _bundler_effect(tmp_path)(argv, cwd)
        _bundler_effect(tmp_path, app="Other.app")(argv, cwd)

    recorder = RunRecorder({"npm": two_apps, "tar": _tar_effect})
    with pytest.raises(ReleaseError, match=r"exactly one coupled \.app/\.dmg pair"):
        bundle_mod.TAURI.compose(
            _request(tmp_path, artifact, (), target=MAC, run_cmd=recorder)
        )


def test_tauri_skips_the_windows_target(tmp_path):
    assert bundle_mod.TAURI.applies(MAC)
    assert bundle_mod.TAURI.applies(LINUX)
    assert not bundle_mod.TAURI.applies(WIN)


ELECTRON_SPEC = {
    "app": {
        "build": ["npm"],
        "bundle": {
            "composition": "electron",
            "command": ["npm", "run", "dist"],
            "source": "release",
        },
    }
}


def _electron_darwin_effect(root, product="Lexed", exe="lexed", ver="1.2.3"):

    def effect(argv, cwd):
        rel = root / "release"
        rel.mkdir(parents=True, exist_ok=True)
        (rel / f"{product}-{ver}-arm64.dmg").write_bytes(b"dmg")
        (rel / f"{product}-{ver}-arm64.dmg.blockmap").write_bytes(b"blockmap")
        app = rel / "mac-arm64" / f"{product}.app"
        _executable(app / "Contents" / "MacOS" / exe)
        (app / "Contents" / "Current").symlink_to("MacOS")
        helper = app / "Contents" / "Frameworks" / f"{product} Helper.app"
        _executable(helper / "Contents" / "MacOS" / f"{product} Helper")

    return effect


def _electron_linux_effect(root, product="Lexed", ver="1.2.3"):

    def effect(argv, cwd):
        rel = root / "release"
        rel.mkdir(parents=True, exist_ok=True)
        (rel / f"{product}-{ver}.AppImage").write_bytes(b"appimage")
        (rel / f"{product}-{ver}.AppImage.blockmap").write_bytes(b"blockmap")
        _executable(rel / "mac-arm64" / f"{product}.app" / "Contents" / "MacOS" / "x")

    return effect


def test_electron_darwin_stages_the_dmg_app_and_reseal_payload(tmp_path):
    (artifact,) = _artifacts(ELECTRON_SPEC)
    recorder = RunRecorder(
        {"npm": _electron_darwin_effect(tmp_path), "tar": _tar_effect}
    )

    composed = bundle_mod.ELECTRON.compose(
        _request(tmp_path, artifact, (), target=MAC, run_cmd=recorder)
    )

    out = tmp_path / "dist"
    source = tmp_path / "release"
    assert recorder.calls == [
        (("npm", "run", "dist"), tmp_path),
        (
            (
                "tar",
                "-czf",
                str(out / "app.unsigned-app.tar.gz"),
                "-C",
                str(source / "mac-arm64"),
                "Lexed.app",
            ),
            tmp_path,
        ),
    ]
    assert (out / "Lexed-1.2.3-arm64.dmg").is_file()
    assert (out / "Lexed-1.2.3-arm64.dmg.blockmap").is_file()
    assert (out / "Lexed.app/Contents/MacOS/lexed").is_file()
    assert (out / "Lexed.app/Contents/Current").is_symlink()
    assert (out / "Lexed.app/Contents/Frameworks/Lexed Helper.app").is_dir()
    assert composed == bundle_mod.Composed(
        "app",
        "electron",
        (
            "Lexed-1.2.3-arm64.dmg",
            "Lexed-1.2.3-arm64.dmg.blockmap",
            "Lexed.app",
            "app.unsigned-app.tar.gz",
        ),
    )


def test_electron_darwin_without_a_top_level_app_is_a_bundle_failure(tmp_path):
    (artifact,) = _artifacts(ELECTRON_SPEC)

    def effect(argv, cwd):
        rel = tmp_path / "release"
        rel.mkdir(parents=True, exist_ok=True)
        (rel / "Lexed-1.2.3-arm64.dmg").write_bytes(b"dmg")

    recorder = RunRecorder({"npm": effect, "tar": _tar_effect})
    with pytest.raises(ReleaseError, match="exactly one top-level .app"):
        bundle_mod.ELECTRON.compose(
            _request(tmp_path, artifact, (), target=MAC, run_cmd=recorder)
        )


def test_electron_darwin_with_several_dmgs_is_a_bundle_failure(tmp_path):
    (artifact,) = _artifacts(ELECTRON_SPEC)

    def effect(argv, cwd):
        rel = tmp_path / "release"
        rel.mkdir(parents=True, exist_ok=True)
        (rel / "Lexed-1.2.3-arm64.dmg").write_bytes(b"dmg")
        (rel / "Lexed-1.2.3-x64.dmg").write_bytes(b"dmg")

    recorder = RunRecorder({"npm": effect, "tar": _tar_effect})
    with pytest.raises(ReleaseError, match=r"needs exactly one \.dmg to reseal"):
        bundle_mod.ELECTRON.compose(
            _request(tmp_path, artifact, (), target=MAC, run_cmd=recorder)
        )


def test_electron_linux_collects_the_appimage_and_no_app(tmp_path):
    (artifact,) = _artifacts(ELECTRON_SPEC)
    recorder = RunRecorder({"npm": _electron_linux_effect(tmp_path)})

    composed = bundle_mod.ELECTRON.compose(
        _request(tmp_path, artifact, (), target=LINUX, run_cmd=recorder)
    )

    out = tmp_path / "dist"
    assert (out / "Lexed-1.2.3.AppImage").is_file()
    assert (out / "Lexed-1.2.3.AppImage.blockmap").is_file()
    assert not (out / "Lexed.app").exists()
    assert composed == bundle_mod.Composed(
        "app",
        "electron",
        ("Lexed-1.2.3.AppImage", "Lexed-1.2.3.AppImage.blockmap"),
    )


def test_electron_windows_collects_the_exe_and_blockmap(tmp_path):
    (artifact,) = _artifacts(ELECTRON_SPEC)

    def effect(argv, cwd):
        rel = tmp_path / "release"
        rel.mkdir(parents=True, exist_ok=True)
        (rel / "Lexed Setup 1.2.3.exe").write_bytes(b"exe")
        (rel / "Lexed Setup 1.2.3.exe.blockmap").write_bytes(b"blockmap")

    recorder = RunRecorder({"npm": effect})
    composed = bundle_mod.ELECTRON.compose(
        _request(tmp_path, artifact, (), target=WIN, run_cmd=recorder)
    )
    out = tmp_path / "dist"
    assert (out / "Lexed Setup 1.2.3.exe").is_file()
    assert (out / "Lexed Setup 1.2.3.exe.blockmap").is_file()
    assert composed.outputs == (
        "Lexed Setup 1.2.3.exe",
        "Lexed Setup 1.2.3.exe.blockmap",
    )


def test_electron_missing_the_primary_distributable_is_a_bundle_failure(tmp_path):
    (artifact,) = _artifacts(ELECTRON_SPEC)
    recorder = RunRecorder({"npm": lambda argv, cwd: None})
    with pytest.raises(ReleaseError, match=r"produced no \.dmg"):
        bundle_mod.ELECTRON.compose(
            _request(tmp_path, artifact, (), target=MAC, run_cmd=recorder)
        )


def test_electron_skips_a_stale_blockmap_with_no_matching_distributable(tmp_path):
    (artifact,) = _artifacts(ELECTRON_SPEC)

    def effect(argv, cwd):
        rel = tmp_path / "release"
        rel.mkdir(parents=True, exist_ok=True)
        (rel / "Lexed-1.2.3.AppImage").write_bytes(b"img")
        (rel / "Lexed-1.2.3.AppImage.blockmap").write_bytes(b"map")
        (rel / "Stale-0.9.0.AppImage.blockmap").write_bytes(b"stale")

    composed = bundle_mod.ELECTRON.compose(
        _request(
            tmp_path, artifact, (), target=LINUX, run_cmd=RunRecorder({"npm": effect})
        )
    )
    assert composed.outputs == (
        "Lexed-1.2.3.AppImage",
        "Lexed-1.2.3.AppImage.blockmap",
    )
    assert not (tmp_path / "dist" / "Stale-0.9.0.AppImage.blockmap").exists()


def test_electron_refuses_a_source_that_is_the_bundle_output_tree(tmp_path):
    (artifact,) = _artifacts(
        {
            "app": {
                "build": ["npm"],
                "bundle": {
                    "composition": "electron",
                    "command": ["npm", "run", "dist"],
                    "source": "dist",
                },
            }
        }
    )

    def effect(argv, cwd):
        (tmp_path / "dist").mkdir(parents=True, exist_ok=True)
        (tmp_path / "dist" / "Lexed-1.2.3-arm64.dmg").write_bytes(b"dmg")

    with pytest.raises(ReleaseError, match=r"resolves to the bundle output tree"):
        bundle_mod.ELECTRON.compose(
            _request(
                tmp_path, artifact, (), target=MAC, run_cmd=RunRecorder({"npm": effect})
            )
        )


GRAMMAR_PAYLOAD = [
    {"path": "src", "required": True},
    {"path": "tree-sitter-lex.wasm", "required": True},
    {"path": "queries"},
    {"path": "grammar.js"},
    {"path": "shared/embedded-grammars.json"},
]


def _tarball_artifact(payload=None, leg="tree-sitter"):
    (artifact,) = _artifacts(
        {
            "parser": {
                "build": ["tree-sitter"],
                "bundle": {
                    "composition": "tarball",
                    "leg": leg,
                    "payload": payload if payload is not None else GRAMMAR_PAYLOAD,
                },
            }
        }
    )
    return artifact


def test_tarball_tars_exactly_the_declared_payload_that_is_present(tmp_path):
    artifact = _tarball_artifact()
    entries = _entries({".": "tree-sitter"})
    (tmp_path / "src").mkdir()
    (tmp_path / "src/parser.c").write_text("/* generated */")
    (tmp_path / "tree-sitter-lex.wasm").write_bytes(b"\x00asm")
    (tmp_path / "queries").mkdir()
    (tmp_path / "queries/highlights.scm").write_text(";; hi")
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared/embedded-grammars.json").write_text("{}")
    recorder = RunRecorder()

    composed = bundle_mod.TARBALL.compose(
        _request(tmp_path, artifact, entries, run_cmd=recorder)
    )

    archive = tmp_path / "dist" / "parser.tar.gz"
    assert recorder.calls == [
        (
            (
                "tar",
                "-czf",
                str(archive),
                "-C",
                str(tmp_path),
                "--",
                "src",
                "tree-sitter-lex.wasm",
                "queries",
                "shared/embedded-grammars.json",
            ),
            tmp_path,
        )
    ]
    assert composed == bundle_mod.Composed("parser", "tarball", ("parser.tar.gz",))


def test_tarball_reads_the_declared_leg_subdir(tmp_path):
    artifact = _tarball_artifact(payload=[{"path": "src", "required": True}])
    entries = _entries({"grammar": "tree-sitter"})
    (tmp_path / "grammar/src").mkdir(parents=True)
    (tmp_path / "grammar/src/parser.c").write_text("/* generated */")
    recorder = RunRecorder()

    bundle_mod.TARBALL.compose(_request(tmp_path, artifact, entries, run_cmd=recorder))

    ((argv, _cwd),) = recorder.calls
    assert argv == (
        "tar",
        "-czf",
        str(tmp_path / "dist" / "parser.tar.gz"),
        "-C",
        str(tmp_path / "grammar"),
        "--",
        "src",
    )


def test_tarball_missing_required_payload_refuses(tmp_path):
    artifact = _tarball_artifact()
    entries = _entries({".": "tree-sitter"})
    (tmp_path / "queries").mkdir()
    recorder = RunRecorder()

    with pytest.raises(ReleaseError) as excinfo:
        bundle_mod.TARBALL.compose(
            _request(tmp_path, artifact, entries, run_cmd=recorder)
        )

    message = str(excinfo.value)
    assert "required payload missing" in message
    assert "src" in message and "tree-sitter-lex.wasm" in message
    assert recorder.calls == []


def test_tarball_without_the_declared_leg_refuses(tmp_path):
    artifact = _tarball_artifact()
    entries = _entries({".": "rust"})
    with pytest.raises(ReleaseError, match="needs a .* tree-sitter leg"):
        bundle_mod.TARBALL.compose(
            _request(tmp_path, artifact, entries, run_cmd=RunRecorder())
        )


def test_tarball_rerun_unlinks_the_stale_archive(tmp_path):
    artifact = _tarball_artifact(payload=[{"path": "src", "required": True}])
    entries = _entries({".": "tree-sitter"})
    (tmp_path / "src").mkdir()
    (tmp_path / "src/parser.c").write_text("/* generated */")
    dist = tmp_path / "dist"
    dist.mkdir()
    stale = dist / "parser.tar.gz"
    stale.write_bytes(b"STALE")

    def _tar_writes(argv, cwd):
        Path(argv[2]).write_bytes(b"FRESH")

    recorder = RunRecorder({"tar": _tar_writes})
    bundle_mod.TARBALL.compose(_request(tmp_path, artifact, entries, run_cmd=recorder))
    assert stale.read_bytes() == b"FRESH"


def test_zed_is_the_same_declared_payload_composition(tmp_path):
    (artifact,) = _artifacts(
        {
            "zed-lex": {
                "build": ["rust"],
                "bundle": {
                    "composition": "zed",
                    "leg": "rust",
                    "payload": [
                        {"path": "extension.toml", "required": True},
                        {"path": "shared"},
                        {"path": "languages"},
                        {"path": "Cargo.toml"},
                    ],
                },
            }
        }
    )
    entries = _entries({"extension": "rust"})
    (tmp_path / "extension").mkdir()
    (tmp_path / "extension/extension.toml").write_text('id = "lex"\n')
    (tmp_path / "extension/shared").mkdir()
    (tmp_path / "extension/shared/g.scm").write_text(";; committed grammar asset")
    recorder = RunRecorder()

    composed = bundle_mod.ZED.compose(
        _request(tmp_path, artifact, entries, run_cmd=recorder)
    )

    assert recorder.calls == [
        (
            (
                "tar",
                "-czf",
                str(tmp_path / "dist" / "zed-lex.tar.gz"),
                "-C",
                str(tmp_path / "extension"),
                "--",
                "extension.toml",
                "shared",
            ),
            tmp_path,
        )
    ]
    assert composed == bundle_mod.Composed("zed-lex", "zed", ("zed-lex.tar.gz",))


def test_zed_missing_required_payload_refuses(tmp_path):
    (artifact,) = _artifacts(
        {
            "zed-lex": {
                "build": ["rust"],
                "bundle": {
                    "composition": "zed",
                    "leg": "rust",
                    "payload": [
                        {"path": "extension.toml", "required": True},
                        {"path": "shared"},
                    ],
                },
            }
        }
    )
    entries = _entries({".": "rust"})
    (tmp_path / "shared").mkdir()
    recorder = RunRecorder()
    with pytest.raises(ReleaseError, match="required payload missing"):
        bundle_mod.ZED.compose(_request(tmp_path, artifact, entries, run_cmd=recorder))
    assert recorder.calls == []


def test_declared_payload_composes_a_real_tarball_holding_exactly_the_payload(
    tmp_path,
):
    artifact = _tarball_artifact()
    entries = _entries({"grammar": "tree-sitter"})
    leg = tmp_path / "grammar"
    (leg / "src").mkdir(parents=True)
    (leg / "src/parser.c").write_text("/* generated */")
    (leg / "tree-sitter-lex.wasm").write_bytes(b"\x00asm\x01")
    (leg / "queries").mkdir()
    (leg / "queries/highlights.scm").write_text(";; hi")
    (leg / "shared").mkdir()
    (leg / "shared/embedded-grammars.json").write_text('{"lex": true}')
    (leg / "shared/NOT-DECLARED.txt").write_text("must not ship")
    (leg / "node_modules").mkdir()
    (leg / "node_modules/junk.js").write_text("// junk")

    def _real_run(argv, cwd):
        return execrun.run([str(a) for a in argv], cwd=cwd)

    composed = bundle_mod.TARBALL.compose(
        _request(tmp_path, artifact, entries, run_cmd=_real_run)
    )

    archive = tmp_path / "dist" / composed.outputs[0]
    with tarfile.open(archive, "r:gz") as tar:
        members = sorted(
            name
            for name in tar.getnames()
            if not PurePosixPath(name).name.startswith("._")
        )
    assert members == [
        "queries",
        "queries/highlights.scm",
        "shared/embedded-grammars.json",
        "src",
        "src/parser.c",
        "tree-sitter-lex.wasm",
    ]
    with tarfile.open(archive, "r:gz") as tar:
        wasm = tar.extractfile("tree-sitter-lex.wasm")
        assert wasm is not None and wasm.read() == b"\x00asm\x01"


def test_declared_payload_refuses_a_path_through_a_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("HOST SECRET")
    leg = tmp_path / "grammar"
    (leg / "src").mkdir(parents=True)
    (leg / "src/parser.c").write_text("/* generated */")
    (leg / "leak").symlink_to(outside, target_is_directory=True)
    assert (leg / "leak/secret.txt").read_text() == "HOST SECRET"

    artifact = _tarball_artifact(
        payload=[
            {"path": "src", "required": True},
            {"path": "leak/secret.txt"},
        ]
    )
    entries = _entries({"grammar": "tree-sitter"})

    def _real_run(argv, cwd):
        return execrun.run([str(a) for a in argv], cwd=cwd)

    with pytest.raises(ReleaseError) as excinfo:
        bundle_mod.TARBALL.compose(
            _request(tmp_path, artifact, entries, run_cmd=_real_run)
        )

    message = str(excinfo.value)
    assert "symlink or junction" in message
    assert "leak" in message
    assert not (tmp_path / "dist" / "parser.tar.gz").exists()


def test_declared_payload_refuses_a_symlink_leaf_even_when_optional(tmp_path):
    outside = tmp_path / "outside"
    (outside / "queries").mkdir(parents=True)
    leg = tmp_path / "grammar"
    (leg / "src").mkdir(parents=True)
    (leg / "queries").symlink_to(outside / "queries", target_is_directory=True)
    artifact = _tarball_artifact(
        payload=[{"path": "src", "required": True}, {"path": "queries"}]
    )
    recorder = RunRecorder()

    with pytest.raises(ReleaseError, match="symlink or junction"):
        bundle_mod.TARBALL.compose(
            _request(
                tmp_path,
                artifact,
                _entries({"grammar": "tree-sitter"}),
                run_cmd=recorder,
            )
        )
    assert recorder.calls == []


def test_declared_payload_refuses_a_leg_pointed_through_a_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "passwd").write_text("HOST SECRET")
    (tmp_path / "grammar").symlink_to(outside, target_is_directory=True)
    assert (tmp_path / "grammar/passwd").read_text() == "HOST SECRET"

    artifact = _tarball_artifact(payload=[{"path": "passwd", "required": True}])
    entries = _entries({"grammar": "tree-sitter"})

    def _real_run(argv, cwd):
        return execrun.run([str(a) for a in argv], cwd=cwd)

    with pytest.raises(ReleaseError) as excinfo:
        bundle_mod.TARBALL.compose(
            _request(tmp_path, artifact, entries, run_cmd=_real_run)
        )

    message = str(excinfo.value)
    assert "symlink or junction" in message
    assert "grammar" in message
    assert not (tmp_path / "dist" / "parser.tar.gz").exists()


def test_declared_payload_refuses_a_leg_symlink_component_mid_path(tmp_path):
    outside = tmp_path / "outside"
    (outside / "src").mkdir(parents=True)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub/leg").symlink_to(outside, target_is_directory=True)
    spec = config.BundleSpec(
        composition="tarball",
        leg="tree-sitter",
        payload=(config.PayloadEntry(path="src", required=True),),
    )
    with pytest.raises(ReleaseError, match="traverses a symlink or junction"):
        bundle_mod._payload_operands("parser", spec, tmp_path, "sub/leg")


def test_declared_payload_operands_can_never_be_read_as_tar_options(tmp_path):
    leg = tmp_path / "grammar"
    (leg / "src").mkdir(parents=True)
    (leg / "src/parser.c").write_text("/* generated */")
    (leg / "--checkpoint=1").write_text("innocent bytes")
    (leg / "--checkpoint-action=exec=touch pwned").write_text("innocent bytes")
    artifact = _tarball_artifact(
        payload=[
            {"path": "src", "required": True},
            {"path": "--checkpoint=1"},
            {"path": "--checkpoint-action=exec=touch pwned"},
        ]
    )

    seen = []

    def _real_run(argv, cwd):
        argv = [str(a) for a in argv]
        seen.append(argv)
        return execrun.run(argv, cwd=cwd)

    composed = bundle_mod.TARBALL.compose(
        _request(
            tmp_path, artifact, _entries({"grammar": "tree-sitter"}), run_cmd=_real_run
        )
    )

    assert list(tmp_path.rglob("pwned")) == []
    (argv,) = seen
    assert argv[argv.index("--") + 1 :] == [
        "src",
        "--checkpoint=1",
        "--checkpoint-action=exec=touch pwned",
    ]
    archive = tmp_path / "dist" / composed.outputs[0]
    with tarfile.open(archive, "r:gz") as tar:
        members = {
            name
            for name in tar.getnames()
            if not PurePosixPath(name).name.startswith("._")
        }
    assert "--checkpoint=1" in members
    assert "--checkpoint-action=exec=touch pwned" in members


def test_declared_payload_refuses_a_leg_relative_escape_at_runtime(tmp_path):
    spec = config.BundleSpec(
        composition="tarball",
        leg="tree-sitter",
        payload=(config.PayloadEntry(path="../outside", required=True),),
    )
    (tmp_path / "outside").mkdir()
    (tmp_path / "grammar").mkdir()
    with pytest.raises(ReleaseError, match="not a leg-relative path"):
        bundle_mod._payload_operands("parser", spec, tmp_path, "grammar")


def test_declared_payload_compositions_share_one_compose(tmp_path):
    assert bundle_mod.declared_payload_names() == ("tarball", "zed")
    assert bundle_mod.TARBALL.compose is bundle_mod.ZED.compose


WASM_SPEC = {
    "wasm": {
        "build": ["rust"],
        "bundle": {"composition": "wasm-pack", "scope": "lex-fmt"},
    }
}


def _wasm_pack_effect(name="package.json"):

    def effect(argv, cwd):
        out = Path(argv[argv.index("--out-dir") + 1])
        out.mkdir(parents=True, exist_ok=True)
        (out / name).write_text('{"name": "@lex-fmt/lex-wasm"}', encoding="utf-8")

    return effect


def _npm_pack_effect(tarball="lex-fmt-lex-wasm-1.2.3.tgz"):

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
        (("npm", "pack", "--ignore-scripts", "--pack-destination", str(scratch)), pkg),
    ]
    assert composed == bundle_mod.Composed(
        "wasm", "wasm-pack", ("lex-fmt-lex-wasm-1.2.3.tgz",)
    )
    assert (dist / "lex-fmt-lex-wasm-1.2.3.tgz").is_file()
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
    assert "--scope" not in wasm_argv
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
    assert not (tmp_path / "dist" / ".pkg-wasm").exists()


def test_wasm_pack_hard_fails_when_npm_pack_yields_no_tarball(tmp_path):
    (artifact,) = _artifacts(WASM_SPEC)
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
    with pytest.raises(
        ReleaseError, match=r"wasm-pack composition needs a \[toolchains\] rust leg"
    ):
        bundle_mod.WASM_PACK.compose(
            _request(
                tmp_path, artifact, _entries({".": "python"}), run_cmd=RunRecorder()
            )
        )


def _cargo_metadata_effect(manifests: dict):

    def effect(argv, cwd):
        packages = [
            {"name": name, "manifest_path": str(path)}
            for name, path in manifests.items()
        ]
        return execrun.ExecResult(
            argv=tuple(argv),
            rc=0,
            stdout=json.dumps({"packages": packages}),
            stderr="",
            duration_ms=1,
        )

    return effect


def test_crate_dir_for_package_resolves_the_manifest_dir():
    metadata = {
        "packages": [
            {"name": "phos-color", "manifest_path": "/repo/phos-color/Cargo.toml"},
            {
                "name": "phos-color-wasm",
                "manifest_path": "/repo/phos-color-wasm/Cargo.toml",
            },
            {"name": "phos-viewer", "manifest_path": "/repo/phos-viewer/Cargo.toml"},
        ]
    }
    assert bundle_mod.crate_dir_for_package(metadata, "phos-color-wasm") == Path(
        "/repo/phos-color-wasm"
    )
    nested = {
        "packages": [
            {"name": "lex-wasm", "manifest_path": "/l/crates/lex-wasm/Cargo.toml"}
        ]
    }
    assert bundle_mod.crate_dir_for_package(nested, "lex-wasm") == Path(
        "/l/crates/lex-wasm"
    )


def test_crate_dir_for_package_is_none_when_the_package_is_absent():
    metadata = {
        "packages": [{"name": "other", "manifest_path": "/repo/other/Cargo.toml"}]
    }
    assert bundle_mod.crate_dir_for_package(metadata, "phos-color-wasm") is None


def test_wasm_pack_runs_in_the_declared_packages_crate_dir(tmp_path):
    (artifact,) = _artifacts(
        {
            "wasm": {
                "build": [{"toolchain": "rust", "package": "phos-color-wasm"}],
                "bundle": {"composition": "wasm-pack", "scope": "phos"},
            }
        }
    )
    crate_dir = tmp_path / "phos-color-wasm"
    recorder = RunRecorder(
        {
            "cargo": _cargo_metadata_effect(
                {"phos-color-wasm": crate_dir / "Cargo.toml"}
            ),
            "wasm-pack": _wasm_pack_effect(),
            "npm": _npm_pack_effect("phos-phos-color-wasm-1.2.3.tgz"),
        }
    )

    bundle_mod.WASM_PACK.compose(
        _request(tmp_path, artifact, _entries({".": "rust"}), run_cmd=recorder)
    )

    assert recorder.calls[0] == (
        ("cargo", "metadata", "--format-version", "1", "--no-deps"),
        tmp_path,
    )
    wasm_argv, wasm_cwd = recorder.calls[1]
    assert wasm_argv[0] == "wasm-pack"
    assert wasm_cwd == crate_dir
    assert wasm_cwd != tmp_path


def test_wasm_pack_without_a_declared_package_skips_cargo_metadata(tmp_path):
    (artifact,) = _artifacts(WASM_SPEC)
    recorder = RunRecorder(
        {"wasm-pack": _wasm_pack_effect(), "npm": _npm_pack_effect()}
    )

    bundle_mod.WASM_PACK.compose(
        _request(
            tmp_path, artifact, _entries({"crates/lex-wasm": "rust"}), run_cmd=recorder
        )
    )

    assert "cargo" not in recorder.heads
    wasm_argv, wasm_cwd = recorder.calls[0]
    assert wasm_argv[0] == "wasm-pack"
    assert wasm_cwd == tmp_path / "crates/lex-wasm"


def test_wasm_pack_hard_fails_when_the_declared_package_is_unknown(tmp_path):
    (artifact,) = _artifacts(
        {
            "wasm": {
                "build": [{"toolchain": "rust", "package": "does-not-exist"}],
                "bundle": {"composition": "wasm-pack"},
            }
        }
    )
    recorder = RunRecorder(
        {"cargo": _cargo_metadata_effect({"other": tmp_path / "other/Cargo.toml"})}
    )
    with pytest.raises(ReleaseError, match=r"names no crate in the `cargo metadata`"):
        bundle_mod.WASM_PACK.compose(
            _request(tmp_path, artifact, _entries({".": "rust"}), run_cmd=recorder)
        )
    assert not (tmp_path / "dist" / ".pkg-wasm").exists()


def _vsce_writes_out(argv, cwd):
    out = argv[argv.index("--out") + 1]
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_bytes(b"PK\x03\x04")


def test_vsce_target_maps_every_shipped_platform_and_refuses_the_rest():
    assert bundle_mod.vsce_target(MAC) == "darwin-arm64"
    assert bundle_mod.vsce_target("x86_64-apple-darwin") == "darwin-x64"
    assert bundle_mod.vsce_target(LINUX) == "linux-x64"
    assert bundle_mod.vsce_target("aarch64-unknown-linux-gnu") == "linux-arm64"
    assert bundle_mod.vsce_target("x86_64-unknown-linux-musl") == "alpine-x64"
    assert bundle_mod.vsce_target(WIN) == "win32-x64"
    with pytest.raises(ReleaseError, match="no VS Code marketplace target"):
        bundle_mod.vsce_target("riscv64gc-unknown-linux-gnu")


def test_vsix_packages_per_target_into_the_out_tree(tmp_path):
    (artifact,) = _artifacts(
        {"ext": {"build": ["npm"], "bundle": {"composition": "vsix"}}}
    )
    entries = _entries({"editors/vscode": "npm"})
    recorder = RunRecorder({"npm": _vsce_writes_out})

    composed = bundle_mod.VSIX.compose(
        _request(tmp_path, artifact, entries, target=MAC, run_cmd=recorder)
    )

    out_path = tmp_path / "dist" / "ext-darwin-arm64.vsix"
    assert recorder.calls == [
        (
            (
                "npm",
                "exec",
                "--",
                "vsce",
                "package",
                "--no-dependencies",
                "--target",
                "darwin-arm64",
                "--out",
                str(out_path),
            ),
            tmp_path / "editors/vscode",
        )
    ]
    assert out_path.is_file()
    assert composed == bundle_mod.Composed("ext", "vsix", ("ext-darwin-arm64.vsix",))


@pytest.mark.parametrize(
    ("target", "vt"),
    [(MAC, "darwin-arm64"), (LINUX, "linux-x64"), (WIN, "win32-x64")],
)
def test_vsix_always_packs_with_no_dependencies(tmp_path, target, vt):
    (artifact,) = _artifacts(
        {"ext": {"build": ["npm"], "bundle": {"composition": "vsix"}}}
    )
    entries = _entries({"editors/vscode": "npm"})
    recorder = RunRecorder({"npm": _vsce_writes_out})

    bundle_mod.VSIX.compose(
        _request(tmp_path, artifact, entries, target=target, run_cmd=recorder)
    )

    ((argv, _cwd),) = recorder.calls
    assert argv == (
        "npm",
        "exec",
        "--",
        "vsce",
        "package",
        "--no-dependencies",
        "--target",
        vt,
        "--out",
        str(tmp_path / "dist" / f"ext-{vt}.vsix"),
    )


def test_vsix_windows_target_maps_to_win32_x64(tmp_path):
    (artifact,) = _artifacts(
        {"ext": {"build": ["npm"], "bundle": {"composition": "vsix"}}}
    )
    entries = _entries({"editors/vscode": "npm"})
    recorder = RunRecorder({"npm": _vsce_writes_out})

    composed = bundle_mod.VSIX.compose(
        _request(tmp_path, artifact, entries, target=WIN, run_cmd=recorder)
    )
    assert composed.outputs == ("ext-win32-x64.vsix",)
    assert (tmp_path / "dist" / "ext-win32-x64.vsix").is_file()


def test_vsix_no_output_is_a_hard_failure(tmp_path):
    (artifact,) = _artifacts(
        {"ext": {"build": ["npm"], "bundle": {"composition": "vsix"}}}
    )
    entries = _entries({"editors/vscode": "npm"})
    recorder = RunRecorder()
    with pytest.raises(ReleaseError, match="produced no ext-darwin-arm64.vsix"):
        bundle_mod.VSIX.compose(
            _request(tmp_path, artifact, entries, target=MAC, run_cmd=recorder)
        )


def test_vsix_without_an_npm_leg_refuses(tmp_path):
    (artifact,) = _artifacts(
        {"ext": {"build": ["npm"], "bundle": {"composition": "vsix"}}}
    )
    entries = _entries({".": "rust"})
    with pytest.raises(ReleaseError, match="needs a .* npm leg"):
        bundle_mod.VSIX.compose(
            _request(tmp_path, artifact, entries, target=MAC, run_cmd=RunRecorder())
        )


def _dep(package, *, repo="lex-fmt/lex", feature=None):
    return config.ArtifactDep(package=package, repo=repo, feature=feature)


def _materialize_dep_bin(tmp_path, dep, *, target=LINUX):
    return _executable(artifactdeps.materialized_bin_path(tmp_path, dep, target=target))


def test_vsix_stages_artifact_dep_native_into_the_layout_before_packaging(tmp_path):
    (artifact,) = _artifacts(
        {
            "ext": {
                "build": ["npm"],
                "bundle": {
                    "composition": "vsix",
                    "stage": {"lexd-lsp": "resources/lexd-lsp"},
                },
            }
        }
    )
    entries = _entries({"editors/vscode": "npm"})
    dep = _dep("lexd-lsp")
    src = _materialize_dep_bin(tmp_path, dep)
    src.write_bytes(b"NATIVE-LSP")
    src.chmod(src.stat().st_mode | 0o755)
    staged = tmp_path / "editors/vscode" / "resources/lexd-lsp"
    seen: dict = {}

    def _vsce_asserts_staged(argv, cwd):
        seen["exists"] = staged.is_file()
        seen["payload"] = staged.read_bytes()
        seen["runnable"] = os.name == "nt" or os.access(staged, os.X_OK)
        _vsce_writes_out(argv, cwd)

    recorder = RunRecorder({"npm": _vsce_asserts_staged})
    composed = bundle_mod.VSIX.compose(
        _request(
            tmp_path,
            artifact,
            entries,
            target=MAC,
            run_cmd=recorder,
            artifact_deps=(dep,),
        )
    )

    assert seen == {"exists": True, "payload": b"NATIVE-LSP", "runnable": True}
    assert not staged.exists()
    assert recorder.heads == ["npm"]
    assert composed == bundle_mod.Composed("ext", "vsix", ("ext-darwin-arm64.vsix",))


def test_vsix_stage_resolves_the_named_feature_env(tmp_path):
    (artifact,) = _artifacts(
        {
            "ext": {
                "build": ["npm"],
                "bundle": {
                    "composition": "vsix",
                    "stage": {"lexd-lsp": "resources/lexd-lsp"},
                },
            }
        }
    )
    entries = _entries({"editors/vscode": "npm"})
    dep = _dep("lexd-lsp", feature="lint")
    _materialize_dep_bin(tmp_path, dep)
    staged = tmp_path / "editors/vscode" / "resources/lexd-lsp"
    seen: dict = {}

    def _vsce(argv, cwd):
        seen["staged"] = staged.is_file()
        _vsce_writes_out(argv, cwd)

    recorder = RunRecorder({"npm": _vsce})
    bundle_mod.VSIX.compose(
        _request(
            tmp_path,
            artifact,
            entries,
            target=MAC,
            run_cmd=recorder,
            artifact_deps=(dep,),
        )
    )
    assert seen == {"staged": True}
    assert not staged.exists()


def test_vsix_win32_target_stages_scripts_exe_to_an_exe_dest(tmp_path):
    (artifact,) = _artifacts(
        {
            "ext": {
                "build": ["npm"],
                "bundle": {
                    "composition": "vsix",
                    "stage": {"lexd-lsp": "resources/lexd-lsp"},
                },
            }
        }
    )
    entries = _entries({"editors/vscode": "npm"})
    dep = _dep("lexd-lsp")
    src = _materialize_dep_bin(tmp_path, dep, target=WIN)
    assert src == tmp_path / ".pixi/envs/default/Scripts/lexd-lsp.exe"
    staged_exe = tmp_path / "editors/vscode" / "resources/lexd-lsp.exe"
    seen: dict = {}

    def _vsce(argv, cwd):
        seen["exe"] = staged_exe.is_file()
        seen["no_bare"] = not (
            tmp_path / "editors/vscode" / "resources/lexd-lsp"
        ).exists()
        _vsce_writes_out(argv, cwd)

    recorder = RunRecorder({"npm": _vsce})
    composed = bundle_mod.VSIX.compose(
        _request(
            tmp_path,
            artifact,
            entries,
            target=WIN,
            run_cmd=recorder,
            artifact_deps=(dep,),
        )
    )
    assert seen == {"exe": True, "no_bare": True}
    assert not staged_exe.exists()
    assert composed.outputs == ("ext-win32-x64.vsix",)


def test_vsix_stage_naming_an_undeclared_pin_refuses(tmp_path):
    (artifact,) = _artifacts(
        {
            "ext": {
                "build": ["npm"],
                "bundle": {
                    "composition": "vsix",
                    "stage": {"lexd-lsp": "resources/lexd-lsp"},
                },
            }
        }
    )
    entries = _entries({"editors/vscode": "npm"})
    recorder = RunRecorder({"npm": _vsce_writes_out})
    with pytest.raises(ReleaseError, match="no \\[artifact-deps.lexd-lsp\\] pin"):
        bundle_mod.VSIX.compose(
            _request(
                tmp_path,
                artifact,
                entries,
                target=MAC,
                run_cmd=recorder,
                artifact_deps=(),
            )
        )
    assert recorder.calls == []


def test_vsix_stage_missing_materialized_binary_points_at_install(tmp_path):
    (artifact,) = _artifacts(
        {
            "ext": {
                "build": ["npm"],
                "bundle": {
                    "composition": "vsix",
                    "stage": {"lexd-lsp": "resources/lexd-lsp"},
                },
            }
        }
    )
    entries = _entries({"editors/vscode": "npm"})
    dep = _dep("lexd-lsp")
    recorder = RunRecorder({"npm": _vsce_writes_out})
    with pytest.raises(ReleaseError, match="not materialized.*shipit install"):
        bundle_mod.VSIX.compose(
            _request(
                tmp_path,
                artifact,
                entries,
                target=MAC,
                run_cmd=recorder,
                artifact_deps=(dep,),
            )
        )
    assert recorder.calls == []


@pytest.mark.parametrize("kind", ["file", "dir"])
def test_vsix_stage_refuses_a_pre_existing_destination(tmp_path, kind):
    (artifact,) = _artifacts(
        {
            "ext": {
                "build": ["npm"],
                "bundle": {
                    "composition": "vsix",
                    "stage": {"lexd-lsp": "resources/lexd-lsp"},
                },
            }
        }
    )
    entries = _entries({"editors/vscode": "npm"})
    dep = _dep("lexd-lsp")
    _materialize_dep_bin(tmp_path, dep)
    tracked = tmp_path / "editors/vscode" / "resources/lexd-lsp"
    tracked.parent.mkdir(parents=True, exist_ok=True)
    if kind == "file":
        tracked.write_bytes(b"TRACKED-COMMITTED")
    else:
        tracked.mkdir()
    recorder = RunRecorder({"npm": _vsce_writes_out})
    with pytest.raises(ReleaseError, match="already exists"):
        bundle_mod.VSIX.compose(
            _request(
                tmp_path,
                artifact,
                entries,
                target=MAC,
                run_cmd=recorder,
                artifact_deps=(dep,),
            )
        )
    assert recorder.calls == []
    if kind == "file":
        assert tracked.read_bytes() == b"TRACKED-COMMITTED"
    else:
        assert tracked.is_dir()


def _vsix_stage_artifact():
    (artifact,) = _artifacts(
        {
            "ext": {
                "build": ["npm"],
                "bundle": {
                    "composition": "vsix",
                    "stage": {"lexd-lsp": "resources/lexd-lsp"},
                },
            }
        }
    )
    return artifact


def test_vsix_stage_refuses_a_dangling_symlink_dest(tmp_path):
    artifact = _vsix_stage_artifact()
    entries = _entries({"editors/vscode": "npm"})
    dep = _dep("lexd-lsp")
    _materialize_dep_bin(tmp_path, dep)
    dst = tmp_path / "editors/vscode" / "resources/lexd-lsp"
    dst.parent.mkdir(parents=True)
    dst.symlink_to(tmp_path / "does-not-exist")
    assert not dst.exists() and dst.is_symlink()
    recorder = RunRecorder({"npm": _vsce_writes_out})
    with pytest.raises(ReleaseError, match="already exists"):
        bundle_mod.VSIX.compose(
            _request(
                tmp_path,
                artifact,
                entries,
                target=MAC,
                run_cmd=recorder,
                artifact_deps=(dep,),
            )
        )
    assert recorder.calls == []
    assert dst.is_symlink()


def test_vsix_stage_refuses_a_symlinked_parent_escape(tmp_path):
    artifact = _vsix_stage_artifact()
    entries = _entries({"editors/vscode": "npm"})
    dep = _dep("lexd-lsp")
    _materialize_dep_bin(tmp_path, dep)
    outside = tmp_path / "outside"
    outside.mkdir()
    leg_dir = tmp_path / "editors/vscode"
    leg_dir.mkdir(parents=True)
    (leg_dir / "resources").symlink_to(outside)
    recorder = RunRecorder({"npm": _vsce_writes_out})
    with pytest.raises(ReleaseError, match="resolves outside"):
        bundle_mod.VSIX.compose(
            _request(
                tmp_path,
                artifact,
                entries,
                target=MAC,
                run_cmd=recorder,
                artifact_deps=(dep,),
            )
        )
    assert recorder.calls == []
    assert not (outside / "lexd-lsp").exists()
    assert (leg_dir / "resources").is_symlink()


def test_vsix_stage_cleans_up_a_partial_copy_that_dies_mid_write(tmp_path, monkeypatch):
    artifact = _vsix_stage_artifact()
    entries = _entries({"editors/vscode": "npm"})
    dep = _dep("lexd-lsp")
    _materialize_dep_bin(tmp_path, dep)
    leg_dir = tmp_path / "editors/vscode"
    leg_dir.mkdir(parents=True)
    staged = leg_dir / "resources/lexd-lsp"

    def _copy2_dies(src, dst):
        Path(dst).write_bytes(b"PARTIAL")
        raise OSError("disk full")

    monkeypatch.setattr(bundle_mod.shutil, "copy2", _copy2_dies)
    recorder = RunRecorder({"npm": _vsce_writes_out})
    with pytest.raises(OSError, match="disk full"):
        bundle_mod.VSIX.compose(
            _request(
                tmp_path,
                artifact,
                entries,
                target=MAC,
                run_cmd=recorder,
                artifact_deps=(dep,),
            )
        )
    assert recorder.calls == []
    assert not staged.exists()
    assert not (leg_dir / "resources").exists()


def test_vsix_stage_leaves_no_empty_dir_behind(tmp_path):
    (artifact,) = _artifacts(
        {
            "ext": {
                "build": ["npm"],
                "bundle": {
                    "composition": "vsix",
                    "stage": {"lexd-lsp": "resources/nested/lexd-lsp"},
                },
            }
        }
    )
    entries = _entries({"editors/vscode": "npm"})
    dep = _dep("lexd-lsp")
    _materialize_dep_bin(tmp_path, dep)
    leg_dir = tmp_path / "editors/vscode"
    leg_dir.mkdir(parents=True)
    (leg_dir / "package.json").write_text("{}")
    recorder = RunRecorder({"npm": _vsce_writes_out})

    composed = bundle_mod.VSIX.compose(
        _request(
            tmp_path,
            artifact,
            entries,
            target=MAC,
            run_cmd=recorder,
            artifact_deps=(dep,),
        )
    )
    assert composed.outputs == ("ext-darwin-arm64.vsix",)
    assert not (leg_dir / "resources").exists()
    assert [p.name for p in leg_dir.iterdir()] == ["package.json"]


def test_vsix_stage_intermediate_component_is_a_file_raises_release_error(tmp_path):
    (artifact,) = _artifacts(
        {
            "ext": {
                "build": ["npm"],
                "bundle": {
                    "composition": "vsix",
                    "stage": {"lexd-lsp": "resources/nested/lexd-lsp"},
                },
            }
        }
    )
    entries = _entries({"editors/vscode": "npm"})
    dep = _dep("lexd-lsp")
    _materialize_dep_bin(tmp_path, dep)
    leg_dir = tmp_path / "editors/vscode"
    leg_dir.mkdir(parents=True)
    (leg_dir / "resources").write_text("i am a file, not a dir")
    recorder = RunRecorder({"npm": _vsce_writes_out})
    with pytest.raises(ReleaseError, match="intermediate path component is a file"):
        bundle_mod.VSIX.compose(
            _request(
                tmp_path,
                artifact,
                entries,
                target=MAC,
                run_cmd=recorder,
                artifact_deps=(dep,),
            )
        )
    assert recorder.calls == []
    assert (leg_dir / "resources").read_text() == "i am a file, not a dir"


def test_vsix_without_a_stage_map_stages_nothing(tmp_path):
    (artifact,) = _artifacts(
        {"ext": {"build": ["npm"], "bundle": {"composition": "vsix"}}}
    )
    entries = _entries({"editors/vscode": "npm"})
    recorder = RunRecorder({"npm": _vsce_writes_out})
    composed = bundle_mod.VSIX.compose(
        _request(tmp_path, artifact, entries, target=MAC, run_cmd=recorder)
    )
    assert recorder.heads == ["npm"]
    assert not (tmp_path / "editors/vscode" / "resources").exists()
    assert composed.outputs == ("ext-darwin-arm64.vsix",)


def test_registry_is_closed_and_platform_scoped():
    assert bundle_mod.names() == (
        "archive",
        "deb",
        "wheel",
        "wasm-pack",
        "vsix",
        "mac-app",
        "tauri",
        "electron",
        "tarball",
        "zed",
    )
    assert bundle_mod.composition("deb") is bundle_mod.DEB
    assert bundle_mod.composition("wasm-pack") is bundle_mod.WASM_PACK
    assert bundle_mod.composition("rpm") is None
    assert bundle_mod.ARCHIVE.applies(LINUX) and bundle_mod.ARCHIVE.applies(MAC)
    assert bundle_mod.DEB.applies(LINUX) and not bundle_mod.DEB.applies(MAC)
    assert bundle_mod.MAC_APP.applies(MAC) and not bundle_mod.MAC_APP.applies(LINUX)
    assert bundle_mod.WHEEL.applies(WIN)
    assert (
        bundle_mod.ELECTRON.applies(MAC)
        and bundle_mod.ELECTRON.applies(LINUX)
        and bundle_mod.ELECTRON.applies(WIN)
    )
    assert not bundle_mod.ELECTRON.applies("wasm32-unknown-unknown")
    assert bundle_mod.VSIX.applies(MAC)
    assert bundle_mod.VSIX.applies(LINUX)
    assert bundle_mod.VSIX.applies(WIN)
    assert bundle_mod.WASM_PACK.applies(LINUX) and bundle_mod.WASM_PACK.applies(MAC)
    assert bundle_mod.TAURI.applies(MAC) and bundle_mod.TAURI.applies(LINUX)
    assert not bundle_mod.TAURI.applies(WIN)
    assert bundle_mod.TARBALL.applies(LINUX) and bundle_mod.TARBALL.applies(MAC)
    assert bundle_mod.TARBALL.applies(WIN)
    assert not bundle_mod.TARBALL.signable


def test_source_compositions_do_not_assert_a_binary():
    assert bundle_mod.ARCHIVE.asserts_binary
    assert bundle_mod.DEB.asserts_binary
    assert bundle_mod.MAC_APP.asserts_binary
    assert bundle_mod.ELECTRON.asserts_binary
    assert not bundle_mod.WHEEL.asserts_binary
    assert not bundle_mod.TARBALL.asserts_binary
    assert not bundle_mod.WASM_PACK.asserts_binary
    assert not bundle_mod.VSIX.asserts_binary
    assert not bundle_mod.ZED.asserts_binary


def test_registry_marks_the_platform_independent_compositions():
    assert bundle_mod.platform_independent_names() == ("wasm-pack", "tarball", "zed")
    assert bundle_mod.TARBALL.platform_independent
    assert bundle_mod.WASM_PACK.platform_independent
    assert bundle_mod.ZED.platform_independent
    assert not bundle_mod.ARCHIVE.platform_independent


def test_registry_marks_the_signer_reopenable_compositions():
    assert bundle_mod.signable_names() == ("archive", "mac-app", "tauri", "electron")


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
    monkeypatch.setattr(release_verb.git, "repo_root", lambda *, cwd: str(tmp_path))
    return tmp_path


def test_bundle_walks_the_map_composing_skipping_and_passing_through(
    tmp_path, monkeypatch, capsys
):
    root = _repo(tmp_path, monkeypatch)
    _executable(root / f"target/{MAC}/release/lex")
    recorder = RunRecorder()

    rc = release_verb.run_bundle(target=MAC, run_cmd=recorder)

    assert rc == 0
    out = capsys.readouterr().out
    assert "bundled 1 artifact" in out
    assert "lex-deb  [deb]  skipped: not for this target" in out
    assert "plugin  passthrough: no bundle declared" in out
    assert recorder.heads == ["tar"]


def test_bundle_native_host_default_reads_target_release(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path, monkeypatch)
    monkeypatch.setattr(release_verb.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(release_verb.platform, "machine", lambda: "arm64")
    _executable(root / "target/release/lex")

    rc = release_verb.run_bundle(run_cmd=RunRecorder())

    assert rc == 0
    out = capsys.readouterr().out
    assert "bundled 1 artifact" in out
    assert MAC in out


def test_bundle_composes_the_deb_on_its_platform(
    tmp_path, monkeypatch, capsys, cargo_deb_on_path
):
    root = _repo(tmp_path, monkeypatch)
    _executable(root / f"target/{LINUX}/release/lex")
    recorder = RunRecorder({"cargo": _deb_effect()})

    rc = release_verb.run_bundle(target=LINUX, run_cmd=recorder)

    assert rc == 0
    assert recorder.heads == ["tar", "cargo"]
    (cargo_argv,) = [argv for argv, _ in recorder.calls if argv[0] == "cargo"]
    assert "--target" in cargo_argv and LINUX in cargo_argv


def test_bundle_json_carries_the_typed_result(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path, monkeypatch)
    _executable(root / f"target/{MAC}/release/lex")
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
    _repo(tmp_path, monkeypatch)
    recorder = RunRecorder({"cargo": _deb_effect()})

    rc = release_verb.run_bundle(target=LINUX, run_cmd=recorder)

    assert rc == 1
    assert "error: " in capsys.readouterr().err
    assert recorder.calls == []


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
    _repo(tmp_path, monkeypatch)
    monkeypatch.setattr(release_verb.git, "repo_root", lambda *, cwd: None)
    rc = release_verb.run_bundle(target=LINUX, run_cmd=RunRecorder())
    assert rc == 1
    assert "not inside a git checkout" in capsys.readouterr().err


def test_bundle_artifact_narrows_the_walk_to_one_artifact(
    tmp_path, monkeypatch, capsys
):
    root = _repo(tmp_path, monkeypatch)
    _executable(root / f"target/{LINUX}/release/lex")
    recorder = RunRecorder({"cargo": _deb_effect()})

    rc = release_verb.run_bundle(target=LINUX, artifact="lex", run_cmd=recorder)

    assert rc == 0
    out = capsys.readouterr().out
    assert "bundled 1 artifact" in out
    assert recorder.heads == ["tar"]
    assert "lex-deb" not in out


def test_bundle_artifact_unknown_name_is_a_loud_refusal(tmp_path, monkeypatch, capsys):
    _repo(tmp_path, monkeypatch)
    recorder = RunRecorder()

    rc = release_verb.run_bundle(target=LINUX, artifact="nope", run_cmd=recorder)

    assert rc == 1
    err = capsys.readouterr().err
    assert "--artifact nope" in err
    assert "lex, lex-deb, plugin" in err
    assert recorder.calls == []
