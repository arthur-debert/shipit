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
  payload's inner ``.app``, or — when the tree bundles no app — its loose
  executables) must be named exactly the expected name. A tree with NO
  discoverable main binary fails loudly: "nothing to assert" is a wrong
  bundle, never a pass.

Pure over the filesystem (reads only); rendered by the verb with uniform
exit codes (0 pass, 1 fail — verdict + expected/actual on stderr, ``--json``
available), so the WS06 blocks call it with no extra plumbing.
"""

from __future__ import annotations

import plistlib
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .. import config

#: The reseal-payload suffix the mac-app composition emits
#: (:mod:`shipit.release.bundle`) — inspected here without extraction.
RESEAL_SUFFIX = ".unsigned-app.tar.gz"


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
    3. only when the tree carries neither: every loose executable file
       (``.exe`` counted by suffix, its stem compared).

    The verdict is ``ok`` exactly when at least one main binary was found
    and EVERY one is named ``expected``. An undeterminable app/payload, or
    a tree with nothing to assert, fails with the diagnosis in ``problem``.
    """
    actual: list[str] = []
    problems: list[str] = []
    apps = sorted(p for p in tree.rglob("*.app") if p.is_dir())
    payloads = sorted(p for p in tree.rglob(f"*{RESEAL_SUFFIX}") if p.is_file())
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
    if not apps and not payloads:
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
