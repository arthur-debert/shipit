"""The assert-bundle pure core — the scar-#2 integrity guard (workflows.lex §3.2).

Signing is not integrity: a src-tauri crate with multiple binaries and no
declared main once let the bundler pick the alphabetically-first one — a dev
tool (``gen_fixtures``) shipped as the app's main executable, and it signed
and notarized cleanly. Signature checks verify the signature, not that the
artifact is the right binary. This module is that guard's pure core (bundle
tree in, verdict out — no network, no toolchain), shipped as its own verb
(``shipit release assert-bundle``) because two workflow blocks must invoke
it: ``wf-sign-mac``'s entry and ``wf-publish``'s unsigned path (ADR-0040 —
the in-block wiring is TOL02-WS06's; THIS module is the check both call).

Two halves:

- :func:`expected_main_binary` — the expected-name fallback chain over the
  artifact declaration (the legacy ``assert-tauri-bundle-binary.sh`` chain,
  generalized): ``main-binary`` (mainBinaryName) → ``product-name``
  (productName) → the first build target's package name → the artifact name.
- :func:`check_tree` — the check itself, over a bundle tree: every main
  binary the tree carries (a ``.app``'s ``CFBundleExecutable``, a reseal
  payload's inner ``.app``, a plain ``.tar.gz``/``.zip`` archive's inner
  executable, a ``.deb``'s inner executable, or — when the tree bundles none
  of those — its loose executables) must be named exactly the expected name.
  A tree with NO discoverable main binary fails loudly: "nothing to assert"
  is a wrong bundle, never a pass.

The archive tier exists because GitHub Actions artifact upload/download
STRIPS Unix exec bits: wf-publish's unsigned-path assert (ADR-0040) runs
over a bundle tree that crossed a cross-job artifact, so the loose staging
binary the archive composition emits arrives non-executable and the exec-bit
loose scan cannot see it. The ``.tar.gz`` beside it is the real distributable
and preserves the exec bit INSIDE its own header (tar stores mode), so the
archive is read in place — the same transport-proof, no-extraction shape as
the reseal payload — and its inner executable is the assertable main binary.

The deb tier (issue #784 F4) exists for the same reason with a thicker shell:
a deb composition's tree carries ONLY the ``.deb`` — opaque to every other
tier (``_is_executable`` deliberately excludes it from the loose scan) — so
without this tier wf-publish's assert fan hard-failed every deb leg with
"nothing to assert". A ``.deb`` is an ar container (``debian-binary`` +
``control.tar.*`` + ``data.tar.*``); the ``data.tar`` member is read in place
(never extracted), and its sole executable regular member — the exec bit
rides the inner tar's headers, transport-proof — is the assertable main
binary.

Pure over the filesystem (reads only); rendered by the verb with uniform
exit codes (0 pass, 1 fail — verdict + expected/actual on stderr, ``--json``
available), so the WS06 blocks call it with no extra plumbing.
"""

from __future__ import annotations

import io
import plistlib
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .. import config

#: The reseal-payload suffix the mac-app composition emits
#: (:mod:`shipit.release.bundle`) — inspected here without extraction.
RESEAL_SUFFIX = ".unsigned-app.tar.gz"

#: The plain-archive suffixes the archive composition emits (tarball on unix,
#: zip on windows) — read in place for the exec-bit-preserving main-binary
#: check. The reseal payload is also a ``.tar.gz`` but is handled separately
#: (:func:`_payload_main_binary`), so it is excluded by :func:`_is_plain_archive`.
PLAIN_ARCHIVE_SUFFIXES = (".tar.gz", ".zip")

#: The suffix the deb composition emits (:mod:`shipit.release.bundle`) —
#: inspected here in place, like every other archive-shaped tier.
DEB_SUFFIX = ".deb"

#: The ar container's global magic — a ``.deb`` IS an ar archive
#: (``debian-binary`` + ``control.tar.*`` + ``data.tar.*``).
_AR_MAGIC = b"!<arch>\n"


def expected_main_binary(artifact: config.Artifact) -> str:
    """The expected main-binary name for ``artifact`` — the fallback chain
    (workflows.lex §3.2): mainBinaryName (``main-binary``) → productName
    (``product-name``) → the first build target's declared package (its
    basename — ``./cmd/padz`` → ``padz``) → the artifact name. A package with
    no usable basename (a bare ``.``/``./``/``..``/``/``) is skipped, never
    asserted as the expected name. Pure."""
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
    """The check's typed outcome (ADR-0030): what was expected, what the
    tree actually carries as its main binaries, and the verdict. ``problem``
    carries the diagnosis when the tree itself is unreadable as a bundle
    (no main binary found, a ``.app`` with no determinable executable)."""

    tree: str
    expected: str
    actual: tuple[str, ...]
    ok: bool
    problem: str | None = None

    def to_dict(self) -> dict:
        """The ``--json`` field set — exactly the declared outputs."""
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
    """The main-binary name of a ``.app`` directory, or ``None`` when
    undeterminable: ``Contents/Info.plist``'s ``CFBundleExecutable`` (the
    authoritative declaration), else the SOLE file in ``Contents/MacOS``."""
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
    """The main-binary name of a reseal payload (``*.unsigned-app.tar.gz``),
    read from the tar WITHOUT extraction: the inner ``.app``'s
    ``Contents/Info.plist`` ``CFBundleExecutable``, else the sole member
    under ``Contents/MacOS``. ``None`` when undeterminable."""
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
    """Whether ``path`` is a plain archive-composition output (a ``.tar.gz``
    or ``.zip``) — NOT the reseal payload (also a ``.tar.gz``, handled by
    :func:`_payload_main_binary`). Pure."""
    if path.name.endswith(RESEAL_SUFFIX):
        return False
    return path.name.endswith(PLAIN_ARCHIVE_SUFFIXES)


def _archive_main_binary(archive: Path) -> str | None:
    """The main-binary name of a plain archive (``.tar.gz``/``.zip``), read
    WITHOUT extraction: the SOLE executable member (the exec bit stored in the
    archive header survives artifact transport, unlike the loose file's; a
    windows ``.zip`` carries no unix mode, so a ``.exe`` member counts by its
    suffix, its stem). ``None`` when undeterminable — zero candidates, or
    SEVERAL by ANY measure: the exec-bit and ``.exe`` tallies are counted
    TOGETHER, so an archive mixing a unix executable and a ``.exe`` is
    ambiguous and fails loudly rather than silently picking one. Only regular
    files are considered (a symlink with the exec bit is never a candidate,
    zip and tar alike); the docs the archive ships beside the binary carry no
    exec bit and are never candidates."""
    exec_members: list[str] = []
    exe_members: list[str] = []
    try:
        if archive.name.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    # Match tar's isfile() filter: a Unix-created entry that
                    # is not a regular file (symlink, device) is not a binary
                    # candidate even with the exec bit set. Windows-created
                    # entries carry no unix mode (mode == 0) and fall through
                    # to the .exe suffix check below.
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
    """The raw bytes of the deb's ``data.tar.*`` member, read from the ar
    container in place. An ar archive is the global magic followed by 60-byte
    member headers (name 16, mtime 12, uid 6, gid 6, mode 8, size 10, magic
    2) each fronting ``size`` data bytes padded to an even offset; a ``.deb``
    carries ``debian-binary``, ``control.tar.*`` and ``data.tar.*`` members.
    ``None`` when the file is not a well-formed ar archive, the data member
    is missing, or it is truncated."""
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
    """The main-binary name of a ``.deb``, read WITHOUT extraction: the SOLE
    executable regular member of its ``data.tar`` (the exec bit rides the
    inner tar's headers — transport-proof, exactly the plain-archive tier's
    premise; a doc/copyright member carries no exec bit and is never a
    candidate). ``None`` when undeterminable — an unreadable container, a
    data.tar compression the runtime cannot open, zero candidates, or
    several (ambiguity fails loudly, never a silent pick)."""
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


def _is_executable(path: Path) -> bool:
    """Whether ``path`` is a loose main-binary candidate: an executable
    regular file (or a ``.exe`` — windows carries no exec bit through)."""
    if not path.is_file() or path.is_symlink():
        return False
    if path.suffix == ".exe":
        return True
    # Archives ride the bundle tree too (the tarball the archive composition
    # wrote); an exec bit on one would misread it as a binary.
    if path.name.endswith((".tar.gz", ".zip", ".dmg", ".deb", ".whl")):
        return False
    return bool(path.stat().st_mode & 0o111)


def check_tree(tree: Path, expected: str) -> BundleVerdict:
    """Assert the bundle tree's MAIN binary is ``expected``. Pure reads.

    Discovery, in precedence order (an app bundle IS the main binary when
    one exists — the gen_fixtures scar lived inside the ``.app``):

    1. every ``*.app`` directory under ``tree`` (:func:`_app_main_binary`);
    2. every reseal payload (``*.unsigned-app.tar.gz``), read in place;
    3. every plain archive (``*.tar.gz``/``*.zip``), read in place
       (:func:`_archive_main_binary`) — the exec bit inside the archive
       survives artifact transport where the loose file's does not;
    4. every ``*.deb``, read in place (:func:`_deb_main_binary`) — its
       ``data.tar`` member's sole executable, same transport-proof shape;
    5. only when the tree carries none of the above: every loose executable
       file (``.exe`` counted by suffix, its stem compared).

    The verdict is ``ok`` exactly when at least one main binary was found
    and EVERY one is named ``expected``. An undeterminable app/payload/
    archive, or a tree with nothing to assert, fails with the diagnosis in
    ``problem``.
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
    if not apps and not payloads and not archives and not debs:
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
