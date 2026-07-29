"""The consumer-agnostic macOS signer: reopen, resign, reseal, notarize, staple.

Two legs — mac-app and archive — dispatched by :func:`detect_shape`.
See docs/adr/0040-workflow-blocks-invariants-in-blocks.md.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import logging
import math
import os
import re
import secrets as pysecrets
import shutil
import tarfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .. import execrun, redact
from . import ReleaseError

logger = logging.getLogger("shipit.release.sign")


CERT_SECRET = "APPLE_CERTIFICATE"

#: An EMPTY value is VALID — a passwordless ``.p12`` is legal PKCS#12.
CERT_PASSWORD_SECRET = "APPLE_CERTIFICATE_PASSWORD"

ASC_SECRETS: tuple[str, ...] = (
    "ASC_API_KEY_BASE64",
    "ASC_API_KEY_ID",
    "ASC_API_ISSUER_ID",
)

APPLE_ID_SECRETS: tuple[str, ...] = ("APPLE_ID", "APPLE_PASSWORD", "APPLE_TEAM_ID")

SIGNING_SECRETS: tuple[str, ...] = (CERT_SECRET, CERT_PASSWORD_SECRET)

#: Alternatives: a repo satisfies notarization with EITHER complete trio.
NOTARY_SECRET_SETS: tuple[tuple[str, ...], ...] = (ASC_SECRETS, APPLE_ID_SECRETS)


def required_secret_names() -> tuple[str, ...]:
    return (*SIGNING_SECRETS, *ASC_SECRETS, *APPLE_ID_SECRETS)


SIGN_CMD_TIMEOUT: float = 600.0

RESEAL_TIMEOUT: float = 1800.0

NOTARY_SUBMIT_TIMEOUT: float = 1800.0

NOTARY_POLL_TIMEOUT: float = 120.0

STAPLE_TIMEOUT: float = 600.0

POLL_INTERVAL: float = 30.0

DEFAULT_NOTARY_TIMEOUT_MIN: int = 60


@dataclass(frozen=True)
class SigningIdentitySource:
    cert_p12_base64: str
    cert_password: str


@dataclass(frozen=True)
class NotaryCredentials:
    """One resolved notary credential set, tagged ``"asc"`` or ``"apple-id"``."""

    style: str
    key_b64: str = ""
    key_id: str = ""
    issuer_id: str = ""
    apple_id: str = ""
    password: str = ""
    team_id: str = ""


def resolve_signing(env: Mapping[str, str]) -> SigningIdentitySource:
    """Resolve the signing cert from ``env``, or hard-fail; an empty password is valid."""
    cert = env.get(CERT_SECRET, "")
    if not cert:
        raise ReleaseError(
            f"signing requested but {CERT_SECRET} is not set — the signer "
            "hard-fails on missing secrets (missing-secrets detection belongs "
            "to preflight; the only unsigned path is the explicit --unsigned "
            "break-glass upstream, never a skip inside the signer)"
        )
    password = env.get(CERT_PASSWORD_SECRET, "")
    redact.register_secret(cert)
    redact.register_secret(password)
    return SigningIdentitySource(cert_p12_base64=cert, cert_password=password)


def resolve_notary(env: Mapping[str, str]) -> NotaryCredentials:
    """Resolve one notary style from ``env``; ASC wins, neither complete hard-fails."""
    asc = {name: env.get(name, "") for name in ASC_SECRETS}
    apple = {name: env.get(name, "") for name in APPLE_ID_SECRETS}
    if all(asc.values()):
        for value in asc.values():
            redact.register_secret(value)
        return NotaryCredentials(
            style="asc",
            key_b64=asc["ASC_API_KEY_BASE64"],
            key_id=asc["ASC_API_KEY_ID"],
            issuer_id=asc["ASC_API_ISSUER_ID"],
        )
    if all(apple.values()):
        for value in apple.values():
            redact.register_secret(value)
        return NotaryCredentials(
            style="apple-id",
            apple_id=apple["APPLE_ID"],
            password=apple["APPLE_PASSWORD"],
            team_id=apple["APPLE_TEAM_ID"],
        )
    asc_missing = ", ".join(n for n, v in asc.items() if not v)
    apple_missing = ", ".join(n for n, v in apple.items() if not v)
    raise ReleaseError(
        "notarization needs one complete credential set and neither is: "
        f"ASC API key trio (missing: {asc_missing}) or Apple-ID trio "
        f"(missing: {apple_missing}) — the signer hard-fails on missing "
        "secrets; there is no warn-and-skip"
    )


def notary_args(creds: NotaryCredentials, key_path: Path | None) -> list[str]:
    if creds.style == "asc":
        if key_path is None:
            raise ReleaseError(
                "ASC notarization requires the decoded .p8 key path but none "
                "was provided (internal error)"
            )
        return [
            "--key",
            str(key_path),
            "--key-id",
            creds.key_id,
            "--issuer",
            creds.issuer_id,
        ]
    return [
        "--apple-id",
        creds.apple_id,
        "--password",
        creds.password,
        "--team-id",
        creds.team_id,
    ]


def codesign_argv(
    identity: str,
    path: Path,
    entitlements: Path | None = None,
    keychain: Path | None = None,
) -> list[str]:
    """One codesign invocation: hardened runtime, secure timestamp, forced re-sign."""
    argv = ["codesign", "--force", "--sign", identity, "--options", "runtime"]
    argv.append("--timestamp")
    if keychain is not None:
        argv += ["--keychain", str(keychain)]
    if entitlements is not None:
        argv += ["--entitlements", str(entitlements)]
    argv.append(str(path))
    return argv


def sign_order(nested: Sequence[Path], app: Path) -> list[Path]:
    """Every nested path first, the ``.app`` LAST; a flat sign leaves nested code unhardened."""
    return [*nested, app]


#: ``.framework`` is OPAQUE; the rest are recursed into, since a root sign
#: covers only its main executable. The loadable roots must be listed: the
#: notary rejects a bundle whose root carries no signature.
BUNDLE_SUFFIXES: tuple[str, ...] = (
    ".framework",
    ".app",
    ".appex",
    ".xpc",
    ".plugin",
    ".bundle",
)

#: These two suffixes are also the shape of data-only RESOURCE bundles, which
#: are not signing units, so they are emitted only when they carry a Mach-O.
_CODE_GATED_SUFFIXES: frozenset[str] = frozenset({".plugin", ".bundle"})


def _contains_macho(root: Path, detect: Callable[[Path], bool]) -> bool:
    return any(
        p.is_file() and not p.is_symlink() and detect(p) for p in root.rglob("*")
    )


#: Mach-O magic numbers as the first four ON-DISK bytes.
_THIN_MAGICS = frozenset(
    {b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe"}
)
_FAT_MAGICS = frozenset({b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"})


def is_macho(path: Path) -> bool:
    """Whether ``path`` is Mach-O, by CONTENT; a fat hit is confirmed by its arch count."""
    try:
        with path.open("rb") as fh:
            head = fh.read(8)
    except OSError:
        return False
    if len(head) < 8:
        return False
    magic = head[:4]
    if magic in _THIN_MAGICS:
        return True
    if magic in _FAT_MAGICS:
        narch = int.from_bytes(head[4:8], "big")
        return 0 < narch < 0x20
    return False


def nested_signable(
    app: Path, *, detect: Callable[[Path], bool] = is_macho
) -> list[Path]:
    """The nested signable paths inside ``app``, deepest-first, EXCLUDING the top ``.app``.

    Files inside a ``.framework`` are skipped — the root is the signing unit — but
    files inside a helper ``.app``/``.appex``/``.xpc`` are not; symlinks are skipped.
    """
    entries: list[tuple[int, Path]] = []
    for path in sorted(app.rglob("*")):
        if path.is_symlink():
            continue
        rel = path.relative_to(app)
        depth = len(rel.parts) - 1
        if path.is_dir():
            if path.suffix in BUNDLE_SUFFIXES:
                if path.suffix in _CODE_GATED_SUFFIXES and not _contains_macho(
                    path, detect
                ):
                    continue  # a data-only resource bundle — not a signing unit
                entries.append((depth, path))
        elif path.is_file():
            if any(part.endswith(".framework") for part in rel.parts[:-1]):
                continue
            if detect(path):
                entries.append((depth, path))
    entries.sort(key=lambda entry: -entry[0])  # stable: keeps the sorted() tie order
    return [path for _, path in entries]


def _plist(entries: Mapping[str, bool]) -> str:
    body = "".join(
        f"\t<key>{key}</key>\n\t<{'true' if value else 'false'}/>\n"
        for key, value in entries.items()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        f"{body}"
        "</dict>\n</plist>\n"
    )


#: ``allow-jit`` alone: without it the app notarizes but crashes at launch, and
#: ``get-task-allow`` MUST stay off in a notarized release.
ELECTRON_APP_ENTITLEMENTS: Mapping[str, bool] = {
    "com.apple.security.cs.allow-jit": True,
}

#: Each helper is its OWN process. NEVER applied to a ``.appex``: a sandboxed
#: extension is not an electron helper and must not receive JIT.
ELECTRON_HELPER_ENTITLEMENTS: Mapping[str, bool] = {
    "com.apple.security.cs.allow-jit": True,
    "com.apple.security.inherit": True,
}


@dataclass(frozen=True)
class EntitlementsPolicy:
    """The ``.plist`` each code role receives, or ``None``; all-``None`` is non-electron."""

    app: Path | None = None
    helper: Path | None = None
    appex: Path | None = None

    @property
    def is_empty(self) -> bool:
        return self.app is None and self.helper is None and self.appex is None


NO_ENTITLEMENTS = EntitlementsPolicy()


def entitlements_for(
    path: Path, *, is_top_app: bool, policy: EntitlementsPolicy
) -> Path | None:
    if is_top_app:
        return policy.app
    if path.suffix == ".appex":
        return policy.appex
    if path.suffix == ".app":
        return policy.helper
    return None


#: Every electron app bundles this and nothing else does — the structural marker.
_ELECTRON_FRAMEWORK = "Electron Framework.framework"


def _is_electron(app: Path) -> bool:
    return any(
        p.is_dir() and p.name == _ELECTRON_FRAMEWORK
        for p in app.rglob(_ELECTRON_FRAMEWORK)
    )


def _electron_policy(scratch: Path) -> EntitlementsPolicy:
    scratch.mkdir(parents=True, exist_ok=True)
    app = scratch / "electron-app.entitlements.plist"
    helper = scratch / "electron-helper.entitlements.plist"
    app.write_text(_plist(ELECTRON_APP_ENTITLEMENTS), encoding="utf-8")
    helper.write_text(_plist(ELECTRON_HELPER_ENTITLEMENTS), encoding="utf-8")
    return EntitlementsPolicy(app=app, helper=helper)


RunCmd = Callable[[Sequence[str], float], execrun.ExecResult]


def _default_uniq() -> str:
    """Per-call unique suffix: fixed paths collided when the two passes ran in one job."""
    return f"{os.getpid()}-{pysecrets.token_hex(4)}"


def _default_pass() -> str:
    return pysecrets.token_hex(16)


@dataclass(frozen=True)
class SignRequest:
    """Everything one invocation needs; ``scratch`` holds every intermediate, and
    credential files under it are ALSO unlinked eagerly.
    """

    tree: Path
    out_dir: Path
    scratch: Path
    run_cmd: RunCmd
    env: Mapping[str, str]
    timeout_minutes: int = DEFAULT_NOTARY_TIMEOUT_MIN
    uniq: Callable[[], str] = _default_uniq
    mint_pass: Callable[[], str] = _default_pass
    sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True)
class SignResult:
    app: str
    dmg: str
    identity: str
    submission_id: str
    stapled: bool
    nested_signed: int

    def to_dict(self) -> dict:
        return {
            "app": self.app,
            "dmg": self.dmg,
            "identity": self.identity,
            "submission_id": self.submission_id,
            "stapled": self.stapled,
            "nested_signed": self.nested_signed,
        }


@dataclass(frozen=True)
class ArchiveSignResult:
    """The archive leg's output. No ``stapled`` field: a bare binary and the zip it
    rides in have no staple target — Gatekeeper verifies online.
    """

    archives: tuple[str, ...]
    binaries: tuple[str, ...]
    identity: str
    submission_ids: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "archives": list(self.archives),
            "binaries": list(self.binaries),
            "identity": self.identity,
            "submission_ids": list(self.submission_ids),
        }


#: The mac-app leg's dispatch signal, and the archive leg's exclusion.
RESEAL_SUFFIX = ".unsigned-app.tar.gz"


def _find_payload(tree: Path) -> Path:
    payloads = sorted(p for p in tree.rglob(f"*{RESEAL_SUFFIX}") if p.is_file())
    if not payloads:
        raise ReleaseError(
            f"no *{RESEAL_SUFFIX} under {tree} — the signer reopens a "
            "bundle tree carrying the reseal payload the mac-app composition "
            "emits (workflows.lex §3.1); was this an unsigned mac bundle?"
        )
    if len(payloads) > 1:
        names = ", ".join(str(p) for p in payloads)
        raise ReleaseError(
            f"expected one unsigned .app payload under {tree}, found "
            f"{len(payloads)}: {names} — the signer signs a single .app/.dmg "
            "pair, never a nondeterministic pick"
        )
    return payloads[0]


def _find_dmg(tree: Path) -> Path | None:
    """The tree's unsigned ``.dmg``, whose NAME the signed one stages under, or ``None``."""
    dmgs = sorted(p for p in tree.rglob("*.dmg") if p.is_file())
    if len(dmgs) > 1:
        names = ", ".join(str(p) for p in dmgs)
        raise ReleaseError(
            f"expected at most one .dmg under {tree}, found {len(dmgs)}: "
            f"{names} — a head-1 pick would sign one nondeterministically "
            "and silently drop the rest"
        )
    return dmgs[0] if dmgs else None


def _find_archives(tree: Path) -> list[Path]:
    return sorted(
        p
        for p in tree.rglob("*.tar.gz")
        if p.is_file() and not p.name.endswith(RESEAL_SUFFIX)
    )


def detect_shape(tree: Path) -> str:
    """Which leg ``tree`` routes to: ``"mac-app"`` (a reseal payload wins) or ``"archive"``."""
    if any(p.is_file() for p in tree.rglob(f"*{RESEAL_SUFFIX}")):
        return "mac-app"
    if _find_archives(tree):
        return "archive"
    raise ReleaseError(
        f"nothing signable under {tree}: no *{RESEAL_SUFFIX} reseal payload "
        "(the mac-app leg) and no plain .tar.gz archive bundle (the archive "
        "leg) — the signer reopens what the bundle stage composed "
        "(workflows.lex §3.1); was this tree bundled?"
    )


def _leaves_root(root: Path, base: Path, path: str) -> bool:
    """Whether a link TARGET leaves ``root`` once resolved against ``base``; ``..``
    resolves LEXICALLY, sound because member NAMES refuse ``..`` outright.
    """
    if os.path.isabs(path):
        return True
    resolved = os.path.normpath(os.path.join(str(base), path))
    root_str = os.path.normpath(str(root))
    return resolved != root_str and not resolved.startswith(root_str + os.sep)


def _name_escapes(member: tarfile.TarInfo) -> bool:
    """Whether ``member``'s path is absolute or carries a ``..`` COMPONENT — refused
    outright, since ``..`` through an extracted in-tree symlink escapes at write time.
    """
    name = member.name.replace("\\", "/")
    return os.path.isabs(name) or ".." in name.split("/")


def _is_special(member: tarfile.TarInfo) -> bool:
    return not (member.isfile() or member.isdir() or member.issym() or member.islnk())


def _target_escapes(root: Path, member: tarfile.TarInfo) -> bool:
    """Whether a link ``member``'s TARGET leaves ``root``, per structured ``linkname``."""
    base = root / Path(member.name).parent if member.issym() else root
    return _leaves_root(root, base, member.linkname)


def _is_confined(root: Path, member: tarfile.TarInfo, *, reject_links: bool) -> bool:
    if _name_escapes(member) or _is_special(member):
        return False
    if member.issym() or member.islnk():
        if reject_links:
            return False
        if _target_escapes(root, member):
            return False
    return True


def _confining_filter(
    root: Path, *, reject_links: bool
) -> Callable[[tarfile.TarInfo, str], tarfile.TarInfo]:
    """An ``extractall`` filter re-asserting confinement, preserving the rwx modes and
    symlinks tarfile's ``data`` filter would destroy; only setuid/setgid/sticky go.
    """

    def _filter(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo:
        if not _is_confined(root, member, reject_links=reject_links):
            raise ReleaseError(
                f"member {member.name!r} escaped the extraction dir mid-unpack"
                " — refusing"
            )
        member.mode &= ~0o7000  # strip setuid/setgid/sticky; keep rwx
        return member

    return _filter


def _untar_validated(
    archive: Path,
    work: Path,
    what: str,
    *,
    reject_links: bool = False,
) -> None:
    """Untar ``archive`` into ``work``, validating every member from the SAME handle.

    Refused before anything unpacks: an escaping member NAME, a special
    (device/FIFO) member, and — with ``reject_links`` — any link at all, else a
    link whose TARGET leaves ``work``.
    """
    work.mkdir(parents=True, exist_ok=True)
    root = work.resolve()
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = tar.getmembers()
            for member in members:
                if _name_escapes(member):
                    raise ReleaseError(
                        f"unsafe path in {what} {archive.name}: {member.name!r} "
                        "escapes the extraction dir (absolute or .. path) — "
                        "refusing to extract"
                    )
                if _is_special(member):
                    raise ReleaseError(
                        f"non-regular member in {what} {archive.name}: "
                        f"{member.name!r} — a character/block device, FIFO, or "
                        "other special entry; a signable bundle ships only "
                        "files, dirs, and links, refusing to extract"
                    )
                if member.issym() or member.islnk():
                    if reject_links:
                        raise ReleaseError(
                            f"non-regular member in {what} {archive.name}: "
                            f"{member.name!r} — a symlink or hardlink escapes "
                            "the extraction dir through its target; a raw-CLI "
                            "archive ships only files and dirs, refusing to "
                            "extract"
                        )
                    if _target_escapes(root, member):
                        raise ReleaseError(
                            f"link escaping {what} {archive.name}: "
                            f"{member.name!r} -> {member.linkname!r} — its "
                            "target resolves outside the extraction dir "
                            "(absolute or .. target); refusing to extract"
                        )
            tar.extractall(
                work,
                members=members,
                filter=_confining_filter(root, reject_links=reject_links),
            )
    except (tarfile.TarError, OSError) as exc:
        raise ReleaseError(f"cannot unpack {what} {archive.name}: {exc}") from exc


def _unpack(payload: Path, work: Path) -> Path:
    _untar_validated(payload, work, "reseal payload")
    apps = sorted(p for p in work.iterdir() if p.is_dir() and p.suffix == ".app")
    if len(apps) != 1:
        names = ", ".join(p.name for p in apps) or "none"
        raise ReleaseError(
            f"expected exactly one .app in {payload.name}, found {len(apps)} ({names})"
        )
    return apps[0]


def _parse_identity(stdout: str) -> str | None:
    match = re.search(r'^\s*\d+\)\s+\S+\s+"(.+)"', stdout, re.MULTILINE)
    return match.group(1) if match else None


def _parse_keychain_list(stdout: str) -> list[str]:
    """The keychain paths from ``security list-keychains``, order preserved. Matched
    quote-at-line-start to quote-at-line-end, since paths print unescaped.
    """
    return [
        match.group(1)
        for match in re.finditer(
            r'^[ \t]*"(.*?)"[ \t]*$', stdout, re.MULTILINE | re.DOTALL
        )
    ]


def _decode_b64(value: str, what: str) -> bytes:
    """Decode base64 secret material as a domain refusal, whitespace stripped first."""
    try:
        return base64.b64decode(re.sub(r"\s+", "", value), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ReleaseError(f"{what} is not valid base64: {exc}") from exc


def _sign_paths(
    paths: Sequence[Path],
    signing: SigningIdentitySource,
    req: SignRequest,
    *,
    policy: EntitlementsPolicy = NO_ENTITLEMENTS,
) -> str:
    """Codesign ``paths`` in order through a per-call temporary keychain.

    The keychain is spliced into the user SEARCH LIST for the pass: ``codesign
    --keychain`` alone does not reliably resolve an identity from a keychain
    outside the list on current macOS. The ``finally`` restores the snapshot, tears
    the keychain down, and unlinks the decoded ``.p12``.
    """
    uniq = req.uniq()
    keychain = req.scratch / f"signing-{uniq}.keychain-db"
    cert = req.scratch / f"cert-{uniq}.p12"
    kc_pass = req.mint_pass()
    redact.register_secret(kc_pass)
    run = req.run_cmd
    # None until the splice reads it, so the finally can tell "never spliced"
    # (a restore would WIPE the user's list) from "spliced, restore it".
    original_keychains: list[str] | None = None
    try:
        cert.write_bytes(_decode_b64(signing.cert_p12_base64, CERT_SECRET))
        run(
            ["security", "create-keychain", "-p", kc_pass, str(keychain)],
            SIGN_CMD_TIMEOUT,
        )
        run(
            ["security", "set-keychain-settings", "-lut", "3600", str(keychain)],
            SIGN_CMD_TIMEOUT,
        )
        run(
            ["security", "unlock-keychain", "-p", kc_pass, str(keychain)],
            SIGN_CMD_TIMEOUT,
        )
        run(
            [
                "security",
                "import",
                str(cert),
                "-k",
                str(keychain),
                "-P",
                signing.cert_password,
                "-T",
                "/usr/bin/codesign",
            ],
            SIGN_CMD_TIMEOUT,
        )
        run(
            [
                "security",
                "set-key-partition-list",
                "-S",
                "apple-tool:,apple:",
                "-s",
                "-k",
                kc_pass,
                str(keychain),
            ],
            SIGN_CMD_TIMEOUT,
        )
        listed = run(["security", "list-keychains", "-d", "user"], SIGN_CMD_TIMEOUT)
        original_keychains = _parse_keychain_list(listed.stdout)
        run(
            [
                "security",
                "list-keychains",
                "-d",
                "user",
                "-s",
                str(keychain),
                *original_keychains,
            ],
            SIGN_CMD_TIMEOUT,
        )
        found = run(
            ["security", "find-identity", "-v", "-p", "codesigning", str(keychain)],
            SIGN_CMD_TIMEOUT,
        )
        identity = _parse_identity(found.stdout)
        if identity is None:
            raise ReleaseError(
                f"no codesigning identity found in the keychain imported from "
                f"{CERT_SECRET} — is the .p12 a Developer ID Application cert?"
            )
        for index, path in enumerate(paths):
            if not path.exists():
                raise ReleaseError(f"path to sign not found: {path}")
            # The top app is the LAST path AND a `.app`, so a single-path dmg
            # or archive-binary pass can never evaluate as the top app.
            path_ent = entitlements_for(
                path,
                is_top_app=index == len(paths) - 1 and path.suffix == ".app",
                policy=policy,
            )
            run(codesign_argv(identity, path, path_ent, keychain), SIGN_CMD_TIMEOUT)
            run(["codesign", "--verify", "--strict", str(path)], SIGN_CMD_TIMEOUT)
        return identity
    finally:
        # Best-effort, so a cleanup failure never masks the aborting error.
        if original_keychains is not None:
            with contextlib.suppress(execrun.ExecError):
                run(
                    ["security", "list-keychains", "-d", "user", "-s"]
                    + original_keychains,
                    SIGN_CMD_TIMEOUT,
                )
        with contextlib.suppress(execrun.ExecError):
            run(["security", "delete-keychain", str(keychain)], SIGN_CMD_TIMEOUT)
        cert.unlink(missing_ok=True)
        keychain.unlink(missing_ok=True)


def _reseal(app: Path, dmg_out: Path, req: SignRequest) -> None:
    """Rebuild the ``.dmg`` from the SIGNED ``.app``; re-bundling would strip it."""
    stage = req.scratch / "reseal"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    shutil.copytree(app, stage / app.name, symlinks=True)
    (stage / "Applications").symlink_to("/Applications")
    dmg_out.unlink(missing_ok=True)
    req.run_cmd(
        [
            "hdiutil",
            "create",
            "-volname",
            app.stem,
            "-srcfolder",
            str(stage),
            "-ov",
            "-format",
            "UDZO",
            str(dmg_out),
        ],
        RESEAL_TIMEOUT,
    )


def _notarize(
    target: Path, creds: NotaryCredentials, req: SignRequest, *, staple: bool = True
) -> tuple[str, bool]:
    """``notarytool`` submit, poll, optionally staple. A transient ``info`` failure is
    one ``Unknown`` poll; a rejection or exhaustion hard-fails, a staple does not.
    """
    key_path: Path | None = None
    try:
        if creds.style == "asc":
            key_path = req.scratch / "AuthKey.p8"
            key_path.write_bytes(_decode_b64(creds.key_b64, "ASC_API_KEY_BASE64"))
        auth = notary_args(creds, key_path)
        submitted = req.run_cmd(
            [
                "xcrun",
                "notarytool",
                "submit",
                str(target),
                *auth,
                "--output-format",
                "json",
                "--no-wait",
            ],
            NOTARY_SUBMIT_TIMEOUT,
        )
        try:
            submission_id = str(json.loads(submitted.stdout)["id"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ReleaseError(
                f"notarytool submit returned no submission id: {exc}"
            ) from exc
        logger.info(
            "notary submission accepted for polling",
            extra={"submission_id": submission_id, "target": str(target)},
        )

        max_polls = math.ceil(req.timeout_minutes * 60 / POLL_INTERVAL)
        status = "Unknown"
        for poll in range(max_polls):
            try:
                info = req.run_cmd(
                    [
                        "xcrun",
                        "notarytool",
                        "info",
                        submission_id,
                        *auth,
                        "--output-format",
                        "json",
                    ],
                    NOTARY_POLL_TIMEOUT,
                )
                status = str(json.loads(info.stdout).get("status", "Unknown"))
            except (execrun.ExecError, ValueError):
                # One flaky poll is not a verdict — keep polling.
                status = "Unknown"
            if status == "Accepted":
                if not staple:
                    return submission_id, False
                stapled = True
                try:
                    req.run_cmd(
                        ["xcrun", "stapler", "staple", str(target)], STAPLE_TIMEOUT
                    )
                except execrun.ExecError:
                    stapled = False
                    logger.warning(
                        "staple failed (non-fatal — online Gatekeeper still verifies)",
                        exc_info=True,
                        extra={"submission_id": submission_id},
                    )
                return submission_id, stapled
            if status in ("Invalid", "Rejected"):
                detail = ""
                with contextlib.suppress(execrun.ExecError):
                    detail = req.run_cmd(
                        ["xcrun", "notarytool", "log", submission_id, *auth],
                        NOTARY_POLL_TIMEOUT,
                    ).stdout.strip()
                logger.error(
                    "notarization %s",
                    status,
                    extra={"submission_id": submission_id, "notary_log": detail},
                )
                raise ReleaseError(
                    f"notarization {status} for submission {submission_id}"
                    + (f": {detail}" if detail else "")
                )
            if poll < max_polls - 1:
                req.sleep(POLL_INTERVAL)
        raise ReleaseError(
            f"notarization unconfirmed after {req.timeout_minutes} min — "
            f"submission {submission_id} last status {status}. Codesigning "
            f"succeeded but notarization did NOT confirm; re-run the sign "
            f"stage (or check with `xcrun notarytool info {submission_id}`)"
        )
    finally:
        if key_path is not None:
            key_path.unlink(missing_ok=True)


def sign_bundle(req: SignRequest) -> SignResult:
    """The full reopen, resign, reseal, notarize, staple sequence over ``req.tree``;
    credentials and the timeout window resolve FIRST, with zero commands run.
    """
    signing = resolve_signing(req.env)
    notary = resolve_notary(req.env)
    if req.timeout_minutes < 1:
        raise ReleaseError(
            f"notary timeout must be at least 1 minute, got "
            f"{req.timeout_minutes} — a non-positive window would sign and "
            "submit, then never poll for the verdict"
        )

    payload = _find_payload(req.tree)
    original_dmg = _find_dmg(req.tree)

    app = _unpack(payload, req.scratch / "unpacked")
    nested = nested_signable(app)
    policy = _electron_policy(req.scratch) if _is_electron(app) else NO_ENTITLEMENTS
    if not policy.is_empty:
        logger.info(
            "electron app detected — applying shipit's JIT entitlements pair",
            extra={"app": app.name, "helpers": sum(p.suffix == ".app" for p in nested)},
        )
    identity = _sign_paths(sign_order(nested, app), signing, req, policy=policy)

    signed_dmg = req.scratch / "signed.dmg"
    _reseal(app, signed_dmg, req)
    # Its OWN temporary keychain; entitlements never apply to a disk image.
    _sign_paths([signed_dmg], signing, req)

    submission_id, stapled = _notarize(signed_dmg, notary, req)

    name = original_dmg.name if original_dmg is not None else f"{app.stem}.dmg"
    req.out_dir.mkdir(parents=True, exist_ok=True)
    dest = req.out_dir / name
    dest.unlink(missing_ok=True)
    shutil.copy2(signed_dmg, dest)

    return SignResult(
        app=app.name,
        dmg=str(dest.absolute()),
        identity=identity,
        submission_id=submission_id,
        stapled=stapled,
        nested_signed=len(nested),
    )


def sign_archives(req: SignRequest) -> ArchiveSignResult:
    """Sign and notarize the Mach-O inside every plain tarball, then re-emit each.

    All binaries sign in ONE :func:`_sign_paths` call, since the identity lives in a
    per-call keychain; notarization lands BEFORE any tarball is re-emitted.
    """
    signing = resolve_signing(req.env)
    notary = resolve_notary(req.env)
    if req.timeout_minutes < 1:
        raise ReleaseError(
            f"notary timeout must be at least 1 minute, got "
            f"{req.timeout_minutes} — a non-positive window would sign and "
            "submit, then never poll for the verdict"
        )

    archives = _find_archives(req.tree)
    if not archives:
        raise ReleaseError(
            f"no plain .tar.gz archive bundle under {req.tree} — the archive "
            "leg reopens the archive composition's darwin tarballs; was this "
            "tree bundled?"
        )

    unpacked: list[tuple[Path, Path]] = []
    binaries: list[Path] = []
    for index, archive in enumerate(archives):
        work = req.scratch / f"archive-{index}"
        _untar_validated(archive, work, "archive bundle", reject_links=True)
        machos = sorted(
            p
            for p in work.rglob("*")
            if p.is_file() and not p.is_symlink() and is_macho(p)
        )
        if not machos:
            raise ReleaseError(
                f"no Mach-O binary inside {archive.name} — the archive leg "
                "signs the archive composition's shipped darwin binaries "
                "(detected by content, never by name); is this a darwin "
                "bundle?"
            )
        unpacked.append((archive, work))
        binaries.extend(machos)

    identity = _sign_paths(binaries, signing, req)

    submission_ids: list[str] = []
    for binary in binaries:
        zip_path = req.scratch / f"{binary.name}-notarize.zip"
        zip_path.unlink(missing_ok=True)
        req.run_cmd(["zip", "-j", str(zip_path), str(binary)], SIGN_CMD_TIMEOUT)
        submission_id, _ = _notarize(zip_path, notary, req, staple=False)
        submission_ids.append(submission_id)
        zip_path.unlink(missing_ok=True)

    staged: list[str] = []
    req.out_dir.mkdir(parents=True, exist_ok=True)
    for index, (archive, work) in enumerate(unpacked):
        signed_tar = req.scratch / f"signed-{index}-{archive.name}"
        signed_tar.unlink(missing_ok=True)
        members = sorted(p.name for p in work.iterdir())
        # `--` terminates option parsing: a member name from an unpacked
        # external bundle is an operand, never a tar flag.
        req.run_cmd(
            ["tar", "-czf", str(signed_tar), "-C", str(work), "--", *members],
            SIGN_CMD_TIMEOUT,
        )
        # Copy to a temp beside dest, then rename: a copy failure leaves the
        # unsigned tarball untouched, and `os.replace` is one-filesystem only.
        dest = req.out_dir / archive.name
        tmp_dest = dest.with_name(f"{dest.name}.signing-tmp")
        tmp_dest.unlink(missing_ok=True)
        shutil.copy2(signed_tar, tmp_dest)
        os.replace(tmp_dest, dest)
        staged.append(str(dest.absolute()))

    return ArchiveSignResult(
        archives=tuple(staged),
        binaries=tuple(binary.name for binary in binaries),
        identity=identity,
        submission_ids=tuple(submission_ids),
    )
