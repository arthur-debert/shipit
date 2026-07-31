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
# This step packages step 20's BUILD on purpose — same-build parity across the
# mac and linux artifacts is the point — so it depends on step 20's build
# outputs (but not on its tool leftovers; packager + driver are set up here).
if [[ ! -d dist || ! -d dist-electron ]]; then
    echo "FAIL: dist/dist-electron missing — run 20-package-mac.sh first" \
        "(this step packages the SAME build)" >&2
    exit 1
fi
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
LEXED_VERSION="$(node -p "require('$LEXED/package.json').version")"
export LEXED_VERSION
nfpm package -f /work/scripts/nfpm.yaml -p deb -t "$OUT/"
DEB="$OUT/lexed_${LEXED_VERSION}_arm64.deb"

echo "==> assert required runtime inputs made it into the deb"
dpkg-deb -c "$DEB" >/tmp/deb-contents.txt
for f in resources/lexd-lsp resources/bin/lexed resources/welcome \
    resources/assets/logo-full.png; do
    grep -q "opt/lexed/$f" /tmp/deb-contents.txt \
        || {
            echo "FAIL: $f missing from deb" >&2
            exit 1
        }
done
# Dictionaries are conditional on the checkout (see stage-shared-resources.sh);
# when the checkout has them, the deb must too.
if compgen -G "$LEXED/dictionaries/*.trie.gz" >/dev/null; then
    grep -q 'opt/lexed/resources/dictionaries/.*\.trie\.gz' /tmp/deb-contents.txt \
        || {
            echo "FAIL: dictionaries missing from deb" >&2
            exit 1
        }
fi

echo "==> zip of the signed mac app (-y preserves symlinks)"
cd "$OUT/pack-darwin/LexEd-darwin-arm64"
rm -f "$OUT/LexEd-mac-arm64.zip"
zip -qry "$OUT/LexEd-mac-arm64.zip" LexEd.app

ls -lh "$OUT"/*.deb "$OUT"/*.zip
