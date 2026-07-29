#!/usr/bin/env bash
# Reconcile pixi and uv to verified pins, then best-effort solve repo environments.
# Fail open because this runs from a session hook.
# Do not edit — `shipit install` overwrites this file.
set -euo pipefail

# Must match the workflow's pixi-version.
PIXI_PIN="0.71.0"
UV_PIN="0.11.28"

BIN_DIR="${HOME}/.local/bin"

warn() {
	echo "setup-dev-env: $*" >&2
}

resolve_triple() {
	case "$(uname -s)/$(uname -m)" in
	Linux/x86_64) echo "x86_64-unknown-linux-musl" ;;
	Linux/aarch64) echo "aarch64-unknown-linux-musl" ;;
	Darwin/arm64) echo "aarch64-apple-darwin" ;;
	*) echo "" ;;
	esac
}

sha256_of() {
	# Return empty on unavailable or failed hashing so verification fails open.
	if command -v sha256sum >/dev/null 2>&1; then
		sha256sum "$1" 2>/dev/null | awk '{print $1}' || echo ""
	elif command -v shasum >/dev/null 2>&1; then
		shasum -a 256 "$1" 2>/dev/null | awk '{print $1}' || echo ""
	else
		echo ""
	fi
}

probe_version() {
	"$1" --version 2>/dev/null | head -n 1 | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1 || true
}

place_binary() {
	# Same-directory rename prevents concurrent readers from seeing a partial binary.
	local staged
	staged="${BIN_DIR}/.${2}.setup-dev-env.$$"
	cp "$1" "$staged" && chmod +x "$staged" && mv -f "$staged" "${BIN_DIR}/${2}"
}

fetch_verified() {
	local got
	if ! command -v curl >/dev/null 2>&1; then
		warn "curl is not available — cannot fetch $1"
		return 1
	fi
	if ! curl -fsSL --retry 2 -o "$3" "$1"; then
		warn "could not fetch $1"
		return 1
	fi
	got="$(sha256_of "$3")"
	if [ -z "$got" ]; then
		warn "could not hash $3 (no sha256sum/shasum, or the tool errored) — refusing the unverified $1"
		return 1
	fi
	if [ "$got" != "$2" ]; then
		warn "sha256 mismatch for $1 (got ${got}, want $2) — refusing to install"
		return 1
	fi
}

provision_pixi() {
	local url sum tmp
	url="https://github.com/prefix-dev/pixi/releases/download/v${PIXI_PIN}/pixi-${TRIPLE}.tar.gz"
	case "$TRIPLE" in
	x86_64-unknown-linux-musl) sum="2f30a2434b3786c860d11494f4dc6c1f3437fb47366d948e398409cae84e0a6c" ;;
	aarch64-unknown-linux-musl) sum="568696c74bd734becf8c7bb84b7d5ea9beda58031f66a6288a8dbc47131dfbf9" ;;
	aarch64-apple-darwin) sum="b3c7e0470a89f63db5b962a72141813e643752825ee8fd950f169ddb4a3d2a44" ;;
	*) return 1 ;;
	esac
	tmp="$(mktemp -d)" || return 1
	if ! fetch_verified "$url" "$sum" "${tmp}/pixi.tar.gz" ||
		! tar -xzf "${tmp}/pixi.tar.gz" -C "$tmp" pixi ||
		! place_binary "${tmp}/pixi" pixi; then
		rm -rf "$tmp"
		return 1
	fi
	rm -rf "$tmp"
}

provision_uv() {
	local url sum tmp
	url="https://github.com/astral-sh/uv/releases/download/${UV_PIN}/uv-${TRIPLE}.tar.gz"
	case "$TRIPLE" in
	x86_64-unknown-linux-musl) sum="f02146b371c35c287d860f003ece7345c86e358a3fd70a9b63700cd141ee7fb4" ;;
	aarch64-unknown-linux-musl) sum="da10cdfa7d92212b7acb62021a0fd61bcf8580c58c3632ec915d10c3a1a7906b" ;;
	aarch64-apple-darwin) sum="33540eb7c883ab857eff79bd5ac2aa31fe27b595abecb4a9c003a2c998447232" ;;
	*) return 1 ;;
	esac
	tmp="$(mktemp -d)" || return 1
	if ! fetch_verified "$url" "$sum" "${tmp}/uv.tar.gz" ||
		! tar -xzf "${tmp}/uv.tar.gz" -C "$tmp" ||
		! place_binary "${tmp}/uv-${TRIPLE}/uv" uv ||
		! place_binary "${tmp}/uv-${TRIPLE}/uvx" uvx; then
		rm -rf "$tmp"
		return 1
	fi
	rm -rf "$tmp"
}

provision_tool() {
	# Direct dispatch keeps shellcheck's reachability analysis sound.
	case "$1" in
	pixi) provision_pixi ;;
	uv) provision_uv ;;
	*) return 1 ;;
	esac
}

reconcile_tool() {
	local have
	have="$(probe_version "$1")"
	if [ "$have" = "$2" ]; then
		return 0
	fi
	if [ -z "$TRIPLE" ]; then
		warn "unsupported platform $(uname -s)/$(uname -m) — cannot provision $1 $2 (found: ${have:-none})"
		return 0
	fi
	warn "reconciling $1 to $2 (found: ${have:-none})"
	if ! provision_tool "$1"; then
		warn "$1 $2 was NOT provisioned — later steps that need it will degrade"
		return 0
	fi
	have="$(probe_version "$1")"
	if [ "$have" != "$2" ]; then
		warn "installed $1 $2 into ${BIN_DIR}, but '$1 --version' resolves ${have:-nothing} — a PATH entry is shadowing the pinned binary (check 'command -v $1')"
	fi
}

manifest_defines_lint_env() {
	# Restrict the lookup to [environments]; [tasks] may also define `lint`.
	awk '
		{ gsub(/\r/, "") }
		/^\[/ { in_envs = ($0 == "[environments]") ? 1 : 0; next }
		in_envs && $0 ~ /^[[:space:]]*lint[[:space:]]*=/ { found = 1; exit }
		END { exit !found }
	' "${REPO_ROOT}/pixi.toml"
}

resolve_script_dir() {
	# Resolve relative links physically, but preserve the caller's logical checkout.
	local path="$1" link dir dirpath hops=0
	while [ -L "$path" ]; do
		hops=$((hops + 1))
		if [ "$hops" -gt 40 ]; then
			warn "symlink loop resolving $1 — using it as-is"
			break
		fi
		if ! dir="$(cd -P -- "$(dirname -- "$path")" && pwd)"; then
			warn "could not resolve the directory holding $path — using it as-is"
			break
		fi
		if ! link="$(readlink -- "$path")"; then
			warn "could not read the symlink $path — using it as-is"
			break
		fi
		case "$link" in
		/*) path="$link" ;;
		*) path="${dir}/${link}" ;;
		esac
	done
	# Never let a failed `cd` collapse the repo root to ".".
	dirpath="$(dirname -- "$path")" || dirpath="$path"
	if ! dir="$(cd -- "$dirpath" && pwd)"; then
		warn "could not resolve the directory holding $path — using it as-is"
		printf '%s\n' "$dirpath"
		return 0
	fi
	printf '%s\n' "$dir"
}

TRIPLE="$(resolve_triple)"
SELF="${BASH_SOURCE[0]:-$0}"
REPO_ROOT="$(dirname -- "$(resolve_script_dir "$SELF")")"

if ! mkdir -p "$BIN_DIR"; then
	warn "could not create ${BIN_DIR} — nothing can be provisioned"
	exit 0
fi

PATH="${BIN_DIR}:${PATH}"
export PATH

# Persist the pinned PATH for later commands in this Claude session.
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
	if ! grep -Fqs "setup-dev-env: pinned base-system PATH" "$CLAUDE_ENV_FILE"; then
		if ! printf '%s\n' "case \":\$PATH:\" in *\":\$HOME/.local/bin:\"*) ;; *) export PATH=\"\$HOME/.local/bin:\$PATH\" ;; esac # setup-dev-env: pinned base-system PATH" >>"$CLAUDE_ENV_FILE"; then
			warn "could not append the PATH line to CLAUDE_ENV_FILE (${CLAUDE_ENV_FILE})"
		fi
	fi
fi

reconcile_tool pixi "$PIXI_PIN"
reconcile_tool uv "$UV_PIN"

# Provisioning may solve against pixi.lock but never mutate it.
if [ -f "${REPO_ROOT}/pixi.toml" ]; then
	if ! command -v pixi >/dev/null 2>&1; then
		warn "pixi is unavailable — skipping the environment solve"
	else
		if ! (cd "$REPO_ROOT" && pixi install --locked); then
			warn "pixi install --locked failed (default env) — the next pixi run will surface the error"
		fi
		if manifest_defines_lint_env; then
			if ! (cd "$REPO_ROOT" && pixi install --locked --environment lint); then
				warn "pixi install --locked -e lint failed — the next lint run will surface the error"
			fi
		fi
	fi
fi

exit 0
