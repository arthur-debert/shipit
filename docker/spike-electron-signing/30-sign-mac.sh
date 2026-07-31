#!/usr/bin/env bash
# Spike #1197 step 4 (container): rcodesign scoped, bottom-up signing.
# Order: appex (its own sandbox entitlements) -> outer .app (rcodesign signs
# nested helpers/frameworks itself, bottom-up, per-bundle) with the appex
# excluded so its signature is preserved and merely sealed by the outer sign.
# NEVER --deep (codesign's --deep stamps one entitlement set everywhere;
# rcodesign has no --deep and nests correctly by default).
# Expects: /work/out/pack-darwin/.../LexEd.app, /work/lexed (entitlements),
# /work/secrets/{cert.p12,p12-password.txt} (ro).
set -euo pipefail

APP=/work/out/pack-darwin/LexEd-darwin-arm64/LexEd.app
APPEX="$APP/Contents/PlugIns/LexQuickLook.appex"
APPEX_ENTITLEMENTS=/work/lexed/quicklook/LexQuickLook/LexQuickLook.entitlements
APP_ENTITLEMENTS=/work/lexed/resources/entitlements.mac.plist
P12=/work/secrets/cert.p12
P12_PASS=/work/secrets/p12-password.txt

SIGN=(rcodesign sign --p12-file "$P12" --p12-password-file "$P12_PASS" --for-notarization)

echo "==> sign appex (own entitlements: sandbox + user-selected-read)"
"${SIGN[@]}" --entitlements-xml-file "$APPEX_ENTITLEMENTS" "$APPEX"
rcodesign verify "$APPEX/Contents/MacOS/LexQuickLook"

echo "==> sign outer app (electron entitlements on main + helpers; appex excluded)"
# Scoped values: bare value applies to the main binary; '<relpath>:<value>'
# applies to that nested path. Helpers need the JIT/unsigned-mem entitlements
# (electron-builder's entitlementsInherit equivalent).
HELPER_ARGS=()
while IFS= read -r helper; do
    rel="${helper#"$APP"/}"
    HELPER_ARGS+=(--entitlements-xml-file "${rel}:${APP_ENTITLEMENTS}")
done < <(find "$APP/Contents/Frameworks" -maxdepth 1 -name '*Helper*.app' | sort)

"${SIGN[@]}" \
    --entitlements-xml-file "$APP_ENTITLEMENTS" \
    "${HELPER_ARGS[@]}" \
    --exclude 'Contents/PlugIns/LexQuickLook.appex/**' \
    "$APP"

echo "==> verify"
rcodesign verify "$APP/Contents/MacOS/LexEd"
rcodesign verify "$APPEX/Contents/MacOS/LexQuickLook"
echo "signed OK"
