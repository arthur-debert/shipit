"""lexd provisioning — the pinned `lexd` into the active pixi env (ADP00-WS03).

lexd is the one gate tool not on conda-forge (a rust binary published at
lex-fmt/lex). The linters proper are conda-forge packages pinned in pixi.lock;
lexd is pinned HERE — in the binary, not in a distributed script — and fetched
from its GitHub release, the same "download a pinned prebuilt binary" pattern
Spike 0 used for wasm-bindgen. This module replaces the repo-local
``tools/provision-lexd.sh``: a consumer's managed ``provision-lexd`` task
invokes ``shipit provision lexd`` and there is no script to reconcile
(docs/prd/adoption.md).

Idempotent: a no-op when the pinned lexd is already installed in the env — the
already-installed probe runs BEFORE platform resolution, so a platform with no
pinned asset (Intel mac) that provisioned lexd another way (the cargo-install
instruction in the refusal) still no-ops instead of failing loud.

Platform note: the pinned v0.18.2 release ships linux (x86_64 / aarch64 gnu)
AND an arm64 macOS asset (aarch64-apple-darwin). There is NO x86_64 (Intel)
macOS asset at this pin, so Intel-mac FAILS LOUD immediately: there is nothing
to provision, so :func:`resolve_triple` raises with an instruction to
provision lexd from the pinned source — never a PATH walk, never a host-lexd
fallback, never a silent skip.

The split (ADR-0028): the decision core — the pin, :func:`resolve_triple`,
:func:`release_url`, :func:`expected_sha`, :func:`is_pinned` — is pure and
unit-tested directly; everything that touches the world goes through the one
Exec runner (:mod:`shipit.execrun`; the version probe and the curl fetch) or
the filesystem boundary of :func:`provision`, which takes an injectable
``runner`` so its orchestration is tested with the boundary faked.
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import re
import tarfile
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .. import execrun

logger = logging.getLogger("shipit.provision")

#: The fleet-wide lexd pin. A pin bump is THIS edit plus re-pinning
#: :data:`SHAS` (download the release tarballs and sha256 them).
PIN = "0.18.2"

#: The GitHub repo lexd releases are published at.
REPO = "lex-fmt/lex"

#: Expected SHA-256 of each release tarball, keyed by target triple. The
#: lex-fmt/lex release ships no checksums file, so these are pinned here
#: (trust-on-first-use): they detect a tampered or silently re-cut release
#: before the binary is installed into the gate env. The key set IS the set of
#: platforms the pin supports — :func:`resolve_triple` refuses anything else.
SHAS: dict[str, str] = {
    "x86_64-unknown-linux-gnu": (
        "f0465c12b7398debae9d4b8d97a88730b86a4e9cd97e8dcc02ae1949e0a2d833"
    ),
    "aarch64-unknown-linux-gnu": (
        "36ad2105c5b7e6fbbb5d8cbad2c2ab07fd3e6e27db24acc30bdc48daf65e1771"
    ),
    "aarch64-apple-darwin": (
        "474073b0ae9f0a877e25d563ecf3e58601bb6cdc0eacfee72573860009ff096e"
    ),
}

#: The version probe's stated timeout, in seconds (ADR-0028: every Exec states
#: its bound deliberately). ``lexd --version`` is instant; anything slower is a
#: wedged binary, and killing it just routes to the reinstall path.
PROBE_TIMEOUT: float = 30.0

#: The release-fetch Exec's stated timeout, in seconds. A GitHub release
#: download over a slow link is a legitimate long-runner, so the runner's
#: generous default IS the right bound — stated on the wire rather than
#: inherited, so the no-implicit-timeout sweep stays grep-verifiable.
FETCH_TIMEOUT: float = execrun.DEFAULT_TIMEOUT

#: :attr:`LexdReport.action` values.
ACTION_NOOP = "noop"  # the pinned lexd was already installed
ACTION_INSTALLED = "installed"  # fetched, verified, and installed at the pin


class ProvisionError(RuntimeError):
    """A provisioning refusal: unsupported platform, bad checksum, malformed
    release, or no active env to install into. Mapped to ``error: …`` + exit 1
    by the CLI error shell (ADR-0030)."""


@dataclass(frozen=True)
class LexdReport:
    """The typed result of one provisioning run (ADR-0030 — rendered at the edge).

    ``triple`` is ``None`` on a no-op: the probe runs before platform
    resolution, so an already-provisioned env never resolves (or refuses on) a
    platform at all.
    """

    pin: str
    action: str
    dest: str
    triple: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "pin": self.pin,
            "action": self.action,
            "dest": self.dest,
            "triple": self.triple,
        }


# --------------------------------------------------------------------------
# The pure decision core
# --------------------------------------------------------------------------


def resolve_triple(system: str, machine: str) -> str:
    """The release target triple for a platform — or the loud refusal.

    ``system``/``machine`` are :func:`platform.system` / :func:`platform.machine`
    spellings (``uname -s`` / ``uname -m``). Only triples with a pinned asset
    AND a pinned SHA-256 resolve; everything else raises :class:`ProvisionError`
    — most deliberately Intel macOS, which has no pinned asset at :data:`PIN`,
    so the refusal carries the one supported alternative (build from the pinned
    source) instead of any host-lexd fallback.
    """
    if system == "Linux":
        if machine == "x86_64":
            return "x86_64-unknown-linux-gnu"
        if machine in ("aarch64", "arm64"):
            return "aarch64-unknown-linux-gnu"
        raise ProvisionError(f"provision lexd: unsupported linux arch '{machine}'")
    if system == "Darwin":
        if machine in ("arm64", "aarch64"):
            return "aarch64-apple-darwin"
        if machine == "x86_64":
            raise ProvisionError(
                f"provision lexd: no pinned macOS-x86_64 (Intel) asset at {PIN} — "
                "lexd is not provisioned on this platform. Provision it from the "
                "pinned source and re-run, e.g.: "
                f"cargo install --git https://github.com/{REPO} --tag v{PIN} lexd"
            )
        raise ProvisionError(f"provision lexd: unsupported darwin arch '{machine}'")
    raise ProvisionError(f"provision lexd: unsupported OS '{system}'")


def release_url(triple: str) -> str:
    """The pinned release tarball's URL for ``triple``."""
    return f"https://github.com/{REPO}/releases/download/v{PIN}/lexd-{triple}.tar.gz"


def expected_sha(triple: str) -> str:
    """The pinned SHA-256 for ``triple``'s tarball — refusing an unpinned triple.

    Structurally unreachable through :func:`resolve_triple` (which only mints
    :data:`SHAS` keys), but the checksum gate must never soften to "no pin, no
    check": a triple without a pinned hash refuses to install.
    """
    sha = SHAS.get(triple)
    if sha is None:
        raise ProvisionError(
            f"provision lexd: no pinned SHA-256 for {triple} — refusing to install"
        )
    return sha


#: Matches a ``lexd --version`` line whose version token is EXACTLY the pin —
#: ``lexd 0.18.2`` / ``lexd 0.18.2 (release)`` pass; a bare-substring near-miss
#: (``lexd 0.18.25``, ``lexd 10.18.25``) does NOT. The trailing ``\b`` pins the
#: token's right edge so a longer version that merely starts with the pin can't
#: read as pinned; ``re.escape`` keeps the dotted pin from acting as a regex. The
#: right edge is ``(?!\S)`` (whitespace or end-of-line) rather than ``\b``: a
#: word boundary sits between ``2`` and a following ``-``/``+``, so ``\b`` would
#: accept a pre-release/build-metadata build (``0.18.2-dev``, ``0.18.2+meta``) as
#: the pinned release; requiring non-word-and-non-``-``/``+`` — i.e. whitespace or
#: end — keeps the match to the exact release token.
_PINNED_RE = re.compile(rf"\blexd {re.escape(PIN)}(?!\S)")


def is_pinned(version_output: str | None) -> bool:
    """Whether a ``lexd --version`` output shows the pinned version.

    ``None`` means the probe could not run at all (no binary); any output not
    carrying the pin (an older lexd, garbage from a broken binary) routes to
    reinstall. The match is the ``lexd <version>`` token, not a bare substring:
    the retired script's ``grep -q "$PIN"`` would false-positive on a longer
    version string that embeds the pin (``0.18.25``, ``10.18.25``), so the
    binary tightens the idempotence test to the exact ``lexd <PIN>`` token.
    """
    return version_output is not None and _PINNED_RE.search(version_output) is not None


# --------------------------------------------------------------------------
# The boundary
# --------------------------------------------------------------------------


def provision(
    prefix: str | os.PathLike | None = None,
    *,
    system: str | None = None,
    machine: str | None = None,
    runner=execrun.run,
) -> LexdReport:
    """Put the pinned lexd at ``<prefix>/bin/lexd``, idempotently.

    ``prefix`` defaults to the active env's ``CONDA_PREFIX`` — the verb runs
    under ``pixi run``, so the env that invokes the task is the env that
    receives the binary (exactly the retired script's contract). No active env
    and no explicit prefix is a refusal: provisioning either targets a real
    env or it does not run.

    The world-touching steps — the version probe and the curl fetch — go
    through ``runner`` (the one Exec seam, ADR-0028; injectable for tests).
    The tarball is checksum-verified against :data:`SHAS` before anything is
    extracted or installed, and the binary lands via write-then-rename so a
    failed install can never leave a half-written lexd on the gate path.
    """
    if prefix is None:
        prefix = os.environ.get("CONDA_PREFIX")
    if not prefix:
        raise ProvisionError(
            "provision lexd: no CONDA_PREFIX — must run inside a pixi/conda env "
            "(e.g. via `pixi run`), or pass an explicit prefix"
        )
    started = time.monotonic()
    dest = Path(prefix) / "bin" / "lexd"

    if is_pinned(_probe_version(dest, runner)):
        logger.debug(
            "lexd already provisioned at the pin",
            extra={"pin": PIN, "dest": str(dest)},
        )
        return LexdReport(pin=PIN, action=ACTION_NOOP, dest=str(dest))

    triple = resolve_triple(
        system if system is not None else platform.system(),
        machine if machine is not None else platform.machine(),
    )
    url = release_url(triple)
    with tempfile.TemporaryDirectory(prefix="shipit-provision-lexd-") as tmp:
        tarball = Path(tmp) / "lexd.tar.gz"
        # The one external fetch, through the one Exec seam. curl's argv is
        # assembled ONLY here (its adapter home — tests/test_tool_argv_sweep.py).
        runner(
            ["curl", "-fsSL", url, "-o", str(tarball)],
            timeout=FETCH_TIMEOUT,
        )
        actual = hashlib.sha256(tarball.read_bytes()).hexdigest()
        expected = expected_sha(triple)
        if actual != expected:
            raise ProvisionError(
                f"provision lexd: SHA-256 mismatch for lexd {PIN} ({triple}) — "
                f"expected {expected}, got {actual}"
            )
        _install(_extract_binary(tarball, url), dest)
    logger.info(
        "lexd provisioned",
        extra={
            "pin": PIN,
            "triple": triple,
            "dest": str(dest),
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
    )
    return LexdReport(pin=PIN, action=ACTION_INSTALLED, dest=str(dest), triple=triple)


def _probe_version(dest: Path, runner: Callable[..., execrun.ExecResult]) -> str | None:
    """``lexd --version`` output from the installed binary, or ``None``.

    An absent ``dest`` answers without an Exec at all — "not yet installed" is
    the probe's NORMAL fresh-env answer, so it must not manufacture the seam's
    ERROR-level missing-binary record. When a binary IS present:
    ``check=False`` — a nonzero exit is a broken binary, an answer (reinstall),
    not a transport failure — and a launch failure (not executable, any
    OS-level error) is the same answer, so the transport error is absorbed
    here rather than surfaced: the probe's only job is the idempotence test.
    """
    if not dest.exists():
        return None
    try:
        result = runner([str(dest), "--version"], check=False, timeout=PROBE_TIMEOUT)
    except execrun.ExecError:
        return None
    return result.stdout if result.rc == 0 else None


def _extract_binary(tarball: Path, url: str) -> bytes:
    """The ``lexd`` member's bytes out of the release tarball.

    The tarball is ``lexd-<triple>/lexd``; matched by basename rather than an
    assumed prefix (the retired script's ``find``-not-assume rule). Members are
    read in place — never extracted to disk — so tar path traversal has no
    surface. A tarball with no lexd member, or one unreadable as a tar.gz at
    all, is a malformed release: refuse.
    """
    try:
        with tarfile.open(tarball, mode="r:gz") as tar:
            for member in tar:
                if member.isfile() and member.name.rsplit("/", 1)[-1] == "lexd":
                    extracted = tar.extractfile(member)
                    if extracted is not None:
                        return extracted.read()
    except tarfile.TarError as exc:
        raise ProvisionError(
            f"provision lexd: unreadable release tarball from {url}: {exc}"
        ) from exc
    raise ProvisionError(f"provision lexd: no lexd binary in {url}")


def _install(binary: bytes, dest: Path) -> None:
    """Write ``binary`` to ``dest`` (mode 0755) via write-then-rename.

    The rename is atomic within ``dest``'s directory, so a crash mid-install
    leaves either the old lexd or the new one on the gate path — never a
    truncated binary a later ``lint`` would execute. The staging file gets a
    UNIQUE name (``mkstemp``) rather than a fixed ``.lexd.provision-tmp``: two
    provisioners racing in the same env (parallel CI jobs, simultaneous git
    hooks) would otherwise truncate and write the same temp file, and one
    could rename a half-written binary into ``dest``.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(dest.parent), prefix=f".{dest.name}.", suffix=".provision-tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(binary)
        tmp.chmod(0o755)
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
