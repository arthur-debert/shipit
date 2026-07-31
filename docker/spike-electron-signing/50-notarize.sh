#!/usr/bin/env bash
# Spike #1197 step 5b (container): notarize the signed dmg from Linux via the
# App Store Connect API (rcodesign speaks the notary API directly — no Xcode,
# no macOS), then staple the ticket into the dmg.
# Expects /work/secrets/{asc-key.p8,asc-key-id.txt,asc-issuer-id.txt} (ro).
set -euo pipefail

DMG=/work/out/LexEd.dmg
KEY_JSON=/tmp/asc-key.json
trap 'rm -f "$KEY_JSON"' EXIT

rcodesign encode-app-store-connect-api-key -o "$KEY_JSON" \
    "$(cat /work/secrets/asc-issuer-id.txt)" \
    "$(cat /work/secrets/asc-key-id.txt)" \
    /work/secrets/asc-key.p8

echo "==> notary-submit (waits for the verdict) + staple"
rcodesign notary-submit --api-key-file "$KEY_JSON" --staple "$DMG"

echo "==> notarization + staple done"
ls -lh "$DMG"
