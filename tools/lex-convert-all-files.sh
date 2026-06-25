#!/usr/bin/env bash
#
# lex-convert-all-files.sh [--no-preamble]
#
# Finds every .lex file tracked by (or visible to) git from the repository
# root, respecting .gitignore rules, and converts each to Markdown by calling
# lex-convert-doc.sh. The --no-preamble flag, if given, is forwarded to each
# conversion.

set -euo pipefail

usage() {
    echo "Usage: $(basename "$0") [--no-preamble]" >&2
}

no_preamble=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-preamble)
            no_preamble=true
            shift
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            echo "Error: unknown argument '$1'" >&2
            usage
            exit 1
            ;;
    esac
done

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
convert_one="$script_dir/lex-convert-doc.sh"

if [[ ! -x "$convert_one" ]]; then
    echo "Error: cannot find executable $convert_one" >&2
    exit 1
fi

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

forward_args=()
if [[ "$no_preamble" == true ]]; then
    forward_args+=(--no-preamble)
fi

# Tracked files plus untracked-but-not-ignored files, so .gitignore is honored.
count=0
while IFS= read -r -d '' lexfile; do
    "$convert_one" "${forward_args[@]}" "$lexfile"
    count=$((count + 1))
done < <(git ls-files -z --cached --others --exclude-standard -- '*.lex')

echo "Done. Converted $count file(s)."
