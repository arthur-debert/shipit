#!/usr/bin/env bash
#
# verify-self-provision.sh — end-to-end proof that `bin/setup-dev-env.sh`
# provisions the whole base system FROM ZERO (#547, docs/dev/containers.md).
#
# Host-side driver: builds the stock ubuntu:24.04 image (docker/
# ubuntu.Dockerfile — no pixi/uv/node/cargo), ships a CLEAN clone of the
# repo's HEAD into it (never the live working tree, so an uncommitted edit can
# neither help nor hurt the verdict), runs the bootstrap inside as the
# non-root user, and asserts:
#
#   1. `pixi --version` and `uv --version` resolve exactly the script's pins;
#   2. a second run is a fast no-op (no "reconciling" line — idempotence);
#   3. `pixi install --locked` succeeds against the shipped pixi.lock;
#   4. `pixi run -e lint lint` goes green end-to-end (lexd provisioned the
#      same way CI does it).
#
# Unlike the bootstrap itself (fail-open — it runs from a session hook), this
# harness FAILS HARD: any missed assertion exits non-zero. Opt-in only — it
# downloads on the order of a gigabyte of toolchains — and deliberately not
# wired into CI (docs/dev/containers.md sketches a nightly/dispatch adoption).
# Host-arch native: works on linux/amd64 CI runners and linux/arm64 Docker
# Desktop alike; no qemu multi-arch matrix.
#
# Usage: bash docker/verify-self-provision.sh

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
image="shipit-self-provision:ubuntu-24.04"

fail() {
    echo "verify-self-provision: FAIL: $*" >&2
    exit 1
}

command -v docker >/dev/null 2>&1 || fail "docker is not on PATH"

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

# A clean clone of HEAD — .git included, since the lint gate reads its scope
# from `git ls-files` (a `git archive` tree would have no index to read).
echo "verify-self-provision: cloning HEAD into a clean copy" >&2
git clone --quiet --depth 1 "file://$repo_root" "$workdir/repo"

echo "verify-self-provision: building $image" >&2
docker build -t "$image" -f "$repo_root/docker/ubuntu.Dockerfile" "$repo_root/docker"

# The whole in-container run is one script, passed as an ARGUMENT (stdin
# carries the tar stream): tar the clean clone in over stdin — no bind mount,
# so container-local files dodge the host-uid ownership mismatch a mount hits
# on CI runners — then bootstrap and assert. The container has no pixi and no
# uv, so everything below `setup-dev-env.sh` is what the script itself
# provisioned.
in_container="$(
    cat <<'IN_CONTAINER'
set -euo pipefail

fail() {
    echo "verify-self-provision(container): FAIL: $*" >&2
    exit 1
}

tar -xf - -C "$HOME"
cd "$HOME/repo"

# The baseline really is zero.
command -v pixi >/dev/null 2>&1 && fail "image already has pixi — not a from-zero baseline"
command -v uv >/dev/null 2>&1 && fail "image already has uv — not a from-zero baseline"

# The pins under test come from the script itself — the harness must never
# carry a second copy that could drift.
pixi_pin="$(sed -n 's/^PIXI_PIN="\(.*\)"$/\1/p' bin/setup-dev-env.sh)"
uv_pin="$(sed -n 's/^UV_PIN="\(.*\)"$/\1/p' bin/setup-dev-env.sh)"
[ -n "$pixi_pin" ] && [ -n "$uv_pin" ] || fail "could not read the pins out of bin/setup-dev-env.sh"

# Run 1: from zero. The bootstrap is fail-open by design, so the harness
# asserts the OUTCOMES, not the exit code.
bash bin/setup-dev-env.sh

export PATH="$HOME/.local/bin:$PATH"
got_pixi="$(pixi --version 2>/dev/null | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1 || true)"
got_uv="$(uv --version 2>/dev/null | grep -Eo '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1 || true)"
[ "$got_pixi" = "$pixi_pin" ] || fail "pixi resolves '${got_pixi:-nothing}', want $pixi_pin"
[ "$got_uv" = "$uv_pin" ] || fail "uv resolves '${got_uv:-nothing}', want $uv_pin"
echo "verify-self-provision(container): OK pins (pixi $got_pixi, uv $got_uv)" >&2

# Run 2: converged — a fast no-op, so no reconcile line may appear.
second="$(bash bin/setup-dev-env.sh 2>&1 || true)"
case "$second" in
*reconciling*) fail "second run still reconciles: $second" ;;
esac
echo "verify-self-provision(container): OK idempotent second run" >&2

# The env solve run 1 kicked off must hold up under --locked on its own.
pixi install --locked || fail "pixi install --locked failed"
pixi install --locked --environment lint || fail "pixi install --locked -e lint failed"
echo "verify-self-provision(container): OK locked env solves" >&2

# End to end: the full managed gate goes green on the provisioned box. lexd
# arrives the same way CI provisions it (the managed provision-lexd task).
pixi run -e lint provision-lexd || fail "provision-lexd failed"
pixi run -e lint lint || fail "pixi run -e lint lint failed"
echo "verify-self-provision(container): OK green lint gate" >&2
IN_CONTAINER
)"

echo "verify-self-provision: running the from-zero bootstrap" >&2
tar -C "$workdir" -cf - repo | docker run --rm -i "$image" bash -c "$in_container"

echo "verify-self-provision: PASS — Layer 0 provisions from zero" >&2
