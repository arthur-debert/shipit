#!/usr/bin/env bash
# Spike #1197 step 5a (container): assemble a UDIF dmg from the SIGNED app,
# entirely on Linux, then sign the dmg. Sign-then-assemble is mandatory:
# rcodesign cannot sign inside a dmg (verified constraint).
# Pipeline (bitcoin-core's): xorrisofs (ISO9660+HFS+ hybrid, Rock Ridge
# symlinks preserved) -> libdmg-hfsplus `dmg` (wraps into UDIF/UDZO with a
# koly trailer rcodesign can sign and the notary service accepts).
set -euo pipefail

APP=/work/out/pack-darwin/LexEd-darwin-arm64/LexEd.app
OUT=/work/out
P12=/work/secrets/cert.p12
P12_PASS=/work/secrets/p12-password.txt
VOLNAME=LexEd

STAGE="$OUT/dmg-root"
rm -rf "$STAGE" "$OUT/uncompressed.dmg" "$OUT/LexEd.dmg"
mkdir -p "$STAGE"
cp -a "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

echo "==> xorrisofs (hybrid HFS+ image)"
xorrisofs -D -l -V "$VOLNAME" -no-pad -r -dir-mode 0755 \
    -sysid APPLE -apm-block-size 2048 -hfsplus \
    -o "$OUT/uncompressed.dmg" "$STAGE"

echo "==> dmg (UDIF/UDZO wrap)"
dmg dmg "$OUT/uncompressed.dmg" "$OUT/LexEd.dmg"
rm -f "$OUT/uncompressed.dmg"

echo "==> sign dmg"
rcodesign sign --p12-file "$P12" --p12-password-file "$P12_PASS" "$OUT/LexEd.dmg"
rcodesign verify "$OUT/LexEd.dmg" || true
ls -lh "$OUT/LexEd.dmg"
