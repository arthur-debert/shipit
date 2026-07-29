#!/usr/bin/env bash
# Convert every tracked or unignored Lex file to Markdown.

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

count=0
while IFS= read -r -d '' lexfile; do
    "$convert_one" "${forward_args[@]}" "$lexfile"
    count=$((count + 1))
done < <(git ls-files -z --cached --others --exclude-standard -- '*.lex')

echo "Done. Converted $count file(s)."
