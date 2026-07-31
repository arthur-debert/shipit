#!/usr/bin/env bash
# Spike #1197: run one container-side step script in the spike image.
# Usage: run-container.sh <lexed-dir> <appex-dir> <out-dir> <secrets-dir|-> <script-name>
# The secrets dir (mounted ro, '-' to skip) must contain:
#   cert.p12 p12-password.txt asc-key.p8 asc-key-id.txt asc-issuer-id.txt
# (local signing credentials, coordinator-provided; values never leave the host)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LEXED="${1:?lexed dir}"
APPEX_DIR="${2:?appex dir}"
OUT="${3:?out dir}"
SECRETS="${4:?secrets dir or -}"
SCRIPT="${5:?script name}"
IMAGE=shipit-spike-electron-signing

if [[ "$SCRIPT" != "$(basename "$SCRIPT")" || ! -f "$HERE/$SCRIPT" ]]; then
    echo "unknown step script: '$SCRIPT' (expected the name of a script in $HERE)" >&2
    exit 1
fi

docker build -q -t "$IMAGE" "$HERE" >/dev/null

MOUNTS=(
    -v "$LEXED:/work/lexed"
    -v "$APPEX_DIR:/work/appex:ro"
    -v "$OUT:/work/out"
    -v "$HERE:/work/scripts:ro"
    -v spike-1197-npm-cache:/root/.npm
    -v spike-1197-electron-cache:/root/.cache
)
[[ "$SECRETS" != - ]] && MOUNTS+=(-v "$SECRETS:/work/secrets:ro")

exec docker run --rm "${MOUNTS[@]}" "$IMAGE" bash "/work/scripts/$SCRIPT"
