#!/usr/bin/env bash
#
# regen-roles.sh — re-flow every role-prompt surface from the lex fragments.
#
# ONE edit to a fragment under src/shipit/data/roles/ re-flows everything
# (ADR-0011). Run after editing any *.lex there. Two steps, in order:
#
#   1. Mirror each role fragment .lex -> .md (the rendered prose the generator
#      reads), via the repo's pinned lexd + prettier.
#   2. Compose the mirrors into the derived surfaces (the subagent agent-defs,
#      the bundled coordinator slice read by policy.py, the AGENTS.md union
#      reference) with the Python generator.
#
# Invoked by `pixi run regen-roles` (lexd + prettier + shipit are on PATH in the
# lint env). Idempotent: a no-op when nothing changed.

set -euo pipefail

cd "$(dirname "$0")/.."

for lex in src/shipit/data/roles/*.lex; do
    bash tools/lex-convert-doc.sh "$lex"
done

python -m shipit.harness.prompts
