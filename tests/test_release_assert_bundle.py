"""`shipit release assert-bundle` — fixture tests over the pure core.

The integrity guard (workflows.lex §3.2) is pure over a bundle tree, so the
tests build real tmp-path fixtures: ``.app`` bundles (Info.plist +
Contents/MacOS), reseal payloads (real tars, read in place), plain archives,
``.deb`` packages (real ar containers, read in place — issue #784 F4), and
loose-binary dist trees. Coverage pins each level of the expected-name fallback chain
(mainBinaryName → productName → package name → artifact name) and reproduces
the scar itself: the gen_fixtures incident — a dev tool shipped as the app's
main executable, signing and notarizing cleanly — as the wrong-binary
fixture. The verb tests pin the uniform exit contract: 0 match, 1 mismatch
with the verdict + expected/actual names on stderr, ``--json`` available.
"""

import io
import json
import plistlib
import shutil
import stat
import tarfile
import zipfile

import pytest

from shipit import config
from shipit.release import integrity
from shipit.verbs import release as release_verb


def _artifact(spec: dict, name: str = "phos") -> config.Artifact:
    (artifact,) = config.load_artifacts({"artifacts": {name: spec}})
    return artifact


# --------------------------------------------------------------------------
# The expected-name fallback chain — one test per level
# --------------------------------------------------------------------------


def test_expected_name_prefers_declared_main_binary():
    artifact = _artifact(
        {
            "main-binary": "phos-bin",
            "product-name": "Phos",
            "build": [{"toolchain": "rust", "package": "phos-app"}],
        }
    )
    assert integrity.expected_main_binary(artifact) == "phos-bin"


def test_expected_name_falls_back_to_product_name():
    artifact = _artifact(
        {
            "product-name": "Phos",
            "build": [{"toolchain": "rust", "package": "phos-app"}],
        }
    )
    assert integrity.expected_main_binary(artifact) == "Phos"


def test_expected_name_falls_back_to_the_package_name():
    artifact = _artifact({"build": [{"toolchain": "rust", "package": "phos-app"}]})
    assert integrity.expected_main_binary(artifact) == "phos-app"


def test_expected_name_takes_a_go_package_basename():
    artifact = _artifact({"build": [{"toolchain": "go", "package": "./cmd/padz"}]})
    assert integrity.expected_main_binary(artifact) == "padz"


def test_expected_name_bottoms_out_at_the_artifact_name():
    artifact = _artifact({"build": ["rust"]})
    assert integrity.expected_main_binary(artifact) == "phos"


@pytest.mark.parametrize("package", [".", "./", "..", "/"])
def test_expected_name_skips_a_package_with_no_usable_basename(package):
    # A path-navigation package (`.`/`./`/`..`/`/`) names no binary — the chain
    # skips it and bottoms out at the artifact name, never asserting `.`/`..`.
    artifact = _artifact({"build": [{"toolchain": "go", "package": package}]})
    assert integrity.expected_main_binary(artifact) == "phos"


# --------------------------------------------------------------------------
# check_tree — .app fixtures
# --------------------------------------------------------------------------


def _make_app(root, app="Phos.app", executable="phos", plist=True, extra=()):
    macos = root / app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    for name in (executable, *extra):
        (macos / name).write_bytes(b"\xcf\xfa\xed\xfe")
        (macos / name).chmod(0o755)
    if plist:
        (root / app / "Contents" / "Info.plist").write_bytes(
            plistlib.dumps({"CFBundleExecutable": executable})
        )
    return root / app


def test_app_with_the_expected_main_binary_passes(tmp_path):
    _make_app(tmp_path)
    verdict = integrity.check_tree(tmp_path, "phos")
    assert verdict.ok
    assert verdict.actual == ("phos",)


def test_the_gen_fixtures_regression_fails_loudly(tmp_path):
    # THE scar (workflows.lex §3.2): a src-tauri crate with multiple binaries
    # and no declared main let the bundler pick the alphabetically-first one —
    # gen_fixtures shipped as the app's main executable and SIGNED CLEANLY.
    # The bundler's pick rides Info.plist; the real app sits beside it, inert.
    _make_app(tmp_path, executable="gen_fixtures", extra=("phos",))
    verdict = integrity.check_tree(tmp_path, "phos")
    assert not verdict.ok
    assert verdict.actual == ("gen_fixtures",)
    assert verdict.expected == "phos"


def test_app_without_plist_uses_the_sole_macos_binary(tmp_path):
    _make_app(tmp_path, plist=False)
    assert integrity.check_tree(tmp_path, "phos").ok


def test_app_without_plist_and_several_binaries_is_undeterminable(tmp_path):
    _make_app(tmp_path, plist=False, extra=("gen_fixtures",))
    verdict = integrity.check_tree(tmp_path, "phos")
    assert not verdict.ok
    assert "no determinable main binary" in verdict.problem


# --------------------------------------------------------------------------
# check_tree — reseal payloads, read in place
# --------------------------------------------------------------------------


def _make_payload(tree, executable="phos", name="phos.unsigned-app.tar.gz"):
    app = _make_app(tree / "staging", executable=executable)
    tree.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tree / name, "w:gz") as tar:
        tar.add(app, arcname=app.name)
    # The staging .app must not shadow the payload under test.
    shutil.rmtree(tree / "staging")
    return tree / name


def test_reseal_payload_is_inspected_without_extraction(tmp_path):
    _make_payload(tmp_path)
    verdict = integrity.check_tree(tmp_path, "phos")
    assert verdict.ok
    assert verdict.actual == ("phos",)


def test_reseal_payload_with_the_wrong_binary_fails(tmp_path):
    _make_payload(tmp_path, executable="gen_fixtures")
    verdict = integrity.check_tree(tmp_path, "phos")
    assert not verdict.ok
    assert verdict.actual == ("gen_fixtures",)


# --------------------------------------------------------------------------
# check_tree — loose-binary trees (the archive layout) and empties
# --------------------------------------------------------------------------


def _make_dist(tmp_path, binary="lex", docs=True):
    stage = tmp_path / f"{binary}-x86_64-unknown-linux-gnu"
    stage.mkdir(parents=True)
    (stage / binary).write_bytes(b"\x7fELF")
    (stage / binary).chmod(0o755)
    if docs:
        (stage / "README.md").write_text("readme")  # not executable, not counted
    return stage


def test_loose_executable_matching_the_expected_name_passes(tmp_path):
    _make_dist(tmp_path)
    verdict = integrity.check_tree(tmp_path, "lex")
    assert verdict.ok
    assert verdict.actual == ("lex",)


def test_loose_executable_with_the_wrong_name_fails(tmp_path):
    _make_dist(tmp_path, binary="gen_fixtures")
    verdict = integrity.check_tree(tmp_path, "lex")
    assert not verdict.ok
    assert verdict.actual == ("gen_fixtures",)


def test_windows_exe_matches_by_stem(tmp_path):
    stage = tmp_path / "lex-x86_64-pc-windows-msvc"
    stage.mkdir(parents=True)
    (stage / "lex.exe").write_bytes(b"MZ")  # no exec bit on windows payloads
    assert integrity.check_tree(tmp_path, "lex").ok


def test_an_empty_tree_has_nothing_to_assert_and_fails(tmp_path):
    verdict = integrity.check_tree(tmp_path, "lex")
    assert not verdict.ok
    assert "no main binary found" in verdict.problem


# --------------------------------------------------------------------------
# check_tree — plain archives, read in place (GH artifact strips exec bits)
# --------------------------------------------------------------------------


def _make_archive(
    tmp_path, binary="lex", docs=True, extra=(), name=None, mode=0o755, strip_loose=True
):
    """A real ``<stem>.tar.gz`` (the archive composition's output): a
    ``<stem>/`` tree with the binary (exec bit `mode`) + docs, tarred with
    the mode PRESERVED in the header. `strip_loose` removes the staging tree
    after tarring so the archive is the only main-binary bearer, and — when
    kept — re-writes the loose binary at 0o644 to model GH artifact transport
    dropping the exec bit off the downloaded staging file."""
    stem = f"{binary}-x86_64-unknown-linux-gnu" if name is None else name
    stage = tmp_path / stem
    stage.mkdir(parents=True)
    for exe in (binary, *extra):
        (stage / exe).write_bytes(b"\x7fELF")
        (stage / exe).chmod(mode)
    if docs:
        (stage / "README.md").write_text("readme")
    archive = tmp_path / f"{stem}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(stage, arcname=stem)
    if strip_loose:
        shutil.rmtree(stage)
    else:
        # GH artifact download hands the loose binary back non-executable.
        for exe in (binary, *extra):
            (stage / exe).chmod(0o644)
    return archive


def test_archive_main_binary_passes_from_the_internal_exec_bit(tmp_path):
    _make_archive(tmp_path)
    verdict = integrity.check_tree(tmp_path, "lex")
    assert verdict.ok
    assert verdict.actual == ("lex",)


def test_archive_with_the_wrong_binary_fails(tmp_path):
    _make_archive(tmp_path, binary="gen_fixtures")
    verdict = integrity.check_tree(tmp_path, "lex")
    assert not verdict.ok
    assert verdict.actual == ("gen_fixtures",)


def test_archive_is_asserted_when_the_loose_staging_binary_lost_its_exec_bit(tmp_path):
    # THE finding: wf-publish downloads a cross-job artifact, GH strips the
    # staging binary's exec bit, and the loose scan goes blind — but the
    # archive beside it preserves the exec bit in its header and still asserts.
    _make_archive(tmp_path, strip_loose=False)
    verdict = integrity.check_tree(tmp_path, "lex")
    assert verdict.ok
    assert verdict.actual == ("lex",)


def test_archive_with_several_exec_members_is_undeterminable(tmp_path):
    _make_archive(tmp_path, extra=("gen_fixtures",))
    verdict = integrity.check_tree(tmp_path, "lex")
    assert not verdict.ok
    assert "no determinable main binary" in verdict.problem


def test_archive_mixing_a_unix_exec_and_an_exe_is_undeterminable(tmp_path):
    # agy, round 1: the exec-bit tally and the `.exe` tally are counted
    # TOGETHER. An archive carrying both a unix executable and a `.exe` is
    # ambiguous — two candidates by different measures — and must fail loudly,
    # never silently return the unix one and drop the `.exe`.
    stem = "lex-x86_64-unknown-linux-gnu"
    stage = tmp_path / stem
    stage.mkdir(parents=True)
    (stage / "helper").write_bytes(b"\x7fELF")
    (stage / "helper").chmod(0o755)
    (stage / "main.exe").write_bytes(b"MZ")
    archive = tmp_path / f"{stem}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(stage, arcname=stem)
    shutil.rmtree(stage)
    verdict = integrity.check_tree(tmp_path, "lex")
    assert not verdict.ok
    assert "no determinable main binary" in verdict.problem


def test_zip_exec_bit_symlink_is_not_a_binary_candidate(tmp_path):
    # agy, round 1: a Unix-created ZIP can carry a symlink WITH the exec bit
    # set. It is not a regular file and must be ignored (matching tar's
    # isfile() filter), so it never inflates the candidate count nor
    # masquerades as the main binary. The real binary beside it still asserts.
    stem = "lex-x86_64-unknown-linux-gnu"
    archive = tmp_path / f"{stem}.zip"
    real = zipfile.ZipInfo(f"{stem}/lex")
    real.external_attr = (stat.S_IFREG | 0o755) << 16
    link = zipfile.ZipInfo(f"{stem}/latest")
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(real, b"\x7fELF")
        zf.writestr(link, "lex")  # symlink target as content
    verdict = integrity.check_tree(tmp_path, "lex")
    assert verdict.ok
    assert verdict.actual == ("lex",)


def test_reseal_payload_is_not_treated_as_a_plain_archive(tmp_path):
    # The reseal payload is a `.tar.gz` too; it stays on the payload path
    # (inner .app), never double-counted as a plain archive.
    _make_payload(tmp_path)
    verdict = integrity.check_tree(tmp_path, "phos")
    assert verdict.ok
    assert verdict.actual == ("phos",)


def test_windows_zip_matches_the_exe_stem(tmp_path):
    stem = "lex-x86_64-pc-windows-msvc"
    stage = tmp_path / stem
    stage.mkdir(parents=True)
    (stage / "lex.exe").write_bytes(b"MZ")
    (stage / "README.md").write_text("readme")
    with zipfile.ZipFile(tmp_path / f"{stem}.zip", "w") as zf:
        for f in sorted(stage.iterdir()):
            zf.write(f, arcname=f"{stem}/{f.name}")
    shutil.rmtree(stage)
    assert integrity.check_tree(tmp_path, "lex").ok


# --------------------------------------------------------------------------
# check_tree — .deb packages, read in place (issue #784 F4)
# --------------------------------------------------------------------------


def _tar_bytes(entries, compression="xz"):
    """An in-memory tar: (path, content, mode) triples, dirs as DIRTYPE."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=f"w:{compression}") as tar:
        for path, content, mode in entries:
            info = tarfile.TarInfo(path)
            info.mode = mode
            if content is None:
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _ar_member(name, payload):
    """One ar member: the 60-byte header + payload, padded to an even offset."""
    header = (
        f"{name:<16}{'0':<12}{'0':<6}{'0':<6}{'100644':<8}{len(payload):<10}".encode()
        + b"`\n"
    )
    return header + payload + (b"\n" if len(payload) % 2 else b"")


def _make_deb(
    tmp_path, binaries=("phos",), name="phos_1.0.0-1_amd64.deb", compression="xz"
):
    """A real ``.deb`` in cargo-deb's shape — an ar container (debian-binary +
    control.tar.gz + data.tar.<compression>) whose data.tar carries
    ``usr/bin/<binary>`` at 0o755 (the exec bit in the INNER tar header, the
    transport-proof bit) beside non-executable doc members and the usual
    directory entries (0o755 dirs must never count as binary candidates)."""
    entries = [
        ("./usr", None, 0o755),
        ("./usr/bin", None, 0o755),
        *((f"./usr/bin/{binary}", b"\x7fELF", 0o755) for binary in binaries),
        ("./usr/share/doc/phos/copyright", b"(c)", 0o644),
    ]
    deb = integrity._AR_MAGIC
    deb += _ar_member("debian-binary", b"2.0\n")
    deb += _ar_member(
        "control.tar.gz", _tar_bytes([("./control", b"Package: phos\n", 0o644)], "gz")
    )
    deb += _ar_member(f"data.tar.{compression}", _tar_bytes(entries, compression))
    (tmp_path / name).write_bytes(deb)
    return tmp_path / name


@pytest.mark.parametrize("compression", ["xz", "gz"])
def test_deb_main_binary_passes_from_the_data_tar(tmp_path, compression):
    # THE #784 F4 finding: a deb leg's bundle tree carries ONLY the .deb —
    # opaque to every other tier — and hard-failed with "nothing to assert".
    # The deb tier reads the data.tar member in place (xz is cargo-deb's
    # default compression; gz stays legal).
    _make_deb(tmp_path, compression=compression)
    verdict = integrity.check_tree(tmp_path, "phos")
    assert verdict.ok
    assert verdict.actual == ("phos",)


def test_deb_with_the_wrong_binary_fails(tmp_path):
    _make_deb(tmp_path, binaries=("gen_fixtures",))
    verdict = integrity.check_tree(tmp_path, "phos")
    assert not verdict.ok
    assert verdict.actual == ("gen_fixtures",)


def test_deb_with_several_exec_members_is_undeterminable(tmp_path):
    _make_deb(tmp_path, binaries=("phos", "gen_fixtures"))
    verdict = integrity.check_tree(tmp_path, "phos")
    assert not verdict.ok
    assert "no determinable main binary" in verdict.problem


def test_a_corrupt_deb_is_undeterminable_never_a_pass(tmp_path):
    (tmp_path / "phos_1.0.0-1_amd64.deb").write_bytes(b"not an ar archive")
    verdict = integrity.check_tree(tmp_path, "phos")
    assert not verdict.ok
    assert "no determinable main binary" in verdict.problem


def test_deb_is_asserted_even_beside_loose_junk(tmp_path):
    # The deb tier suppresses the loose scan exactly like the other archive
    # tiers: a stray non-executable file beside the .deb changes nothing.
    _make_deb(tmp_path)
    (tmp_path / "README.md").write_text("readme")
    assert integrity.check_tree(tmp_path, "phos").ok


def test_an_app_takes_precedence_over_loose_executables(tmp_path):
    # When the tree bundles an app, the app IS the main binary — a correct
    # loose helper binary beside a wrong .app must not mask the scar.
    _make_app(tmp_path, executable="gen_fixtures")
    _make_dist(tmp_path, binary="phos")
    verdict = integrity.check_tree(tmp_path, "phos")
    assert not verdict.ok
    assert verdict.actual == ("gen_fixtures",)


# --------------------------------------------------------------------------
# check_tree — electron .dmg / .AppImage, asserted by the declared filename
# --------------------------------------------------------------------------


def test_dmg_asserts_the_product_name_from_the_filename(tmp_path):
    # The .dmg is opaque to pure reads (issue #790), so the tier asserts the
    # product name electron-builder stamped into `<product>-<version>-<arch>`.
    (tmp_path / "Phos-1.2.3-arm64.dmg").write_bytes(b"udif")
    verdict = integrity.check_tree(tmp_path, "Phos")
    assert verdict.ok
    assert verdict.actual == ("Phos",)


def test_dmg_with_the_wrong_product_name_fails(tmp_path):
    (tmp_path / "gen_fixtures-1.2.3-arm64.dmg").write_bytes(b"udif")
    verdict = integrity.check_tree(tmp_path, "Phos")
    assert not verdict.ok
    assert verdict.actual == ("gen_fixtures",)


def test_dmg_with_a_hyphenated_product_name_keeps_the_whole_name(tmp_path):
    # The version boundary is the first `-<digit>`, so a hyphen in the product
    # name (before the version) survives.
    (tmp_path / "Simple-Gal-UI-0.4.0-arm64.dmg").write_bytes(b"udif")
    assert integrity.check_tree(tmp_path, "Simple-Gal-UI").ok


def test_appimage_asserts_the_product_name_and_is_not_a_loose_binary(tmp_path):
    # An .AppImage is an executable ELF; without its tier the loose scan would
    # misread it as a main binary named `Phos-1.2.3.AppImage`. The tier asserts
    # the product segment instead, and the .blockmap sidecar is inert.
    appimage = tmp_path / "Phos-1.2.3.AppImage"
    appimage.write_bytes(b"\x7fELF")
    appimage.chmod(0o755)
    (tmp_path / "Phos-1.2.3.AppImage.blockmap").write_bytes(b"blockmap")
    verdict = integrity.check_tree(tmp_path, "Phos")
    assert verdict.ok
    assert verdict.actual == ("Phos",)


def test_an_authoritative_app_takes_precedence_over_the_dmg_name_tier(tmp_path):
    # The .dmg/.AppImage name tiers are a FALLBACK: when an authoritative
    # binary tier is present (here a .app), it asserts and the opaque .dmg name
    # tier is skipped. The verdict still passes off the .app's authoritative
    # CFBundleExecutable — the transport-fragile .app just does not reach
    # wf-publish, where the .dmg name tier is what runs.
    _make_app(tmp_path, app="Phos.app", executable="Phos")
    (tmp_path / "Phos-1.2.3-arm64.dmg").write_bytes(b"udif")
    (tmp_path / "Phos-1.2.3-arm64.dmg.blockmap").write_bytes(b"blockmap")
    verdict = integrity.check_tree(tmp_path, "Phos")
    assert verdict.ok
    assert verdict.actual == ("Phos",)


def test_a_mac_app_dmg_does_not_fail_the_tree_its_app_asserts(tmp_path):
    # Regression: a tauri mac-app ships its OWN .dmg beside the .app and reseal
    # payload — and tauri names it `Product_1.0.0_arch.dmg` (underscores), which
    # the electron name tier cannot split into product/version. The .dmg tier
    # must NOT escalate that non-electron container to a failure the .app and
    # payload already cleared: it is a fallback, gated off the authoritative
    # tiers being present.
    _make_app(tmp_path, app="Phos.app", executable="phos")
    _make_payload(tmp_path, executable="phos")
    (tmp_path / "Phos_1.0.0_aarch64.dmg").write_bytes(b"udif")
    verdict = integrity.check_tree(tmp_path, "phos")
    assert verdict.ok
    assert verdict.actual == ("phos",)


def test_dmg_without_a_version_boundary_is_undeterminable(tmp_path):
    # A name the tier cannot split into product/version fails loudly with the
    # diagnosis — never a silent pass. (No authoritative tier present, so the
    # .dmg fallback tier runs and reports the undeterminable container.)
    (tmp_path / "installer.dmg").write_bytes(b"udif")
    verdict = integrity.check_tree(tmp_path, "Phos")
    assert not verdict.ok
    assert "no determinable main binary" in verdict.problem


# --------------------------------------------------------------------------
# The verb: exit contract, stderr diagnosis, --json, artifact resolution
# --------------------------------------------------------------------------

REPO_TOML = """\
[artifacts.phos]
build = [{ toolchain = "rust", package = "phos-app" }]
main-binary = "phos"
"""


def _repo(tmp_path, monkeypatch, toml=REPO_TOML):
    (tmp_path / ".shipit.toml").write_text(toml, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # assert-bundle anchors config to the checkout root (not cwd), so the map
    # branch resolves the same from any subdirectory; the tmp repo is not a git
    # checkout, so stub the adapter to report its root.
    monkeypatch.setattr(release_verb.git, "repo_root", lambda *, cwd: str(tmp_path))
    return tmp_path


def test_verb_passes_on_a_matching_tree(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path, monkeypatch)
    _make_app(root / "dist")
    rc = release_verb.run_assert_bundle(str(root / "dist"))
    assert rc == 0
    captured = capsys.readouterr()
    assert "assert-bundle: ok" in captured.out
    assert captured.err == ""


def test_verb_fails_with_the_verdict_on_stderr(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path, monkeypatch)
    _make_app(root / "dist", executable="gen_fixtures")
    rc = release_verb.run_assert_bundle(str(root / "dist"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "expected main binary 'phos'" in err
    assert "gen_fixtures" in err


def test_verb_json_renders_the_typed_verdict_even_on_failure(
    tmp_path, monkeypatch, capsys
):
    root = _repo(tmp_path, monkeypatch)
    _make_app(root / "dist", executable="gen_fixtures")
    rc = release_verb.run_assert_bundle(str(root / "dist"), as_json=True)
    assert rc == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload == {
        "tree": str(root / "dist"),
        "expected": "phos",
        "actual": ["gen_fixtures"],
        "ok": False,
    }
    assert "expected main binary" in captured.err


def test_verb_expected_flag_bypasses_the_artifact_map(tmp_path, monkeypatch, capsys):
    # No .shipit.toml at all: --expected needs no config (the WS06 blocks'
    # no-extra-plumbing path) — and no checkout either.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(release_verb.git, "repo_root", lambda *, cwd: None)
    _make_app(tmp_path / "dist")
    rc = release_verb.run_assert_bundle(str(tmp_path / "dist"), expected="phos")
    assert rc == 0


def test_verb_map_branch_refuses_outside_a_git_checkout(tmp_path, monkeypatch, capsys):
    # Without --expected the expected name resolves from the artifact map, which
    # is anchored to the checkout root; no checkout is a loud refusal, not a
    # misread of cwd as declaring zero artifacts.
    root = _repo(tmp_path, monkeypatch)
    (root / "dist").mkdir()
    monkeypatch.setattr(release_verb.git, "repo_root", lambda *, cwd: None)
    rc = release_verb.run_assert_bundle(str(root / "dist"))
    assert rc == 1
    assert "not inside a git checkout" in capsys.readouterr().err


def test_verb_refuses_an_unknown_artifact(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path, monkeypatch)
    (root / "dist").mkdir()
    rc = release_verb.run_assert_bundle(str(root / "dist"), artifact="nope")
    assert rc == 1
    assert "unknown artifact 'nope'" in capsys.readouterr().err


def test_verb_requires_naming_one_of_several_artifacts(tmp_path, monkeypatch, capsys):
    root = _repo(
        tmp_path,
        monkeypatch,
        "[artifacts.a]\nendpoints = []\n[artifacts.b]\nendpoints = []\n",
    )
    (root / "dist").mkdir()
    rc = release_verb.run_assert_bundle(str(root / "dist"))
    assert rc == 1
    assert "name one" in capsys.readouterr().err


def test_verb_resolves_the_named_artifact_through_the_chain(
    tmp_path, monkeypatch, capsys
):
    root = _repo(
        tmp_path,
        monkeypatch,
        '[artifacts.other]\nendpoints = []\n[artifacts.app]\nproduct-name = "Phos"\n',
    )
    _make_app(root / "dist", executable="Phos")
    rc = release_verb.run_assert_bundle(str(root / "dist"), artifact="app")
    assert rc == 0
