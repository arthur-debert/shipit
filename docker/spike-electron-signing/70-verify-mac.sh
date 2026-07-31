#!/usr/bin/env bash
# Spike #1197 step 7 (HOST, real mac): the DoD verification.
# Gatekeeper assessment of the dmg, staple validation, install, launch,
# QuickLook appex registration + render.
# Usage: 70-verify-mac.sh <out-dir> [sample.lex]
set -euo pipefail

OUT="${1:?out dir}"
SAMPLE="${2:-}"
DMG="$OUT/LexEd.dmg"

echo "==> spctl --assess -vv --type install (dmg)"
spctl --assess -vv --type install "$DMG"

echo "==> stapler validate (dmg)"
xcrun stapler validate "$DMG"

echo "==> mount + install to /Applications"
MOUNT="$(hdiutil attach "$DMG" -nobrowse | awk -F'\t' '/\/Volumes\//{print $NF}' | tail -1)"
trap 'hdiutil detach "$MOUNT" >/dev/null || true' EXIT
rm -rf /Applications/LexEd.app
cp -R "$MOUNT/LexEd.app" /Applications/

echo "==> Gatekeeper on the installed app"
spctl --assess -vv --type execute /Applications/LexEd.app
codesign --verify --deep --strict --verbose=2 /Applications/LexEd.app

echo "==> launch"
open -a /Applications/LexEd.app
sleep 8
pgrep -fl "LexEd" | head -3

echo "==> QuickLook appex registration"
qlmanage -m plugins 2>/dev/null | grep -i lex || true
pluginkit -m -v 2>/dev/null | grep -i "com.lex.lexed.quicklook" || true

if [[ -n "$SAMPLE" ]]; then
    echo "==> qlmanage -p (render preview; appex process spawning is the proof)"
    qlmanage -p "$SAMPLE" >/tmp/qlmanage-spike.log 2>&1 &
    QL_PID=$!
    sleep 6
    if pgrep -fl "LexQuickLook" | head -2; then
        echo "APPEX PROCESS SPAWNED — QuickLook extension functioning"
    else
        echo "APPEX PROCESS NOT FOUND — QuickLook render did not use the appex"
    fi
    kill "$QL_PID" 2>/dev/null || true
    grep -iE 'error|cancel' /tmp/qlmanage-spike.log | head -3 || true
fi
echo "verification done"
