#!/usr/bin/env bash
# Regenerate role Markdown mirrors before composing their derived surfaces.

set -euo pipefail

cd "$(dirname "$0")/.."

for lex in src/shipit/data/roles/*.lex; do
    bash tools/lex-convert-doc.sh "$lex"
done

python -m shipit.harness.prompts
