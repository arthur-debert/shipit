#!/usr/bin/env bash
# Spike #1197 step 6 (container): nfpm deb of the linux build + zip of the
# signed mac app (symlink-preserving), from the same source build.
set -euo pipefail

LEXED=/work/lexed
OUT=/work/out
PACKAGER_VERSION=20.0.4
LEXD_LSP_VERSION=v0.17.0
cd "$LEXED"

echo "==> @electron/packager linux-arm64 (same dist/ build as the mac app)"
# Self-sufficient per-step rerun: install the packager and copy the driver in
# (same mechanics as 20-package-mac.sh) instead of relying on step 20 leftovers.
npm install --no-save --no-audit --no-fund "@electron/packager@${PACKAGER_VERSION}"
cp /work/scripts/packager.mjs .spike-packager.mjs
node .spike-packager.mjs linux "$LEXED" "$OUT/pack-linux"
# Linux runtime resources land next to the binary in resources/ (electron-
# builder parity: extraResources -> resources/).
LINRES="$OUT/pack-linux/lexed-linux-arm64/resources"
curl -fsSL -o /tmp/lexd-lsp-linux.tar.gz \
    "https://github.com/lex-fmt/lex/releases/download/${LEXD_LSP_VERSION}/lexd-lsp-aarch64-unknown-linux-gnu.tar.gz"
tar -xzf /tmp/lexd-lsp-linux.tar.gz -C /tmp
install -m 0755 /tmp/lexd-lsp-aarch64-unknown-linux-gnu/lexd-lsp "$LINRES/lexd-lsp"
bash /work/scripts/stage-shared-resources.sh "$LEXED" "$LINRES"

echo "==> nfpm deb"
LEXED_VERSION="$(node -p "require('$LEXED/package.json').version")" \
    nfpm package -f /work/scripts/nfpm.yaml -p deb -t "$OUT/"

echo "==> assert required runtime inputs made it into the deb"
dpkg-deb -c "$OUT"/lexed_*.deb >/tmp/deb-contents.txt
for f in resources/lexd-lsp resources/bin/lexed resources/welcome \
    resources/assets/logo-full.png; do
    grep -q "opt/lexed/$f" /tmp/deb-contents.txt \
        || {
            echo "FAIL: $f missing from deb" >&2
            exit 1
        }
done

echo "==> zip of the signed mac app (-y preserves symlinks)"
cd "$OUT/pack-darwin/LexEd-darwin-arm64"
rm -f "$OUT/LexEd-mac-arm64.zip"
zip -qry "$OUT/LexEd-mac-arm64.zip" LexEd.app

ls -lh "$OUT"/*.deb "$OUT"/*.zip
