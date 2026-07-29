"""The assert-bundle pure core: is the artifact the RIGHT binary, not just signed?

:func:`expected_main_binary` derives the name; :func:`check_tree` asserts it
against every main binary a bundle tree carries. Pure reads.
"""

from __future__ import annotations

import io
import json
import plistlib
import re
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .. import config

#: The mac-app composition's reseal payload; inspected without extraction.
RESEAL_SUFFIX = ".unsigned-app.tar.gz"

#: The archive composition's outputs; the reseal payload is also a ``.tar.gz``
#: and is excluded by :func:`_is_plain_archive`.
PLAIN_ARCHIVE_SUFFIXES = (".tar.gz", ".zip")

DEB_SUFFIX = ".deb"

#: ``npm pack``'s suffix, NOT ``.tar.gz``, so the plain-archive tier skips it.
NPM_TARBALL_SUFFIX = ".tgz"

#: A ``.deb`` IS an ar archive.
_AR_MAGIC = b"!<arch>\n"

#: Both are OPAQUE to pure reads, so their tier asserts the product name
#: electron-builder stamped into the FILENAME, never an inner binary.
DMG_SUFFIX = ".dmg"
APPIMAGE_SUFFIX = ".AppImage"

#: Inert data beside a distributable, never a main-binary candidate.
BLOCKMAP_SUFFIX = ".blockmap"

#: The version boundary in ``<product>-<version>[-<arch>]<suffix>``: the first
#: ``-`` followed by a digit. Heuristic — a bad split fails loudly.
_ELECTRON_VERSION_BOUNDARY = re.compile(r"-\d")


def expected_main_binary(artifact: config.Artifact) -> str:
    """The expected main-binary name: ``main-binary`` -> ``product-name`` -> the first
    build target's package basename -> the artifact name, ``@scope/``-prefixed for
    a scoped wasm-pack bundle.
    """
    base = _base_main_binary(artifact)
    bundle = artifact.bundle
    if bundle is not None and bundle.scope is not None:
        return f"@{bundle.scope}/{base}"
    return base


def _base_main_binary(artifact: config.Artifact) -> str:
    if artifact.main_binary is not None:
        return artifact.main_binary
    if artifact.product_name is not None:
        return artifact.product_name
    for target in artifact.build:
        basename = target.package_basename
        if basename is not None:
            return basename
    return artifact.name


@dataclass(frozen=True)
class BundleVerdict:
    """The check's typed outcome; ``problem`` diagnoses a tree unreadable as a bundle."""

    tree: str
    expected: str
    actual: tuple[str, ...]
    ok: bool
    problem: str | None = None

    def to_dict(self) -> dict:
        out: dict = {
            "tree": self.tree,
            "expected": self.expected,
            "actual": list(self.actual),
            "ok": self.ok,
        }
        if self.problem is not None:
            out["problem"] = self.problem
        return out


def _app_main_binary(app: Path) -> str | None:
    """A ``.app``'s ``CFBundleExecutable``, else the SOLE file in ``Contents/MacOS``."""
    info = app / "Contents" / "Info.plist"
    if info.is_file():
        try:
            executable = plistlib.loads(info.read_bytes()).get("CFBundleExecutable")
        except plistlib.InvalidFileException:
            executable = None
        if isinstance(executable, str) and executable:
            return executable
    macos = app / "Contents" / "MacOS"
    if macos.is_dir():
        files = [p.name for p in sorted(macos.iterdir()) if p.is_file()]
        if len(files) == 1:
            return files[0]
    return None


def _payload_main_binary(payload: Path) -> str | None:
    """A reseal payload's inner ``.app`` main binary, read WITHOUT extraction."""
    macos_members: list[str] = []
    try:
        with tarfile.open(payload, mode="r:gz") as tar:
            for member in tar:
                parts = PurePosixPath(member.name).parts
                if "Contents" not in parts:
                    continue
                at = parts.index("Contents")
                inner = parts[at + 1 :]
                if inner == ("Info.plist",) and member.isfile():
                    extracted = tar.extractfile(member)
                    if extracted is not None:
                        try:
                            plist = plistlib.loads(extracted.read())
                        except plistlib.InvalidFileException:
                            plist = {}
                        executable = plist.get("CFBundleExecutable")
                        if isinstance(executable, str) and executable:
                            return executable
                elif len(inner) == 2 and inner[0] == "MacOS" and member.isfile():
                    macos_members.append(inner[1])
    except (tarfile.TarError, OSError):
        return None
    if len(macos_members) == 1:
        return macos_members[0]
    return None


def _is_plain_archive(path: Path) -> bool:
    """Whether ``path`` is an archive-composition output rather than a reseal payload."""
    if path.name.endswith(RESEAL_SUFFIX):
        return False
    return path.name.endswith(PLAIN_ARCHIVE_SUFFIXES)


def _archive_main_binary(archive: Path) -> str | None:
    """A plain archive's SOLE executable member, read WITHOUT extraction.

    The exec bit inside the archive header survives artifact transport where the
    loose file's does not; a windows ``.zip`` carries no unix mode, so a ``.exe``
    member counts by suffix. Exec-bit and ``.exe`` candidates are tallied TOGETHER,
    so a mixed archive is ambiguous and fails loudly.
    """
    exec_members: list[str] = []
    exe_members: list[str] = []
    try:
        if archive.name.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    # Match tar's isfile() filter; a windows-created entry
                    # carries no unix mode and falls through to the .exe check.
                    mode = info.external_attr >> 16
                    if mode and (mode & 0o170000) != 0o100000:
                        continue
                    base = PurePosixPath(info.filename).name
                    if base.endswith(".exe"):
                        exe_members.append(base[: -len(".exe")])
                    elif mode & 0o111:
                        exec_members.append(base)
        else:
            with tarfile.open(archive, mode="r:gz") as tar:
                for member in tar:
                    if not member.isfile():
                        continue
                    base = PurePosixPath(member.name).name
                    if base.endswith(".exe"):
                        exe_members.append(base[: -len(".exe")])
                    elif member.mode & 0o111:
                        exec_members.append(base)
    except (tarfile.TarError, zipfile.BadZipFile, OSError):
        return None
    candidates = exec_members + exe_members
    if len(candidates) == 1:
        return candidates[0]
    return None


def _deb_data_tar(deb: Path) -> bytes | None:
    """The deb's ``data.tar.*`` bytes, sliced out of the ar container WITHOUT extraction.

    The whole file is read into memory, bounded because a shipit ``.deb`` wraps one
    pre-built CLI binary. ``None`` for a malformed, missing, or truncated member.
    """
    try:
        raw = deb.read_bytes()
    except OSError:
        return None
    if not raw.startswith(_AR_MAGIC):
        return None
    offset = len(_AR_MAGIC)
    while offset + 60 <= len(raw):
        header = raw[offset : offset + 60]
        if header[58:60] != b"`\n":
            return None
        # GNU ar terminates a name with `/`; both spellings strip to the name.
        name = header[:16].decode("ascii", errors="replace").rstrip().rstrip("/")
        try:
            size = int(header[48:58])
        except ValueError:
            return None
        offset += 60
        if name.startswith("data.tar"):
            member = raw[offset : offset + size]
            return member if len(member) == size else None
        offset += size + (size % 2)
    return None


def _deb_main_binary(deb: Path) -> str | None:
    """The SOLE executable regular member of a ``.deb``'s ``data.tar``, read in place."""
    data = _deb_data_tar(deb)
    if data is None:
        return None
    execs: list[str] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
            for member in tar:
                if member.isfile() and member.mode & 0o111:
                    execs.append(PurePosixPath(member.name).name)
    except (tarfile.TarError, OSError):
        return None
    if len(execs) == 1:
        return execs[0]
    return None


def _container_product_name(path: Path, suffix: str) -> str | None:
    """The product-name segment of an electron-builder filename, before the version."""
    stem = path.name[: -len(suffix)]
    match = _ELECTRON_VERSION_BOUNDARY.search(stem)
    if match is None:
        return None
    product = stem[: match.start()]
    return product or None


def _npm_tarball_main_binary(tarball: Path) -> str | None:
    """A ``.tgz``'s inner ``package/package.json`` ``name``, read WITHOUT extraction."""
    try:
        with tarfile.open(tarball, mode="r:gz") as tar:
            for member in tar:
                parts = PurePosixPath(member.name).parts
                if parts[-1:] != ("package.json",) or not member.isfile():
                    continue
                # A nested bundled dependency's manifest is not the identity.
                if len(parts) != 2 or parts[0] != "package":
                    continue
                extracted = tar.extractfile(member)
                if extracted is None:
                    return None
                try:
                    manifest = json.loads(extracted.read())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return None
                name = manifest.get("name") if isinstance(manifest, dict) else None
                return name if isinstance(name, str) and name else None
    except (tarfile.TarError, OSError):
        return None
    return None


def _is_executable(path: Path) -> bool:
    """Whether ``path`` is a loose main-binary candidate: an executable regular file
    or a ``.exe``, excluding the container suffixes their own tiers assert.
    """
    if not path.is_file() or path.is_symlink():
        return False
    if path.suffix == ".exe":
        return True
    # Archives and opaque distributables ride the tree too and have their own
    # tiers; an .AppImage is an executable ELF the loose scan would misread.
    if path.name.endswith(
        (".tar.gz", ".tgz", ".zip", ".dmg", ".deb", ".whl", ".AppImage", ".blockmap")
    ):
        return False
    return bool(path.stat().st_mode & 0o111)


def check_tree(tree: Path, expected: str) -> BundleVerdict:
    """Assert the bundle tree's MAIN binary is ``expected``. Pure reads.

    Discovery runs in precedence order: ``.app`` dirs, reseal payloads, plain
    archives, ``.deb``, npm ``.tgz`` — then, ONLY when none of those was found, the
    opaque ``.dmg``/``.AppImage`` filename heuristic, then loose executables. The
    filename tier is a fallback precisely because it is a heuristic: a tauri
    mac-app's own ``.dmg`` rides beside the ``.app`` that already asserts its
    binary. The verdict is ``ok`` exactly when at least one binary was found and
    every one is named ``expected``.
    """
    actual: list[str] = []
    problems: list[str] = []
    apps = sorted(p for p in tree.rglob("*.app") if p.is_dir())
    payloads = sorted(p for p in tree.rglob(f"*{RESEAL_SUFFIX}") if p.is_file())
    archives = sorted(
        p
        for suffix in PLAIN_ARCHIVE_SUFFIXES
        for p in tree.rglob(f"*{suffix}")
        if p.is_file() and _is_plain_archive(p)
    )
    debs = sorted(p for p in tree.rglob(f"*{DEB_SUFFIX}") if p.is_file())
    tarballs = sorted(p for p in tree.rglob(f"*{NPM_TARBALL_SUFFIX}") if p.is_file())
    dmgs = sorted(p for p in tree.rglob(f"*{DMG_SUFFIX}") if p.is_file())
    appimages = sorted(p for p in tree.rglob(f"*{APPIMAGE_SUFFIX}") if p.is_file())
    for app in apps:
        name = _app_main_binary(app)
        if name is None:
            problems.append(f"{app.relative_to(tree)}: no determinable main binary")
        else:
            actual.append(name)
    for payload in payloads:
        name = _payload_main_binary(payload)
        if name is None:
            problems.append(f"{payload.relative_to(tree)}: no determinable main binary")
        else:
            actual.append(name)
    for archive in archives:
        name = _archive_main_binary(archive)
        if name is None:
            problems.append(f"{archive.relative_to(tree)}: no determinable main binary")
        else:
            actual.append(name)
    for deb in debs:
        name = _deb_main_binary(deb)
        if name is None:
            problems.append(f"{deb.relative_to(tree)}: no determinable main binary")
        else:
            actual.append(name)
    for tarball in tarballs:
        name = _npm_tarball_main_binary(tarball)
        if name is None:
            problems.append(
                f"{tarball.relative_to(tree)}: no determinable package name"
            )
        else:
            actual.append(name)
    # The opaque-container tiers assert only when no authoritative binary was
    # found; see :func:`check_tree`.
    if not (apps or payloads or archives or debs or tarballs):
        for dmg in dmgs:
            name = _container_product_name(dmg, DMG_SUFFIX)
            if name is None:
                problems.append(f"{dmg.relative_to(tree)}: no determinable main binary")
            else:
                actual.append(name)
        for appimage in appimages:
            name = _container_product_name(appimage, APPIMAGE_SUFFIX)
            if name is None:
                problems.append(
                    f"{appimage.relative_to(tree)}: no determinable main binary"
                )
            else:
                actual.append(name)
    if not (apps or payloads or archives or debs or tarballs or dmgs or appimages):
        for path in sorted(tree.rglob("*")):
            if _is_executable(path):
                actual.append(path.stem if path.suffix == ".exe" else path.name)
    names = tuple(sorted(set(actual)))
    if problems:
        return BundleVerdict(
            tree=str(tree),
            expected=expected,
            actual=names,
            ok=False,
            problem="; ".join(problems),
        )
    if not names:
        return BundleVerdict(
            tree=str(tree),
            expected=expected,
            actual=(),
            ok=False,
            problem="no main binary found in the bundle tree — nothing to assert",
        )
    return BundleVerdict(
        tree=str(tree), expected=expected, actual=names, ok=names == (expected,)
    )
