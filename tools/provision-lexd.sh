#!/usr/bin/env bash
#
# provision-lexd.sh — put the pinned `lexd` on PATH inside the pixi env.
#
# lexd is the one gate tool not on conda-forge (it is a rust binary published at
# lex-fmt/lex). The linters proper are conda-forge packages pinned in pixi.lock;
# lexd is pinned HERE and fetched from its GitHub release — the same "download a
# pinned prebuilt binary" pattern Spike 0 used for wasm-bindgen.
#
# Idempotent: a no-op when the pinned lexd is already installed in the env. The
# non-managed `install-hooks` pixi task depends on this (and the CI lint job runs it
# explicitly), so a fresh clone / CI run provisions lexd into the lint env before the
# hooks' / CI's `pixi run -e lint lint` — the hard gate never silently skips the lex leg.
#
# Platform note: the pinned v0.18.2 release ships linux (x86_64 / aarch64 gnu) AND an
# arm64 macOS asset (aarch64-apple-darwin) — so macOS arm64 fetches the PINNED binary
# exactly like linux, with NO reliance on a host `lexd`. There is NO x86_64 (Intel)
# macOS asset at this pin, so Intel-mac falls back LOUDLY to a host `lexd` (e.g. a
# cargo build) and fails loudly if none is present — never a silent skip.

set -euo pipefail

PIN="0.18.2"
TAG="v${PIN}"
REPO="lex-fmt/lex"

# Expected SHA-256 of each release tarball — see sha_for() below. The lex-fmt/lex
# release ships no checksums file, so these are pinned here (trust-on-first-use):
# they detect a tampered or silently re-cut release before the binary is
# installed into the gate env. Re-pin when bumping PIN (download + sha256 them).
sha_for() {
    case "$1" in
        x86_64-unknown-linux-gnu)
            echo "f0465c12b7398debae9d4b8d97a88730b86a4e9cd97e8dcc02ae1949e0a2d833"
            ;;
        aarch64-unknown-linux-gnu)
            echo "36ad2105c5b7e6fbbb5d8cbad2c2ab07fd3e6e27db24acc30bdc48daf65e1771"
            ;;
        aarch64-apple-darwin)
            echo "474073b0ae9f0a877e25d563ecf3e58601bb6cdc0eacfee72573860009ff096e"
            ;;
        *) echo "" ;;
    esac
}

prefix="${CONDA_PREFIX:?provision-lexd: must run inside a pixi/conda env}"
dest="${prefix}/bin/lexd"

have_pinned() {
    [ -x "$1" ] && "$1" --version 2>/dev/null | grep -q "$PIN"
}

# Already provisioned at the pin — nothing to do.
if have_pinned "$dest"; then
    exit 0
fi

# Canonicalize a path (follow symlinks) as portably as we can; print it unchanged when
# no resolver is available. Used to tell an EXTERNAL host lexd apart from our own $dest
# (which is first on PATH), so the Intel-mac fallback never self-symlinks the target.
_resolve() {
    if command -v realpath >/dev/null 2>&1; then
        realpath "$1" 2>/dev/null || printf '%s\n' "$1"
    elif readlink -f "$1" >/dev/null 2>&1; then
        readlink -f "$1"
    else
        printf '%s\n' "$1"
    fi
}

os="$(uname -s)"
arch="$(uname -m)"

case "${os}" in
    Linux)
        case "${arch}" in
            x86_64) triple="x86_64-unknown-linux-gnu" ;;
            aarch64 | arm64) triple="aarch64-unknown-linux-gnu" ;;
            *)
                echo "provision-lexd: unsupported linux arch '${arch}'" >&2
                exit 1
                ;;
        esac
        ;;
    Darwin)
        case "${arch}" in
            arm64 | aarch64)
                # v0.18.2 publishes an arm64 macOS asset, so fetch the PINNED binary
                # exactly like linux (fall through to the download + checksum below).
                triple="aarch64-apple-darwin"
                ;;
            x86_64)
                # No x86_64 (Intel) macOS asset at this pin. Fail LOUD, never a silent
                # skip: fall back to an EXTERNAL host `lexd` if present (noting any
                # version drift), else instruct — the pinned fetch is unavailable here.
                #
                # $CONDA_PREFIX/bin is FIRST on PATH, so `command -v lexd` can resolve to
                # our own install target ($dest); reject that self-match — symlinking
                # $dest to itself (or reusing a stale env-local binary) is not a real
                # host fallback. Compare canonical paths so a symlink-to-$dest is caught.
                existing="$(command -v lexd 2>/dev/null || true)"
                existing_real="$(_resolve "$existing")"
                dest_real="$(_resolve "$dest")"
                if [ -n "$existing" ] && [ "$existing_real" != "$dest_real" ]; then
                    ln -sf "$existing" "$dest"
                    got="$("$existing" --version 2>/dev/null || echo '?')"
                    case "$got" in
                        *"$PIN"*) : ;;
                        *) echo "provision-lexd: using host lexd (${got#lexd }); pin is ${PIN}, no x86_64-apple-darwin asset at this pin" >&2 ;;
                    esac
                    exit 0
                fi
                echo "provision-lexd: no pinned macOS-x86_64 (Intel) asset at ${PIN} and no external host lexd. Install it" >&2
                echo "  (cargo install --git https://github.com/${REPO} lexd) and re-run." >&2
                exit 1
                ;;
            *)
                echo "provision-lexd: unsupported darwin arch '${arch}'" >&2
                exit 1
                ;;
        esac
        ;;
    *)
        echo "provision-lexd: unsupported OS '${os}'" >&2
        exit 1
        ;;
esac

url="https://github.com/${REPO}/releases/download/${TAG}/lexd-${triple}.tar.gz"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "provision-lexd: fetching lexd ${PIN} (${triple})"
curl -fsSL "$url" -o "${tmp}/lexd.tar.gz"

# Verify the tarball against the pinned SHA-256 before extracting/installing.
expected="$(sha_for "$triple")"
if [ -z "$expected" ]; then
    echo "provision-lexd: no pinned SHA-256 for ${triple} — refusing to install" >&2
    exit 1
fi
if command -v sha256sum >/dev/null 2>&1; then
    actual="$(sha256sum "${tmp}/lexd.tar.gz" | awk '{print $1}')"
else
    actual="$(shasum -a 256 "${tmp}/lexd.tar.gz" | awk '{print $1}')"
fi
if [ "$actual" != "$expected" ]; then
    echo "provision-lexd: SHA-256 mismatch for lexd ${PIN} (${triple})" >&2
    echo "  expected ${expected}" >&2
    echo "  actual   ${actual}" >&2
    exit 1
fi

tar -xzf "${tmp}/lexd.tar.gz" -C "$tmp"

# The tarball is `lexd-<triple>/lexd`; find it rather than assume the prefix.
binary="$(find "$tmp" -type f -name lexd | head -n 1)"
if [ -z "$binary" ]; then
    echo "provision-lexd: no lexd binary in ${url}" >&2
    exit 1
fi

install -m 0755 "$binary" "$dest"
echo "provision-lexd: installed lexd ${PIN} -> ${dest}"
