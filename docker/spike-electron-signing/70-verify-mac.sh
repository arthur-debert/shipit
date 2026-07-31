#!/usr/bin/env bash
# Spike #1197 step 7 (HOST, real mac): the DoD verification.
# Gatekeeper assessment of the dmg, staple validation, install, launch,
# QuickLook appex registration + render. Every check is an assertion: any
# failure exits nonzero.
# Usage: 70-verify-mac.sh <out-dir> <sample.lex|->
#   Pass a sample .lex file for the FULL gate (registration + render).
#   Pass '-' for the explicitly PARTIAL mode (registration only, no render) —
#   e.g. on a locked-screen host where qlmanage cannot present its panel.
set -euo pipefail

OUT="${1:?out dir}"
SAMPLE="${2:?sample.lex for the full gate, or - for PARTIAL (registration-only) mode}"
DMG="$OUT/LexEd.dmg"
APPEX_ID=com.lex.lexed.quicklook

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

echo "==> launch (pgrep is an assertion: pipefail fails the script on no match)"
open -a /Applications/LexEd.app
sleep 8
pgrep -fl "LexEd" | head -3

echo "==> QuickLook appex registration (asserted: ${APPEX_ID} from /Applications/LexEd.app)"
pluginkit -m -v 2>/dev/null | grep "${APPEX_ID}" | grep "/Applications/LexEd.app/Contents/PlugIns/LexQuickLook.appex"
# Informational only: appex-based extensions register via pluginkit, not the
# legacy qlgenerator list.
qlmanage -m plugins 2>/dev/null | grep -i lex \
    || echo "(no legacy qlgenerator entry — expected for an appex-based extension)"

if [[ "$SAMPLE" != - ]]; then
    echo "==> qlmanage -p (render; a NEWLY spawned appex process is the proof)"
    PRE_PIDS="$(pgrep -f "LexQuickLook" || true)"
    qlmanage -p "$SAMPLE" >/tmp/qlmanage-spike.log 2>&1 &
    QL_PID=$!
    sleep 6
    POST_PIDS="$(pgrep -f "LexQuickLook" || true)"
    NEW_PIDS="$(comm -13 <(sort <<<"$PRE_PIDS") <(sort <<<"$POST_PIDS") | grep . || true)"
    kill "$QL_PID" 2>/dev/null || true
    if [[ -z "$NEW_PIDS" ]]; then
        echo "FAIL: no new LexQuickLook process — the render did not use the appex" >&2
        grep -iE 'error|cancel' /tmp/qlmanage-spike.log | head -5 >&2 || true
        exit 1
    fi
    echo "APPEX PROCESS SPAWNED (pid(s): ${NEW_PIDS//$'\n'/ }) — QuickLook extension functioning"
    echo "verification done (FULL)"
else
    echo "verification done (PARTIAL: registration asserted, render NOT exercised —"
    echo "rerun with a sample.lex at an unlocked desktop for the full gate)"
fi
