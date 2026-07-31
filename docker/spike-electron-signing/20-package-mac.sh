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
# The fetch cache lives under /work/out (a mounted volume) so downloads
# survive across container runs.
FETCH=/work/out/fetch
mkdir -p "$FETCH" resources
if [[ ! -f "$FETCH/lexd-lsp-darwin" ]]; then
    curl -fsSL -o "$FETCH/lexd-lsp-darwin.tar.gz" \
        "https://github.com/lex-fmt/lex/releases/download/${LEXD_LSP_VERSION}/lexd-lsp-aarch64-apple-darwin.tar.gz"
    tar -xzf "$FETCH/lexd-lsp-darwin.tar.gz" -C "$FETCH"
    mv "$FETCH/lexd-lsp-aarch64-apple-darwin/lexd-lsp" "$FETCH/lexd-lsp-darwin"
fi
if [[ ! -f resources/tree-sitter-lex.wasm ]]; then
    curl -fsSL -o "$FETCH/tree-sitter.tar.gz" \
        "https://github.com/lex-fmt/tree-sitter-lex/releases/download/${TREE_SITTER_VERSION}/tree-sitter.tar.gz"
    mkdir -p "$FETCH/tree-sitter"
    tar -xzf "$FETCH/tree-sitter.tar.gz" -C "$FETCH/tree-sitter"
    cp "$FETCH/tree-sitter/tree-sitter-lex.wasm" resources/
    mkdir -p resources/queries && cp -R "$FETCH/tree-sitter/queries/." resources/queries/
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

echo "==> input placement: extraResources (shared staging keeps deb parity)"
install -m 0755 "$FETCH/lexd-lsp-darwin" "$RES/lexd-lsp"
bash /work/scripts/stage-shared-resources.sh "$LEXED" "$RES"

echo "==> input placement: QuickLook appex -> Contents/PlugIns/"
mkdir -p "$APP/Contents/PlugIns"
cp -R "$APPEX" "$APP/Contents/PlugIns/LexQuickLook.appex"

echo "==> packaged app:"
du -sh "$APP"
find "$APP/Contents" -maxdepth 2 | sort
