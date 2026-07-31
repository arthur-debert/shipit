#!/usr/bin/env bash
# Spike #1197 steps 2+3 (container): build the lexed web/electron output,
# package the darwin-arm64 .app with @electron/packager, then do the
# packaging-stage input placement: extraResources + the Darwin-built
# QuickLook appex into Contents/PlugIns/.
# Expects: /work/lexed (rw), /work/appex/LexQuickLook.appex (ro), /work/out (rw).
set -euo pipefail

LEXED=/work/lexed
OUT=/work/out
APPEX=/work/appex/LexQuickLook.appex
PACKAGER_VERSION=20.0.4
LEXD_LSP_VERSION=v0.17.0
TREE_SITTER_VERSION=v0.11.0

cd "$LEXED"

echo "==> npm ci"
npm ci --no-audit --no-fund

echo "==> fetch-deps equivalent (deps.json assets, straight from GitHub releases)"
mkdir -p /work/fetch resources
if [[ ! -f /work/fetch/lexd-lsp-darwin ]]; then
    curl -fsSL -o /work/fetch/lexd-lsp-darwin.tar.gz \
        "https://github.com/lex-fmt/lex/releases/download/${LEXD_LSP_VERSION}/lexd-lsp-aarch64-apple-darwin.tar.gz"
    tar -xzf /work/fetch/lexd-lsp-darwin.tar.gz -C /work/fetch
    mv /work/fetch/lexd-lsp /work/fetch/lexd-lsp-darwin
fi
if [[ ! -f resources/tree-sitter-lex.wasm ]]; then
    curl -fsSL -o /work/fetch/tree-sitter.tar.gz \
        "https://github.com/lex-fmt/tree-sitter-lex/releases/download/${TREE_SITTER_VERSION}/tree-sitter.tar.gz"
    tar -xzf /work/fetch/tree-sitter.tar.gz -C /work/fetch/
    cp /work/fetch/tree-sitter-lex.wasm resources/
    mkdir -p resources/queries && cp -R /work/fetch/queries/. resources/queries/
fi

echo "==> icons (icon-gen, pure JS)"
node app-bin/generate-icons.mjs

echo "==> vite build (renderer + main + preload)"
npx vite build

echo "==> @electron/packager darwin-arm64"
npm install --no-save --no-audit --no-fund "@electron/packager@${PACKAGER_VERSION}"
# packager.mjs resolves @electron/packager relative to its own location, so it
# runs from inside the app checkout (scratch clone; --no-save keeps it clean).
cp /work/scripts/packager.mjs .spike-packager.mjs
node .spike-packager.mjs darwin "$LEXED" "$OUT/pack-darwin"

APP="$OUT/pack-darwin/LexEd-darwin-arm64/LexEd.app"
RES="$APP/Contents/Resources"

echo "==> input placement: extraResources"
install -m 0755 /work/fetch/lexd-lsp-darwin "$RES/lexd-lsp"
mkdir -p "$RES/bin" && install -m 0755 bin/lexed "$RES/bin/lexed"
cp -R welcome "$RES/welcome"
mkdir -p "$RES/dictionaries/licenses"
cp dictionaries/*.trie.gz "$RES/dictionaries/" 2>/dev/null || true
cp -R dictionaries/licenses/. "$RES/dictionaries/licenses/" 2>/dev/null || true
mkdir -p "$RES/assets" && cp comms/assets/logo-full.png "$RES/assets/logo-full.png"

echo "==> input placement: QuickLook appex -> Contents/PlugIns/"
mkdir -p "$APP/Contents/PlugIns"
cp -R "$APPEX" "$APP/Contents/PlugIns/LexQuickLook.appex"

echo "==> packaged app:"
du -sh "$APP"
find "$APP/Contents" -maxdepth 2 | sort
