#!/usr/bin/env bash
# Spike #1197 step 1 (HOST, Darwin leg): build the QuickLook appex UNSIGNED
# with xcodebuild. Signing happens later, from Linux, with rcodesign — this
# leg stays macOS regardless of the spike outcome (xcodebuild has no Linux
# story), but it produces a build-only artifact: no signing identity needed.
# Usage: 10-build-appex.sh <lexed-checkout> <output-dir>
set -euo pipefail

LEXED="${1:?lexed checkout dir}"
OUT="${2:?output dir}"
DD="$OUT/derived-data"

mkdir -p "$OUT"
xcodebuild -project "$LEXED/quicklook/LexQuickLook.xcodeproj" \
    -scheme LexQuickLook -configuration Release \
    -derivedDataPath "$DD" \
    build CODE_SIGNING_ALLOWED=NO CODE_SIGNING_REQUIRED=NO

rm -rf "$OUT/LexQuickLook.appex"
cp -R "$DD/Build/Products/Release/LexQuickLook.appex" "$OUT/"
echo "appex at $OUT/LexQuickLook.appex"
