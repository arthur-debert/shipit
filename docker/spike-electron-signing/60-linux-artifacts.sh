#!/usr/bin/env bash
# Spike #1197 step 6 (container): nfpm deb of the linux build + zip of the
# signed mac app (symlink-preserving), from the same source build.
set -euo pipefail

LEXED=/work/lexed
OUT=/work/out
cd "$LEXED"

echo "==> @electron/packager linux-arm64 (same dist/ build as the mac app)"
node .spike-packager.mjs linux "$LEXED" "$OUT/pack-linux"
# Linux runtime resources land next to the binary in resources/ (electron-
# builder parity: extraResources -> resources/).
LINRES="$OUT/pack-linux/lexed-linux-arm64/resources"
curl -fsSL -o /tmp/lexd-lsp-linux.tar.gz \
    "https://github.com/lex-fmt/lex/releases/download/v0.17.0/lexd-lsp-aarch64-unknown-linux-gnu.tar.gz"
tar -xzf /tmp/lexd-lsp-linux.tar.gz -C /tmp
install -m 0755 /tmp/lexd-lsp-aarch64-unknown-linux-gnu/lexd-lsp "$LINRES/lexd-lsp"
cp -R welcome "$LINRES/welcome"

echo "==> nfpm deb"
LEXED_VERSION="$(node -p "require('$LEXED/package.json').version")" \
    nfpm package -f /work/scripts/nfpm.yaml -p deb -t "$OUT/"

echo "==> zip of the signed mac app (-y preserves symlinks)"
cd "$OUT/pack-darwin/LexEd-darwin-arm64"
rm -f "$OUT/LexEd-mac-arm64.zip"
zip -qry "$OUT/LexEd-mac-arm64.zip" LexEd.app

ls -lh "$OUT"/*.deb "$OUT"/*.zip
dpkg-deb -c "$OUT"/lexed_*.deb | sed -n '1,5p'
