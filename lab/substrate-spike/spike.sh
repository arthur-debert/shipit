#!/usr/bin/env bash
# Substrate dev-loop spike: edit on host, execute in container.
# The runnable measurements behind docs/dev/substrate-devloop-spike.md.
#
# Usage: bash lab/substrate-spike/spike.sh <build-image|parity|ergonomics|all> [results-dir]
#
#   build-image  build the shipit wheel and the fleet rust-baseline image
#   parity       run shipit lint/test/build on the SAME rustloc commit on the
#                host (today's pixi path) and in the container, then diff;
#                exits nonzero on any rc/output/artifact difference
#   ergonomics   cold-start, build-cache (target on mount vs volume), file-watch
#                round-trip, and gh credential passthrough measurements
#
# Env overrides: SPIKE_IMAGE (image tag), SPIKE_RUSTLOC_REV (rustloc revision
# to measure; defaults to the commit the recorded report measured),
# SPIKE_RUSTLOC_DIR (reuse an existing checkout as-is instead of cloning),
# SPIKE_RUSTLOC_URL, SPIKE_NO_CACHE=1 (build-image with --no-cache).
set -euo pipefail

repo_root="$(cd -- "$(dirname "${BASH_SOURCE[0]}")/../.." >/dev/null && pwd)"
image="${SPIKE_IMAGE:-shipit-rust-baseline:spike}"
rustloc_url="${SPIKE_RUSTLOC_URL:-https://github.com/arthur-debert/rustloc.git}"
rustloc_rev="${SPIKE_RUSTLOC_REV:-84467c96604919dd8ae7518f722c30ef3de4b62f}"
rustloc_dir=""
results=""

die() {
    echo "spike: FAIL: $*" >&2
    exit 1
}

note() {
    echo "spike: $*" >&2
}

now_ms() {
    python3 -c 'import time; print(int(time.time() * 1000))'
}

# Run "$@" in a subshell with output to log file $1; sets timed_rc / timed_ms.
timed_rc=0
timed_ms=0
timed() {
    local log="$1"
    shift
    local t0 t1
    t0="$(now_ms)"
    timed_rc=0
    ("$@") >"$log" 2>&1 || timed_rc=$?
    t1="$(now_ms)"
    timed_ms=$((t1 - t0))
}

# Pins the harness-owned clone to $rustloc_rev (detached, verified clean) so
# reruns measure the same test subject; SPIKE_RUSTLOC_DIR is the explicit
# escape hatch and is used as-is, whatever it has checked out.
ensure_rustloc() {
    if [ -n "${SPIKE_RUSTLOC_DIR:-}" ]; then
        # Absolute path: docker -v rejects relative host paths.
        rustloc_dir="$(cd -- "$SPIKE_RUSTLOC_DIR" >/dev/null 2>&1 && pwd)" \
            || die "SPIKE_RUSTLOC_DIR=$SPIKE_RUSTLOC_DIR is not a directory"
        [ -d "$rustloc_dir/.git" ] || die "SPIKE_RUSTLOC_DIR=$rustloc_dir is not a git checkout"
    else
        rustloc_dir="$results/rustloc"
        if [ ! -d "$rustloc_dir/.git" ]; then
            note "cloning rustloc fresh into $rustloc_dir"
            git clone --quiet "$rustloc_url" "$rustloc_dir"
        fi
        [ -z "$(git -C "$rustloc_dir" status --porcelain)" ] \
            || die "harness clone $rustloc_dir is dirty; remove it and rerun"
        git -C "$rustloc_dir" rev-parse --quiet --verify "$rustloc_rev^{commit}" >/dev/null \
            || git -C "$rustloc_dir" fetch --quiet origin "$rustloc_rev"
        git -C "$rustloc_dir" checkout --quiet --detach "$rustloc_rev"
    fi
    git -C "$rustloc_dir" rev-parse HEAD >"$results/rustloc-commit.txt"
}

# Host runs use the Tree's own shipit build via the launcher's sanctioned
# SHIPIT_EXEC override, so host and container run the SAME shipit code and the
# substrate is the only variable. Runs under timed()'s subshell.
_host_cmd() {
    cd "$rustloc_dir"
    # Scrub the spawning Tree's pixi activation leaks; a real rustloc dev
    # shell has none of these set.
    unset PIXI_PROJECT_MANIFEST CARGO_TARGET_DIR SCCACHE_BASEDIRS CARGO_INCREMENTAL
    export SHIPIT_EXEC="$repo_root/.pixi/envs/default/bin/shipit"
    pixi run --locked "$@"
}

# The unmodified today's-path launcher (the repo's own shipit pin), for the
# cold-start anchor.
_host_launcher() {
    cd "$rustloc_dir"
    unset PIXI_PROJECT_MANIFEST CARGO_TARGET_DIR SCCACHE_BASEDIRS CARGO_INCREMENTAL
    pixi run --locked "$@"
}

_wheel_build() {
    cd "$repo_root"
    uv build --wheel --out-dir "$1"
}

host_verb() {
    local log="$1"
    shift
    timed "$log" _host_cmd "$@"
}

container_verb() {
    local log="$1"
    shift
    timed "$log" docker run --rm -v "$rustloc_dir:/work" "$image" "$@"
}

# Strip the run-varying noise (compile progress, durations, pixi task banners,
# the SHIPIT_EXEC announcement) so host and container output diff on findings
# only.
normalize() {
    sed -E \
        -e '/^shipit: SHIPIT_EXEC/d' \
        -e '/Pixi task/d' \
        -e '/^ *WARN /d' \
        -e '/^[[:space:]]*(Compiling|Checking|Finished|Downloading|Downloaded|Updating|Locking|Blocking|Installed)/d' \
        -e 's/in [0-9]+(\.[0-9]+)?s//g' \
        -e 's/\[[ 0-9.]+s\]/[]/g' \
        -e 's|/[^ ]*/shipit/data/|<data>/|g' \
        -e 's|^lint: [^ ]* |lint: <root> |' \
        "$1"
}

build_image() {
    command -v uv >/dev/null 2>&1 || die "uv is not on PATH (run from the shipit Tree's pixi env)"
    command -v docker >/dev/null 2>&1 || die "docker is not on PATH"
    local ctx
    ctx="$(mktemp -d)"
    note "building shipit wheel"
    timed "$results/wheel-build.log" _wheel_build "$ctx/wheels"
    [ "$timed_rc" -eq 0 ] || die "wheel build failed (see $results/wheel-build.log)"
    note "wheel built in ${timed_ms}ms"
    local extra=()
    if [ "${SPIKE_NO_CACHE:-0}" = "1" ]; then
        extra+=(--no-cache)
    fi
    note "building $image"
    timed "$results/image-build.log" docker build "${extra[@]+"${extra[@]}"}" \
        -t "$image" -f "$repo_root/docker/rust-baseline.Dockerfile" "$ctx"
    local rc="$timed_rc"
    rm -rf "$ctx"
    [ "$rc" -eq 0 ] || die "image build failed (see $results/image-build.log)"
    echo "image-build ms=$timed_ms no_cache=${SPIKE_NO_CACHE:-0}" | tee -a "$results/timings.txt"
}

# Artifact NAMES go to $out (compared host-vs-container); the binary format
# goes to $out.filetype (recorded only — Mach-O vs ELF is the expected
# platform difference, not a parity break).
artifact_listing() {
    local out="$1"
    if [ -d "$rustloc_dir/target/release" ]; then
        (cd "$rustloc_dir/target/release" && find . -maxdepth 1 -type f ! -name '*.d' | sort) >"$out"
        file "$rustloc_dir/target/release/rustloc" >"$out.filetype" 2>&1 || true
    else
        echo "(no target/release)" >"$out"
    fi
}

parity() {
    ensure_rustloc
    note "parity on rustloc commit $(cat "$results/rustloc-commit.txt")"

    rm -rf "$rustloc_dir/target"
    note "host: lint"
    host_verb "$results/host-lint.log" lint-full
    local host_lint_rc="$timed_rc" host_lint_ms="$timed_ms"
    note "host: test"
    host_verb "$results/host-test.log" test
    local host_test_rc="$timed_rc" host_test_ms="$timed_ms"
    note "host: build"
    host_verb "$results/host-build.log" ./bin/shipit build
    local host_build_rc="$timed_rc" host_build_ms="$timed_ms"
    artifact_listing "$results/host-artifacts.txt"

    rm -rf "$rustloc_dir/target"
    note "container: lint"
    container_verb "$results/cont-lint.log" shipit lint
    local cont_lint_rc="$timed_rc" cont_lint_ms="$timed_ms"
    note "container: test"
    container_verb "$results/cont-test.log" shipit test
    local cont_test_rc="$timed_rc" cont_test_ms="$timed_ms"
    note "container: build"
    container_verb "$results/cont-build.log" shipit build
    local cont_build_rc="$timed_rc" cont_build_ms="$timed_ms"
    artifact_listing "$results/cont-artifacts.txt"

    normalize "$results/host-lint.log" >"$results/host-lint.norm"
    normalize "$results/cont-lint.log" >"$results/cont-lint.norm"
    diff -u "$results/host-lint.norm" "$results/cont-lint.norm" >"$results/lint.diff" || true
    grep -E 'tests run|TEST:' "$results/host-test.log" >"$results/host-test.norm" || true
    grep -E 'tests run|TEST:' "$results/cont-test.log" >"$results/cont-test.norm" || true
    diff -u <(normalize "$results/host-test.norm") <(normalize "$results/cont-test.norm") \
        >"$results/test.diff" || true
    diff -u "$results/host-artifacts.txt" "$results/cont-artifacts.txt" \
        >"$results/artifacts.diff" || true

    # The verdict: any nonzero verb, empty test-summary extraction, or
    # host/container difference fails parity (and stops `all`).
    local failures=()
    if [ "$host_lint_rc" -ne 0 ] || [ "$cont_lint_rc" -ne 0 ]; then
        failures+=("lint rc: host=$host_lint_rc cont=$cont_lint_rc")
    fi
    if [ "$host_test_rc" -ne 0 ] || [ "$cont_test_rc" -ne 0 ]; then
        failures+=("test rc: host=$host_test_rc cont=$cont_test_rc")
    fi
    if [ "$host_build_rc" -ne 0 ] || [ "$cont_build_rc" -ne 0 ]; then
        failures+=("build rc: host=$host_build_rc cont=$cont_build_rc")
    fi
    if [ ! -s "$results/host-test.norm" ] || [ ! -s "$results/cont-test.norm" ]; then
        failures+=("test summary extraction came up empty (host-test.norm / cont-test.norm)")
    fi
    if [ -s "$results/lint.diff" ]; then
        failures+=("lint output differs (lint.diff)")
    fi
    if [ -s "$results/test.diff" ]; then
        failures+=("test results differ (test.diff)")
    fi
    if [ -s "$results/artifacts.diff" ]; then
        failures+=("artifact names differ (artifacts.diff)")
    fi

    {
        echo "rustloc commit: $(cat "$results/rustloc-commit.txt")"
        echo "verb   host_rc cont_rc host_ms cont_ms"
        echo "lint   $host_lint_rc $cont_lint_rc $host_lint_ms $cont_lint_ms"
        echo "test   $host_test_rc $cont_test_rc $host_test_ms $cont_test_ms"
        echo "build  $host_build_rc $cont_build_rc $host_build_ms $cont_build_ms"
        echo "lint diff lines (normalized): $(grep -c '^[+-][^+-]' "$results/lint.diff" || true)"
        echo "test diff lines (normalized): $(grep -c '^[+-][^+-]' "$results/test.diff" || true)"
        echo "artifact name diff lines: $(grep -c '^[+-][^+-]' "$results/artifacts.diff" || true)"
        if [ "${#failures[@]}" -gt 0 ]; then
            printf 'parity: FAIL: %s\n' "${failures[@]+"${failures[@]}"}"
        else
            echo "parity: PASS"
        fi
    } | tee "$results/parity-summary.txt"
    [ "${#failures[@]}" -eq 0 ] || die "parity failed (logs and diffs in $results)"
}

# One cold/warm/incremental `shipit build` trio; $1 label, rest: extra docker
# run args (label "host" = today's pixi path).
build_trio() {
    local label="$1"
    shift
    rm -rf "$rustloc_dir/target"
    local src cold warm incr
    src="$(find "$rustloc_dir/crates" -name '*.rs' | head -1)"
    if [ "$label" = "host" ]; then
        host_verb "$results/cache-$label-cold.log" ./bin/shipit build
        cold="$timed_ms"
        host_verb "$results/cache-$label-warm.log" ./bin/shipit build
        warm="$timed_ms"
        touch "$src"
        host_verb "$results/cache-$label-incr.log" ./bin/shipit build
        incr="$timed_ms"
    else
        timed "$results/cache-$label-cold.log" \
            docker run --rm -v "$rustloc_dir:/work" "$@" "$image" shipit build
        cold="$timed_ms"
        timed "$results/cache-$label-warm.log" \
            docker run --rm -v "$rustloc_dir:/work" "$@" "$image" shipit build
        warm="$timed_ms"
        touch "$src"
        timed "$results/cache-$label-incr.log" \
            docker run --rm -v "$rustloc_dir:/work" "$@" "$image" shipit build
        incr="$timed_ms"
    fi
    echo "build-cache $label cold_ms=$cold warm_ms=$warm incr_ms=$incr" | tee -a "$results/timings.txt"
}

file_watch_cleanup() {
    docker rm -f spike-watch >/dev/null 2>&1 || true
    rm -f "$rustloc_dir"/.spike-ping-* "$rustloc_dir"/.spike-pong-* \
        "$rustloc_dir/.spike-stop" 2>/dev/null || true
}

file_watch() {
    file_watch_cleanup
    trap file_watch_cleanup EXIT
    docker run -d --name spike-watch -v "$rustloc_dir:/work" "$image" bash -c \
        'i=0; while [ ! -f /work/.spike-stop ]; do
            if [ -f "/work/.spike-ping-$i" ]; then : >"/work/.spike-pong-$i"; i=$((i + 1)); fi
            sleep 0.01
        done' >/dev/null
    local watch_rc=0
    python3 - "$rustloc_dir" <<'PYEOF' | tee -a "$results/timings.txt" || watch_rc=$?
import os
import statistics
import sys
import time

d = sys.argv[1]
rtts = []
time.sleep(1)  # let the watcher start
for i in range(10):
    ping, pong = f"{d}/.spike-ping-{i}", f"{d}/.spike-pong-{i}"
    t0 = time.monotonic()
    open(ping, "w").close()
    deadline = t0 + 30
    while not os.path.exists(pong):
        if time.monotonic() > deadline:
            print(f"file-watch: no pong for ping {i} within 30s", file=sys.stderr)
            sys.exit(1)
        time.sleep(0.001)
    rtts.append((time.monotonic() - t0) * 1000)
open(f"{d}/.spike-stop", "w").close()
print(f"file-watch rtt_ms median={statistics.median(rtts):.0f} "
      f"min={min(rtts):.0f} max={max(rtts):.0f} all={[round(r) for r in rtts]}")
PYEOF
    if [ "$watch_rc" -ne 0 ]; then
        note "watcher container state:" \
            "$(docker inspect -f '{{.State.Status}}' spike-watch 2>/dev/null || echo removed)"
        docker logs spike-watch >&2 || true
        die "file-watch probe failed or timed out"
    fi
    file_watch_cleanup
    trap - EXIT
}

gh_passthrough() {
    local token rc
    token="$(gh auth token 2>/dev/null)" || die "host gh has no token (gh auth login first)"
    rc=0
    # The token rides the environment (-e GH_TOKEN with no value), never argv.
    GH_TOKEN="$token" docker run --rm -e GH_TOKEN "$image" \
        bash -c 'gh auth status && gh api user --jq .login' \
        >"$results/gh-token-env.log" 2>&1 || rc=$?
    echo "gh-passthrough token-env rc=$rc" | tee -a "$results/timings.txt"
    rc=0
    if [ -d "$HOME/.config/gh" ]; then
        docker run --rm -v "$HOME/.config/gh:/root/.config/gh:ro" "$image" \
            bash -c 'gh auth status && gh api user --jq .login' \
            >"$results/gh-config-mount.log" 2>&1 || rc=$?
        echo "gh-passthrough config-mount rc=$rc" | tee -a "$results/timings.txt"
    else
        echo "gh-passthrough config-mount skipped (no ~/.config/gh)" | tee -a "$results/timings.txt"
    fi
}

ergonomics() {
    ensure_rustloc
    docker image inspect "$image" >/dev/null 2>&1 || die "image $image missing — run build-image first"

    local i
    for i in 1 2 3 4 5; do
        timed /dev/null docker run --rm "$image" true
        echo "container-start noop run=$i ms=$timed_ms" | tee -a "$results/timings.txt"
    done
    for i in 1 2 3 4 5; do
        timed "$results/cont-version-$i.log" docker run --rm "$image" shipit --version
        echo "container-start shipit-version run=$i ms=$timed_ms" | tee -a "$results/timings.txt"
    done
    # The real today's-path anchor: rustloc's pinned launcher under pixi.
    for i in 1 2 3; do
        timed "$results/host-version-$i.log" _host_launcher ./bin/shipit --version
        echo "host-launcher shipit-version run=$i ms=$timed_ms" | tee -a "$results/timings.txt"
    done

    docker volume rm -f spike-target spike-registry >/dev/null 2>&1 || true
    build_trio host
    # Ephemeral registry: every fresh container refetches the crate sources.
    build_trio mount-target-ephemeral-registry
    # Prewarm a registry volume so the two target-dir strategies compare fairly.
    rm -rf "$rustloc_dir/target"
    timed "$results/registry-prewarm.log" docker run --rm -v "$rustloc_dir:/work" \
        -v spike-registry:/opt/cargo/registry "$image" cargo fetch
    build_trio mount-target -v spike-registry:/opt/cargo/registry
    build_trio volume-target -v spike-target:/work/target -v spike-registry:/opt/cargo/registry
    docker volume rm -f spike-target spike-registry >/dev/null 2>&1 || true

    file_watch
    gh_passthrough
    note "ergonomics numbers in $results/timings.txt"
}

main() {
    local cmd="${1:-}"
    results="${2:-${TMPDIR:-/tmp}/spike-1198-results}"
    mkdir -p "$results"
    # Absolute path: docker -v rejects relative host paths.
    local abs_results
    abs_results="$(cd -- "$results" >/dev/null 2>&1 && pwd)" \
        || die "cannot resolve results dir: $results"
    results="$abs_results"
    case "$cmd" in
        build-image)
            build_image
            ;;
        parity)
            parity
            ;;
        ergonomics)
            ergonomics
            ;;
        all)
            build_image
            parity
            ergonomics
            ;;
        *)
            die "usage: spike.sh <build-image|parity|ergonomics|all> [results-dir]"
            ;;
    esac
}

main "$@"
