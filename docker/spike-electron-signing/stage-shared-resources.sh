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
# Dictionaries ship when the checkout carries them; when present, staging is
# required (no silent skip — deb/app parity is asserted downstream).
if compgen -G "dictionaries/*.trie.gz" >/dev/null; then
    mkdir -p "$RES/dictionaries/licenses"
    cp dictionaries/*.trie.gz "$RES/dictionaries/"
    cp -R dictionaries/licenses/. "$RES/dictionaries/licenses/"
else
    echo "(no dictionaries/*.trie.gz in the checkout — dictionary staging skipped)"
fi
mkdir -p "$RES/assets"
cp comms/assets/logo-full.png "$RES/assets/logo-full.png"
