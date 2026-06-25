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
# `lint` pixi task depends on this, so `pixi run lint` (CI and the pre-commit
# hook alike) provisions lexd identically — the hard gate never silently skips
# the lex leg.
#
# Platform note: the v0.18.4 release ships linux (gnu/musl) + windows binaries,
# but NO macOS asset. On Darwin this falls back to a `lexd` already on PATH
# (e.g. a cargo-built ~/.cargo/bin/lexd) and fails loudly if none is present.

set -euo pipefail

PIN="0.18.4"
TAG="v${PIN}"
REPO="lex-fmt/lex"

# Expected SHA-256 of each release tarball — see sha_for() below. The lex-fmt/lex
# release ships no checksums file, so these are pinned here (trust-on-first-use):
# they detect a tampered or silently re-cut release before the binary is
# installed into the gate env. Re-pin when bumping PIN (download + sha256 them).
sha_for() {
    case "$1" in
        x86_64-unknown-linux-gnu)
            echo "ac7706d9d841e9d90ca8ccef86cb3ad476da033084a00cc7bde3a46663f8c78f"
            ;;
        aarch64-unknown-linux-gnu)
            echo "93d7a540e98e74a583c38479f2803c23d04821e11ca40d56317cef6e73c9b6b6"
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
        # The v0.18.4 release ships NO macOS asset, so the pin cannot be fetched
        # here. Fall back to any lexd already on PATH (e.g. a cargo build), which
        # is enough for local dev — the pin is enforced where it gates: linux CI.
        if existing="$(command -v lexd 2>/dev/null)"; then
            ln -sf "$existing" "$dest"
            got="$("$existing" --version 2>/dev/null || echo '?')"
            case "$got" in
                *"$PIN"*) : ;;
                *) echo "provision-lexd: using PATH lexd (${got#lexd }); pin is ${PIN}, no macOS asset to fetch" >&2 ;;
            esac
            exit 0
        fi
        echo "provision-lexd: no lexd on PATH and no macOS release asset. Install it" >&2
        echo "  (cargo install --git https://github.com/${REPO} lexd) and re-run." >&2
        exit 1
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
