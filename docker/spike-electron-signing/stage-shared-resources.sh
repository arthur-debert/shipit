#!/usr/bin/env bash
# Spike #1197: stage the platform-independent extraResources (electron-builder
# parity) from a lexed checkout into a packaged app's resources dir. Both the
# mac and linux packaging steps call this, so the artifacts stay in parity;
# the platform lexd-lsp binary is staged by the caller.
# Usage: stage-shared-resources.sh <lexed-checkout> <resources-dir>
set -euo pipefail

LEXED="${1:?lexed checkout dir}"
RES="${2:?packaged app resources dir}"
cd "$LEXED"

mkdir -p "$RES/bin"
install -m 0755 bin/lexed "$RES/bin/lexed"
cp -R welcome "$RES/welcome"
mkdir -p "$RES/dictionaries/licenses"
cp dictionaries/*.trie.gz "$RES/dictionaries/" 2>/dev/null || true
cp -R dictionaries/licenses/. "$RES/dictionaries/licenses/" 2>/dev/null || true
mkdir -p "$RES/assets"
cp comms/assets/logo-full.png "$RES/assets/logo-full.png"
