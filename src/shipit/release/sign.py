"""The mac signer unit — reopen, resign, reseal, notarize, staple (TOL02-WS04).

``shipit release sign`` is the consumer-agnostic macOS signer behind the
scar-#1 invariant (workflows.lex §3.1): the model is **bundle(unsigned) →
sign-reopens-and-reseals**, never sign → bundle. The unit carries TWO legs,
one per signable bundle shape, dispatched by what the tree carries
(:func:`detect_shape`):

- the **mac-app leg** (:func:`sign_bundle`, TOL02-WS04) — the coupled
  ``.app``/``.dmg`` pair, reopened from the reseal payload;
- the **archive leg** (:func:`sign_archives`, TOL02-WS08 #779) — the archive
  composition's raw darwin CLI binaries, reopened from the ``.tar.gz``
  bundles. The legacy ``rust-cli.yml@v3`` sign + notarize steps are the
  parity contract: codesign each shipped Mach-O with the Developer ID cert,
  notarytool-submit each binary as a zip (a bare binary has NO staple
  target), then re-emit the tarball so the distributable carries the signed
  binary (artifact transport strips loose exec bits; the tar preserves them).

The mac-app leg: given a bundle tree carrying
an unsigned ``.app`` reseal payload (``<name>.unsigned-app.tar.gz`` — a tar
because artifact upload destroys a ``.app``'s symlinks and exec bits) and at
most one ``.dmg``, one invocation:

1. unpacks the unsigned ``.app`` (exactly one ``.app``, at most one ``.dmg``;
   zero or multiple is a hard error, never a nondeterministic pick);
2. enumerates the nested signable Mach-O inside it (extra executables, loose
   dylibs, helper bundles — recursively, because signing a nested helper's
   root covers only ITS main executable);
3. codesigns inner-first with the ``.app`` LAST, hardened runtime + secure
   timestamp on every Mach-O (a flat sign leaves nested code unhardened and
   the notary rejects it; ``codesign --deep`` is not used — Apple discourages
   it for distribution and it mis-applies entitlements);
4. reseals the ``.dmg`` FROM the signed ``.app`` via ``hdiutil`` (re-bundling
   would strip the signature), then codesigns the resealed ``.dmg``;
5. ``notarytool`` submit / poll / staple against the signed ``.dmg``, and
   stages it under the original dmg filename.

Fork-by-copy per ADR-0001/ADR-0010 from the legacy ``sign-notarize-mac.yml``
reusable workflow and its composites (``sign-mac``, ``notarize-mac``,
``unpack-unsigned-app``, ``enumerate-macho``, ``reseal-mac-dmg``) — and, for
the archive leg, from ``rust-cli.yml@v3``'s "Sign macOS binaries" /
"Notarize signed binaries" steps: bash stayed
where bash was right — ``codesign`` / ``security`` / ``hdiutil`` / ``xcrun``
remain the tools — but as ONE shipit-owned, locally-runnable unit: a release
engineer with a mac and the certs runs the whole
reopen→resign→reseal→notarize→staple sequence on a laptop, no CI push.

The unit makes ZERO tauri or electron assumptions: it operates on the
``.app``/``.dmg`` pair; whatever bundler produced the unsigned input is the
caller's business. The ``assert-bundle`` integrity guard at the signer's
ENTRY is the WS06 ``wf-sign-mac`` block's job (ADR-0040) — this unit stays a
pure ``.app``/``.dmg`` transformer.

Credentials (the secret-name constants below are this unit's declaration to
the TOL01 secrets-requirements derivation — registry entries declare the
names they need, the required set is derived from what the repo ships):

- signing always uses the Developer ID ``.p12``
  (:data:`CERT_SECRET`/:data:`CERT_PASSWORD_SECRET`) imported into a
  per-invocation TEMPORARY keychain torn down on every exit path, with
  unique keychain/cert paths per call so the ``.app`` and ``.dmg`` signing
  passes in one run cannot collide (the legacy fixed path collided with exit
  48). An EMPTY cert password is VALID — a passwordless ``.p12`` is legal
  PKCS#12, and gating a skip on the password once silently shipped
  ad-hoc-signed binaries;
- notarization accepts either credential style behind one flag array
  (:func:`notary_args`): the ASC API key trio (:data:`ASC_SECRETS`, wins when
  both are present) or the Apple-ID trio (:data:`APPLE_ID_SECRETS`). Decoded
  credential material (``.p12``, ``.p8``) is wiped on any exit.

One deliberate behaviour change from the legacy composites (PRD tol01 stories
28–29), on BOTH legs: the warn-and-skip on missing cert/notary credentials is
GONE (the legacy rust-cli notarize step skipped with a warning when the ASC
key was absent, and knew only the ASC trio — the archive leg hard-fails like
the mac-app leg and accepts either notary trio). The
unit HARD-FAILS when invoked without its secrets, naming the missing names —
missing-secrets detection belongs to ``preflight``, and the only unsigned
path is the explicit ``--unsigned`` break-glass decided upstream, never an
ambient skip inside the signer. The legacy notary-timeout soft-pass is gone
for the same reason: an unconfirmed notarization is a FAILURE (ADR-0009's
partial-release prevention), resumable by re-running the stage.

**This unit is act-untestable**: ``codesign``/``notarytool`` need a real
macOS runner and real Apple credentials, so no act smoke exists for it.
Remote verification is the TOL02-WS07 lex rc (and the full ``.app``/``.dmg``
leg with phos-app in ADP02). What CAN be tested locally, and is
(``tests/test_release_sign.py``): the pure argument assembly (sign-order
construction, credential-set resolution, notary flag trio selection) as
fixture tests, and the full recorded command-line sequence — including the
hard-fail refusals — through the one Exec seam (ADR-0028): every external
command runs through the injected runner, and the ``codesign`` /
``security`` / ``xcrun`` / ``hdiutil`` / ``tar`` argv literals below are
those tools' one assembly point, whitelisted in the mechanized argv sweep
(``tests/test_tool_argv_sweep.py``).

The effectful shell is ``shipit release sign`` (:mod:`shipit.verbs.release`),
which owns the scratch-dir lifecycle and the terminal rendering.
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

# --------------------------------------------------------------------------
# The unit's secret-name declaration (the secrets-derivation registry's input)
# --------------------------------------------------------------------------

#: The Developer ID Application certificate, base64-encoded ``.p12``. Signing
#: is impossible without it — a missing value is a hard fail naming it.
CERT_SECRET = "APPLE_CERTIFICATE"

#: The ``.p12`` password. The NAME is declared (the derivation registry syncs
#: it), but an EMPTY value is VALID — passwordless ``.p12`` is legal PKCS#12.
CERT_PASSWORD_SECRET = "APPLE_CERTIFICATE_PASSWORD"

#: Notary credential style 1 — the App Store Connect API key trio (base64
#: ``.p8`` + key id + issuer UUID). WINS when both styles are present.
ASC_SECRETS: tuple[str, ...] = (
    "ASC_API_KEY_BASE64",
    "ASC_API_KEY_ID",
    "ASC_API_ISSUER_ID",
)

#: Notary credential style 2 — Apple-ID email + app-specific password + team
#: id. Used only when the ASC trio is incomplete.
APPLE_ID_SECRETS: tuple[str, ...] = ("APPLE_ID", "APPLE_PASSWORD", "APPLE_TEAM_ID")

#: The signing pair, as the derivation registry consumes it.
SIGNING_SECRETS: tuple[str, ...] = (CERT_SECRET, CERT_PASSWORD_SECRET)

#: The two notary credential alternatives, as the derivation registry
#: consumes them: a repo satisfies notarization with EITHER complete trio.
NOTARY_SECRET_SETS: tuple[tuple[str, ...], ...] = (ASC_SECRETS, APPLE_ID_SECRETS)


def required_secret_names() -> tuple[str, ...]:
    """Every secret NAME this unit can consume, flat — the sync-side view
    (gh-setup provisions all of them; presence VALIDATION uses the structured
    :data:`SIGNING_SECRETS` / :data:`NOTARY_SECRET_SETS` instead, because the
    notary trios are alternatives, not a conjunction). Pure."""
    return (*SIGNING_SECRETS, *ASC_SECRETS, *APPLE_ID_SECRETS)


# --------------------------------------------------------------------------
# Timeouts — every Exec states its bound deliberately (ADR-0028)
# --------------------------------------------------------------------------

#: ``codesign`` / ``security`` and the payload untar: local, quick — but a
#: codesign --timestamp round-trips to Apple's timestamp service, so the
#: bound is minutes, not the runner default alone.
SIGN_CMD_TIMEOUT: float = 600.0

#: ``hdiutil create`` compresses the whole ``.app`` into the UDZO image — a
#: big app takes real minutes.
RESEAL_TIMEOUT: float = 1800.0

#: ``notarytool submit`` UPLOADS the ``.dmg`` to Apple before returning the
#: submission id (even with ``--no-wait``).
NOTARY_SUBMIT_TIMEOUT: float = 1800.0

#: One ``notarytool info`` poll — a single API round-trip.
NOTARY_POLL_TIMEOUT: float = 120.0

#: ``stapler staple`` — one ticket download + rewrite.
STAPLE_TIMEOUT: float = 600.0

#: Seconds between notary polls (the legacy composite's cadence).
POLL_INTERVAL: float = 30.0

#: Default max minutes to wait for Apple's notary verdict.
DEFAULT_NOTARY_TIMEOUT_MIN: int = 60


# --------------------------------------------------------------------------
# Credential resolution (pure over the injected env)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SigningIdentitySource:
    """The Developer ID cert material: base64 ``.p12`` + password (empty VALID)."""

    cert_p12_base64: str
    cert_password: str


@dataclass(frozen=True)
class NotaryCredentials:
    """One resolved notary credential set, tagged with its style.

    ``style`` is ``"asc"`` (key material in ``key_b64``/``key_id``/
    ``issuer_id``) or ``"apple-id"`` (``apple_id``/``password``/``team_id``)
    — the one axis :func:`notary_args` branches on.
    """

    style: str
    key_b64: str = ""
    key_id: str = ""
    issuer_id: str = ""
    apple_id: str = ""
    password: str = ""
    team_id: str = ""


def resolve_signing(env: Mapping[str, str]) -> SigningIdentitySource:
    """Resolve the signing cert from ``env``, or hard-fail naming the name.

    No warn-and-skip exists here (PRD stories 28–29): a signer invoked
    without its cert is a caller bug preflight should have caught. The empty
    password is deliberately accepted — see :data:`CERT_PASSWORD_SECRET`.
    Both values are registered with the central redactor at the one moment
    the unit provably holds them.
    """
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
    """Resolve one notary credential style from ``env`` — ASC wins when both
    complete sets are present; neither complete is a hard fail NAMING the
    missing names of both alternatives. Every resolved value is registered
    with the central redactor (the Apple-ID password rides ``notarytool``
    argv, so masking is load-bearing, not hygiene)."""
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
    """The shared ``notarytool`` auth flag array — built once, reused by
    every submit/info/log call. ``key_path`` is where the decoded ``.p8``
    lives for the ASC style (unused for Apple-ID). Pure."""
    if creds.style == "asc":
        if key_path is None:
            # An internal invariant (_notarize always decodes the .p8 first),
            # enforced explicitly rather than via `assert` — which `python -O`
            # strips, letting a None flow into a confusing notarytool call.
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


# --------------------------------------------------------------------------
# Sign-order assembly (pure) and Mach-O enumeration
# --------------------------------------------------------------------------


def codesign_argv(
    identity: str,
    path: Path,
    entitlements: Path | None = None,
    keychain: Path | None = None,
) -> list[str]:
    """One codesign invocation: hardened runtime + secure timestamp, forced
    re-sign (the bundler may have ad-hoc-signed), the signing identity pinned
    to ``keychain`` via ``--keychain`` (found there without touching the user's
    global keychain search list), optional entitlements. Pure."""
    argv = ["codesign", "--force", "--sign", identity, "--options", "runtime"]
    argv.append("--timestamp")
    if keychain is not None:
        argv += ["--keychain", str(keychain)]
    if entitlements is not None:
        argv += ["--entitlements", str(entitlements)]
    argv.append(str(path))
    return argv


def sign_order(nested: Sequence[Path], app: Path) -> list[Path]:
    """The signing order: every nested path first, the ``.app`` LAST — a flat
    sign leaves nested code unhardened and the notary rejects it. Pure."""
    return [*nested, app]


#: The nested code-bundle roots the enumeration recognises. ``.framework`` is
#: OPAQUE (the root is the signing unit; its internals are never signed
#: individually); ``.app`` / ``.appex`` / ``.xpc`` / ``.plugin`` / ``.bundle``
#: are RECURSED into — their root sign covers only their main executable, so
#: their inner extra Mach-O is enumerated too. The loadable ``.plugin`` /
#: ``.bundle`` roots MUST be listed: signing only their inner Mach-O and never
#: the bundle root leaves the root unsigned, which the notary/Gatekeeper
#: rejects (the signature must land on the bundle root).
BUNDLE_SUFFIXES: tuple[str, ...] = (
    ".framework",
    ".app",
    ".appex",
    ".xpc",
    ".plugin",
    ".bundle",
)

#: The bundle suffixes emitted ONLY when the directory actually carries code.
#: ``.app`` / ``.appex`` / ``.xpc`` / ``.framework`` are code bundles by
#: definition, but ``.plugin`` / ``.bundle`` are also the shape of data-only
#: RESOURCE bundles (icons, nibs, plists — no Mach-O anywhere). Those must not
#: be handed to ``codesign``: a data-only bundle root is not a signing unit
#: and signing it can fail the pass. So a ``.plugin`` / ``.bundle`` root is
#: emitted only when it contains a Mach-O (:func:`_contains_macho`).
_CODE_GATED_SUFFIXES: frozenset[str] = frozenset({".plugin", ".bundle"})


def _contains_macho(root: Path, detect: Callable[[Path], bool]) -> bool:
    """Whether ``root`` carries any Mach-O file — the code-bundle test for the
    loadable ``.plugin`` / ``.bundle`` roots. Detected by CONTENT (``detect``),
    never by name; a data-only resource bundle has none and is NOT a signing
    unit."""
    return any(
        p.is_file() and not p.is_symlink() and detect(p) for p in root.rglob("*")
    )


#: Mach-O magic numbers, as the first four ON-DISK bytes — thin 32/64-bit in
#: both byte orders, plus the fat/universal header (always big-endian on
#: disk: ``ca fe ba be`` / ``ca fe ba bf``).
_THIN_MAGICS = frozenset(
    {b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe"}
)
_FAT_MAGICS = frozenset({b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"})


def is_macho(path: Path) -> bool:
    """Whether ``path`` is Mach-O, detected by CONTENT (magic bytes), never by
    name — the legacy ``file``-based detection without the subprocess.

    The fat magic collides with Java's class-file magic (``cafebabe``), so a
    fat hit is confirmed by the next field: a fat header's arch count is tiny
    (a handful of slices) where a class file's version bytes read as a large
    big-endian int. Unreadable / short files are simply not Mach-O.
    """
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
    """The nested signable paths inside ``app``, inner-first (deepest first),
    EXCLUDING the top-level ``.app`` — the caller appends it and signs it last.

    The legacy ``enumerate-macho.sh`` contract, ported whole:

    - nested code-bundle roots (:data:`BUNDLE_SUFFIXES`) are emitted once at
      their root; being shallower than their own contents, each root lands
      AFTER them — the correct inner-out order, main-executable re-sign last.
      A loadable ``.plugin`` / ``.bundle`` root is emitted only when it
      carries code (:data:`_CODE_GATED_SUFFIXES`): a data-only resource
      ``.bundle`` is not a signing unit and must never reach ``codesign``;
    - Mach-O FILES are detected by content (``detect``), excluding anything
      inside a ``.framework`` (opaque — its root is the signing unit) but
      INCLUDING files inside helper ``.app``/``.appex``/``.xpc`` bundles
      (that is the recursion: a helper's extra executables must be signed
      too);
    - symlinks are skipped (the legacy ``find -type f/-type d`` behaviour —
      a ``Versions/Current`` link must not be signed as a second copy);
    - ordering is deterministic: deepest first, lexicographic among equal
      depths.
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


# --------------------------------------------------------------------------
# The runner seam and the request/result values
# --------------------------------------------------------------------------

#: The runner seam every external command goes through — ``(argv, timeout) ->
#: ExecResult`` with check=True semantics (a failing command raises
#: :class:`~shipit.execrun.ExecError`). The verb injects the production
#: runner; tests inject a recorder with canned stdouts.
RunCmd = Callable[[Sequence[str], float], execrun.ExecResult]


def _default_uniq() -> str:
    """Per-call unique suffix for keychain/cert paths — pid + random hex, the
    legacy recipe (fixed paths collided with exit 48 when the ``.app`` and
    ``.dmg`` passes ran in one job)."""
    return f"{os.getpid()}-{pysecrets.token_hex(4)}"


def _default_pass() -> str:
    """A fresh throwaway password for the temporary keychain."""
    return pysecrets.token_hex(16)


@dataclass(frozen=True)
class SignRequest:
    """Everything one signer invocation needs.

    ``tree`` is the bundle tree carrying the reseal payload (+ optional
    unsigned ``.dmg``); ``out_dir`` is where the signed ``.dmg`` is staged
    under the original dmg filename; ``scratch`` is the caller-owned
    temporary dir every intermediate (unpacked ``.app``, keychains, decoded
    ``.p12``/``.p8``, the pre-stage ``signed.dmg``) lives under — the shell
    removes it whole on any exit, and the credential files are ALSO unlinked
    eagerly in ``finally`` blocks so decoded material never outlives its use.
    ``env`` is the secrets source (injected; ``os.environ`` in production).
    ``uniq`` / ``mint_pass`` / ``sleep`` are the nondeterminism seams the
    tests pin.
    """

    tree: Path
    out_dir: Path
    scratch: Path
    run_cmd: RunCmd
    env: Mapping[str, str]
    entitlements: Path | None = None
    timeout_minutes: int = DEFAULT_NOTARY_TIMEOUT_MIN
    uniq: Callable[[], str] = _default_uniq
    mint_pass: Callable[[], str] = _default_pass
    sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True)
class SignResult:
    """The signer's uniform, typed output (ADR-0030).

    ``dmg`` is the ABSOLUTE staged path of the signed, notarized ``.dmg``
    (under the original dmg filename); ``nested_signed`` how many nested
    signable paths (Mach-O files AND nested bundle roots) preceded the
    ``.app``; ``stapled`` whether the ticket was stapled (a staple failure is
    non-fatal — online Gatekeeper still verifies).
    """

    app: str
    dmg: str
    identity: str
    submission_id: str
    stapled: bool
    nested_signed: int

    def to_dict(self) -> dict:
        """The ``--json`` field set — exactly the declared outputs."""
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
    """The archive leg's uniform, typed output (ADR-0030).

    ``archives`` are the ABSOLUTE staged paths of the re-emitted tarballs
    (each under its original filename); ``binaries`` the signed Mach-O
    names, discovery order; ``submission_ids`` one notary submission per
    signed binary, same order. There is no ``stapled`` field on purpose: a
    bare binary (and the zip it is submitted in) has no staple target —
    Gatekeeper verifies the notarization online.
    """

    archives: tuple[str, ...]
    binaries: tuple[str, ...]
    identity: str
    submission_ids: tuple[str, ...]

    def to_dict(self) -> dict:
        """The ``--json`` field set — exactly the declared outputs."""
        return {
            "archives": list(self.archives),
            "binaries": list(self.binaries),
            "identity": self.identity,
            "submission_ids": list(self.submission_ids),
        }


# --------------------------------------------------------------------------
# The stages
# --------------------------------------------------------------------------


#: The reseal-payload suffix the mac-app composition emits — the mac-app
#: leg's dispatch signal (:func:`detect_shape`), and the exclusion that keeps
#: the archive leg from misreading a payload as a plain archive (the same
#: split :mod:`shipit.release.integrity` draws).
RESEAL_SUFFIX = ".unsigned-app.tar.gz"


def _find_payload(tree: Path) -> Path:
    """The tree's ONE reseal payload; zero or multiple is a hard error."""
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
    """The tree's unsigned ``.dmg`` (its NAME is what the signed one stages
    under), or ``None``; more than one is a hard error."""
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
    """The tree's plain archive tarballs — the archive composition's darwin
    outputs: every ``.tar.gz`` that is NOT a reseal payload, sorted. The
    composition's windows ``.zip`` twin never reaches the signer (``sign``
    is darwin-only by declaration), so only tarballs are archive-leg inputs.
    Pure reads."""
    return sorted(
        p
        for p in tree.rglob("*.tar.gz")
        if p.is_file() and not p.name.endswith(RESEAL_SUFFIX)
    )


def detect_shape(tree: Path) -> str:
    """Which signer leg ``tree`` routes to: ``"mac-app"`` when it carries a
    reseal payload (the payload is the explicit mac-app signal, so it wins
    if both shapes ever appear), ``"archive"`` when it carries plain
    ``.tar.gz`` archive bundles, and a hard refusal naming both shapes
    otherwise — the signer reopens what the bundle stage composed, never
    guesses. Pure reads; the verb dispatches on the answer."""
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
    """Whether ``path`` — an archive member NAME or a link TARGET — lands
    OUTSIDE ``root`` once resolved against ``base`` (used when ``path`` is
    relative). An absolute path always leaves; ``..`` segments resolve
    LEXICALLY (a textual ``normpath``, never following an on-disk symlink, so a
    hostile link already unpacked cannot redirect the check). Pure."""
    if os.path.isabs(path):
        return True
    resolved = os.path.normpath(os.path.join(str(base), path))
    root_str = os.path.normpath(str(root))
    return resolved != root_str and not resolved.startswith(root_str + os.sep)


def _name_escapes(root: Path, member: tarfile.TarInfo) -> bool:
    """Whether ``member``'s archive path would write OUTSIDE ``root`` — an
    ABSOLUTE name or one that climbs out with ``..`` (classic tar path
    traversal). The EXACT stored name is checked, so a legitimate literal like
    ``.. `` (dot-dot-space) is the confined child it names, never a stripped
    ``..``. Pure."""
    return _leaves_root(root, root, member.name)


def _target_escapes(root: Path, member: tarfile.TarInfo) -> bool:
    """Whether a link ``member``'s TARGET resolves OUTSIDE ``root``. The target
    is read from the archive's STRUCTURED metadata (``member.linkname``), so a
    member name OR target that itself contains the display delimiters
    (``" -> "`` for a symlink, ``" link to "`` for a hardlink) cannot skew the
    parse the way a ``tar -tvzf`` text listing could. A symlink target resolves
    against the link's OWN directory; a hardlink target against the archive
    root. Pure."""
    base = root / Path(member.name).parent if member.issym() else root
    return _leaves_root(root, base, member.linkname)


def _is_confined(root: Path, member: tarfile.TarInfo, *, reject_links: bool) -> bool:
    """Whether ``member`` is safe to extract under ``root``: its name stays in
    tree AND, for a link, either links are rejected outright (``reject_links``,
    the archive leg) or its target also stays in tree (the mac-app leg). The
    per-member gate the extraction filter re-asserts after the scan. Pure."""
    if _name_escapes(root, member):
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
    """An ``extractall`` filter that re-asserts each member is confined to
    ``root`` before it is written — the pre-extraction scan already refused any
    escaper, so this only ever fires on a race, and otherwise passes the member
    through UNCHANGED to preserve the bundle's exact modes and symlinks (unlike
    tarfile's sanitising ``data`` filter, which a re-signed ``.app`` cannot
    survive)."""

    def _filter(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo:
        if not _is_confined(root, member, reject_links=reject_links):
            raise ReleaseError(
                f"member {member.name!r} escaped the extraction dir mid-unpack"
                " — refusing"
            )
        return member

    return _filter


def _untar_validated(
    archive: Path,
    work: Path,
    what: str,
    *,
    reject_links: bool = False,
) -> None:
    """Untar ``archive`` into ``work`` in a SINGLE structured pass: open the
    archive once, validate every member against its ``tarfile`` metadata, then
    extract from that SAME handle — the check and the extraction see one
    identical member set (nothing on disk is re-listed or re-opened between
    them), and a large payload is decompressed once, not three times.

    Two escapes are refused BEFORE anything is unpacked:

    * A member NAME that is absolute or climbs out with ``..`` would let a
      tampered or garbled ``what`` write OUTSIDE ``work`` (tar path traversal).
    * A link member escapes through its TARGET even when its name is confined.
      With ``reject_links`` (the archive leg — a raw-CLI tarball has no business
      carrying links) ANY symlink or hardlink is refused. Without it (the
      mac-app leg — a resealed ``.app`` legitimately carries the framework
      symlinks Apple's bundle layout requires) a link is allowed only while its
      TARGET resolves UNDER ``work``; an absolute or ``..`` target is refused.

    Targets come from structured ``linkname`` metadata, never a parsed text
    listing, so a member name or target that itself contains ``" -> "`` /
    ``" link to "`` cannot smuggle an escaping link past the check. The
    extraction re-asserts the same confinement per member
    (:func:`_confining_filter`) and otherwise keeps every member faithfully
    intact."""
    work.mkdir(parents=True, exist_ok=True)
    root = work.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            if _name_escapes(root, member):
                raise ReleaseError(
                    f"unsafe path in {what} {archive.name}: {member.name!r} "
                    "escapes the extraction dir (absolute or .. path) — "
                    "refusing to extract"
                )
            if member.issym() or member.islnk():
                if reject_links:
                    raise ReleaseError(
                        f"non-regular member in {what} {archive.name}: "
                        f"{member.name!r} — a symlink or hardlink escapes the "
                        "extraction dir through its target; a raw-CLI archive "
                        "ships only files and dirs, refusing to extract"
                    )
                if _target_escapes(root, member):
                    raise ReleaseError(
                        f"link escaping {what} {archive.name}: {member.name!r}"
                        f" -> {member.linkname!r} — its target resolves outside"
                        " the extraction dir (absolute or .. target); refusing "
                        "to extract"
                    )
        tar.extractall(
            work,
            members=members,
            filter=_confining_filter(root, reject_links=reject_links),
        )


def _unpack(payload: Path, work: Path) -> Path:
    """Untar the reseal payload into ``work`` (validated first —
    :func:`_untar_validated`) and return the ONE extracted ``.app``; zero or
    multiple is a hard error."""
    _untar_validated(payload, work, "reseal payload")
    apps = sorted(p for p in work.iterdir() if p.is_dir() and p.suffix == ".app")
    if len(apps) != 1:
        names = ", ".join(p.name for p in apps) or "none"
        raise ReleaseError(
            f"expected exactly one .app in {payload.name}, found {len(apps)} ({names})"
        )
    return apps[0]


def _parse_identity(stdout: str) -> str | None:
    """The first codesigning identity name out of ``security find-identity -v``
    output (``  1) <hash> "<name>"``), or ``None`` when the keychain holds
    none. Pure."""
    match = re.search(r'^\s*\d+\)\s+\S+\s+"(.+)"', stdout, re.MULTILINE)
    return match.group(1) if match else None


def _decode_b64(value: str, what: str) -> bytes:
    """Decode base64 secret material, re-shaping a garbage value as the
    domain refusal (never a raw ``binascii.Error`` traceback).

    Whitespace is stripped before decoding: secrets injected through the
    environment routinely carry a trailing newline (or wrapped lines), and
    ``validate=True`` — which rejects any non-alphabet byte — would fail those
    otherwise-valid base64 payloads. Stripping first keeps the strict check on
    genuinely corrupt input while tolerating the newline."""
    try:
        return base64.b64decode(re.sub(r"\s+", "", value), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ReleaseError(f"{what} is not valid base64: {exc}") from exc


def _sign_paths(
    paths: Sequence[Path],
    signing: SigningIdentitySource,
    req: SignRequest,
    *,
    entitlements: Path | None = None,
) -> str:
    """Codesign ``paths`` in order through a per-call temporary keychain.

    The full legacy ``sign-mac`` lifecycle: unique keychain + cert paths per
    call (the ``.app`` and ``.dmg`` passes in one run must not collide),
    create/unlock/import/partition-list, identity discovery IN that keychain,
    then per path a forced hardened-runtime + timestamp sign followed by
    ``codesign --verify --strict``. The signing keychain is pinned on every
    ``codesign`` via ``--keychain`` rather than prepended to the user's global
    keychain search list: a search-list mutation outlives a ``SIGKILL`` (or a
    power loss) that skips the ``finally`` teardown and would then permanently
    pollute a release engineer's laptop. Entitlements ride ONLY the final path
    (the top-level ``.app``); a nested framework or helper must never carry the
    app's entitlements — that mis-application is exactly what the notary
    rejects (and why ``codesign --deep`` is shunned). The ``finally`` tears the
    keychain down and unlinks the decoded ``.p12`` on every exit path — success
    or failure.
    """
    uniq = req.uniq()
    keychain = req.scratch / f"signing-{uniq}.keychain-db"
    cert = req.scratch / f"cert-{uniq}.p12"
    kc_pass = req.mint_pass()
    # The throwaway password rides `security` argv — register it so the Exec
    # records can never carry it in clear.
    redact.register_secret(kc_pass)
    run = req.run_cmd
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
        # -P "" is the passwordless-.p12 import — deliberately valid.
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
            # Entitlements belong ONLY on the top-level .app — the LAST path
            # (sign_order appends it). Applying them to nested frameworks and
            # helper Mach-O is the mis-application the notary rejects.
            path_ent = entitlements if index == len(paths) - 1 else None
            run(codesign_argv(identity, path, path_ent, keychain), SIGN_CMD_TIMEOUT)
            run(["codesign", "--verify", "--strict", str(path)], SIGN_CMD_TIMEOUT)
        return identity
    finally:
        # Teardown on EVERY exit: delete-keychain removes the file and its
        # search-list entry; best-effort so a cleanup failure never masks the
        # error that aborted the pass. The decoded cert must not outlive it.
        with contextlib.suppress(execrun.ExecError):
            run(["security", "delete-keychain", str(keychain)], SIGN_CMD_TIMEOUT)
        cert.unlink(missing_ok=True)
        keychain.unlink(missing_ok=True)


def _reseal(app: Path, dmg_out: Path, req: SignRequest) -> None:
    """Rebuild the ``.dmg`` from the SIGNED ``.app`` via ``hdiutil`` — never
    re-bundle, which would strip the signature (workflows.lex §3.1). The
    staged volume carries the ``.app`` (symlinks intact) plus the
    conventional ``/Applications`` link."""
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
    """``notarytool`` submit → poll (→ staple) against ``target``.

    Returns ``(submission_id, stapled)``. Submit is ``--no-wait`` + a poll
    loop cadenced at :data:`POLL_INTERVAL` (the poll count derives from it,
    never a hard-coded factor); ``req.timeout_minutes`` is >= 1 by the time
    this runs (the leg entries refuse a non-positive window up front, so
    the loop always polls at least once). A transient ``info`` failure counts
    as one ``Unknown`` poll, never an abort. ``Invalid``/``Rejected`` fetches
    the notary log and hard-fails; so does poll exhaustion (the legacy
    timed-out soft-pass is gone — an unconfirmed notarization is a failure,
    resumable by re-running the stage). With ``staple`` (the mac-app leg's
    ``.dmg``), ``Accepted`` staples the ticket; a staple failure is NON-fatal
    — online Gatekeeper still verifies. The archive leg passes
    ``staple=False``: its submission is a zip around a bare binary, and
    neither is a staple target (the legacy rust-cli contract). The decoded
    ``.p8`` is wiped on any exit.
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

        # Poll count derives from POLL_INTERVAL (never a hard-coded factor):
        # one poll per interval across the whole timeout window, rounded up.
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
    """One signer invocation: the full reopen→resign→reseal→notarize→staple
    sequence over ``req.tree``, staging the signed ``.dmg`` into
    ``req.out_dir`` under the original dmg filename.

    Credentials resolve FIRST — a missing secret hard-fails before any work,
    with zero commands run (the recorded-invocation tests pin exactly that);
    a non-positive ``timeout_minutes`` is refused in the same up-front pass,
    before any signing, so a misconfigured window never wastes the sign +
    reseal only to fail at the notary poll.
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
    identity = _sign_paths(
        sign_order(nested, app), signing, req, entitlements=req.entitlements
    )

    signed_dmg = req.scratch / "signed.dmg"
    _reseal(app, signed_dmg, req)
    # The dmg pass runs through its OWN temporary keychain (unique paths —
    # the legacy exit-48 scar). Entitlements never apply to a disk image.
    _sign_paths([signed_dmg], signing, req)

    submission_id, stapled = _notarize(signed_dmg, notary, req)

    # Stage under the ORIGINAL dmg filename (the consumer's name survives the
    # round-trip); with no incoming .dmg, fall back to `<App>.dmg`.
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
    """One archive-leg invocation (TOL02-WS08 #779): reopen every plain
    ``.tar.gz`` archive bundle under ``req.tree``, codesign the Mach-O
    binaries inside, notarize each, re-emit each tarball into ``req.out_dir``
    under its original filename. The legacy ``rust-cli.yml@v3`` sign +
    notarize steps are the parity contract:

    1. every archive is unpacked (listed and validated first — the reseal
       payload's tar path-traversal refusal, PLUS a symlink/hardlink refusal:
       a raw-CLI tarball ships only files and dirs, and a link would escape
       the extraction dir through its target) and its shipped binaries found
       by CONTENT (:func:`is_macho`, never by name — the docs beside the
       binary are not Mach-O); an archive with none is a hard error, never a
       quiet pass;
    2. every binary across all archives signs in ONE
       :func:`_sign_paths` call — one temporary keychain, hardened runtime +
       secure timestamp, verify --strict per path (the legacy scar: the
       identity lives in a per-call keychain, so all paths must go through a
       single call);
    3. each signed binary is zipped (``notarytool`` needs a container) and
       submitted → polled; NO staple — a bare binary has no staple target,
       Gatekeeper verifies online. Rejection or an unconfirmed verdict is a
       hard fail (ADR-0009), and it lands BEFORE any tarball is re-emitted,
       so a failed run leaves the unsigned tarballs untouched — never a
       half-signed distributable;
    4. each tarball is re-emitted from its unpacked (now signed) tree and
       staged under the original filename — the archive is the distributable
       (loose exec bits do not survive artifact transport; the tar's headers
       do).

    Credentials resolve FIRST — a missing secret hard-fails with zero
    commands run, exactly like the mac-app leg (the legacy notarize step's
    warn-and-skip on a missing ASC key is deliberately gone, and either
    notary trio is accepted). ``req.entitlements`` is REFUSED on this leg:
    entitlements belong to the mac-app leg's ``.app`` root, never a raw CLI
    binary (the legacy rust-cli sign step passed none).
    """
    signing = resolve_signing(req.env)
    notary = resolve_notary(req.env)
    if req.timeout_minutes < 1:
        raise ReleaseError(
            f"notary timeout must be at least 1 minute, got "
            f"{req.timeout_minutes} — a non-positive window would sign and "
            "submit, then never poll for the verdict"
        )
    if req.entitlements is not None:
        raise ReleaseError(
            "entitlements apply to the mac-app leg's .app root only — a raw "
            "CLI binary carries none (legacy rust-cli parity); drop "
            "--entitlements for archive bundles"
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
        # -j junks the path: the zip carries the bare binary name — the
        # legacy `cd <dir> && zip <bin>-notarize.zip <bin>` layout.
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
        # `--` terminates option parsing: a member whose name begins with `-`
        # (it came from an unpacked external bundle) is an operand, never a
        # tar flag — no flag injection through a crafted filename.
        req.run_cmd(
            ["tar", "-czf", str(signed_tar), "-C", str(work), "--", *members],
            SIGN_CMD_TIMEOUT,
        )
        # Stage under the ORIGINAL archive filename (the consumer's name
        # survives the round-trip; with out_dir == tree this replaces the
        # unsigned tarball in place — the laptop-run shape). Copy into a temp
        # path in the DESTINATION dir, then atomically rename over dest: a
        # copy failure leaves the unsigned tarball untouched (ADR-0009), and
        # `os.replace` is atomic only within one filesystem, so the temp must
        # be beside dest, never the scratch-dir signed_tar (a possibly cross-
        # device rename).
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
