# CI cache spike — pixi provisioning + caching for the wf-checks block

> Spike report for [shipit#582](https://github.com/arthur-debert/shipit/issues/582),
> run 2026-07-09 on shipit-canary (the standing test bed). The probe workflows
> lived on canary (`cache-spike.yml`, `docker-cache-spike.yml`, a rust fixture
> crate, env-carried uv) and are deleted with the spike; this report is what
> survives. Numbers are step durations from the Actions API
> (`gh run view --json jobs`), never eyeballed. Plan grilled against
> ADR-0010/0015/0033/0039/0040 before execution.

## The five decisions, answered

### 1. setup-pixi caching: already on in production; two sharp edges

What `prefix-dev/setup-pixi` does with **no cache inputs** (the current
`wf-checks.yml` state): `cache: true` (when `pixi.lock` exists),
`cache-write: true`, key `pixi-<conda-arch>-<sha256(pixi.lock)>`,
`run-install: true` (default env). Production is therefore **already
lockfile-keyed caching** — verified in shipit CI run 29010252485: all three
jobs restore `pixi-linux-64-<lockhash>`.

Sharp edge A — **cache is coupled to install**: `cache: true` with
`run-install: false` is a hard error ("Cannot cache without running
install"). The block cannot separate restore-timing from install-timing into
different steps; it must let setup-pixi install the lane's envs itself via the
`environments:` input.

Sharp edge B — **one key, many envs**: every job of a run shares the single
`(arch, lockhash)` key while installing different envs, and Actions cache keys
are immutable — the first job to finish saves, every other save is rejected.
Whether a lane's env is warm forever after depends on which job won the first
save. Compounding it, our per-lane key isolation in the spike showed the
opposite failure: three rust lanes wrote three **identical 496 MB** entries.

**Recommendation:** key the cache on the *env-set*, not the lane —
`cache-key: pixi-<envs>-` (the planner knows each lane's envs; see decision
5). Lanes sharing an env-set share one entry; different env-sets stop racing
for one key. Keep `cache-write: true` everywhere (the grilled policy: warm PR
branches from their second push; sizes below say quota is manageable).

Cold vs warm (canary; conda-forge envs):

| env (lane)                  | cold setup-pixi | warm setup-pixi | entry size |
| --------------------------- | --------------- | --------------- | ---------- |
| lint, linux                 | 10s             | 4s              | 152 MB     |
| lint, macos-14 (osx-arm64)  | 8s              | 6s              | 109 MB     |
| default (python-only)       | 4s              | 2s              | 32 MB      |
| rust-spike (rust 1.96)      | 18–36s          | 12–16s          | 496 MB     |

The honest headline: **cold conda solves are already fast**. Caching roughly
halves env setup; it does not transform it. The win scales with env size
(rust) and is worth having, but nothing here justifies complexity.

### 2. Rust: rust-cache delivers the whole win; sccache costs more than it pays

Ladder on a fixture crate (serde + serde_json + regex, ~24s clean build) with
the pixi-provisioned toolchain and the ADR-0015 `[activation.env]` block
(`CARGO_TARGET_DIR`, `SCCACHE_BASEDIRS`, `CARGO_INCREMENTAL=0`) in place:

| config                   | cold build+test | warm build+test |
| ------------------------ | --------------- | --------------- |
| no cache                 | 24s             | 24s             |
| Swatinem/rust-cache      | 24s             | **1s**          |
| rust-cache + sccache-GHA | **38s**         | 2s              |

- **Composition works.** rust-cache needs `rustc` on the *runner* PATH (pixi
  never puts it there): one `echo "$PWD/.pixi/envs/<env>/bin" >> "$GITHUB_PATH"`
  step after setup-pixi fixes it. The repo-root `CARGO_TARGET_DIR` is covered
  by rust-cache's workspace mapping (`workspaces: "spike/rust-fixture -> ../../target"`).
  No interference from the ADR-0015 env was observed in any configuration.
- **sccache (GHA backend) made cold builds 58% slower** (24s → 38s:
  per-compile-unit cache-miss overhead) and added nothing warm — a rust-cache
  target/ hit skips compilation entirely, leaving sccache no work. Its niche
  (object reuse when the rust-cache key busts but most compile units are
  unchanged) is real but narrow.

**Recommendation:** rust lanes get the PATH-export step + `Swatinem/rust-cache`.
No sccache in CI by default; revisit only if key-busting changes (toolchain
bumps, lockfile churn on big crates) measurably hurt. Scale caveat: the
fixture is small — re-measure the knee on a real crate (e.g. rustloc) before
treating the sccache verdict as final.

### 3. Docker: type=gha works — but needs the runtime token, and our image is too small to care

Three-way on the stock `ubuntu.Dockerfile` (24.04 + five apt packages):

| config                                         | cold | warm     |
| ---------------------------------------------- | ---- | -------- |
| plain `docker build`                           | 22s  | 14–31s\* |
| buildx `type=gha`, bare `run:` step            | 15s  | 12–14s   |
| buildx `type=gha`, subprocess + runtime action | 22s  | **1–2s** |

\* apt-mirror variance across runs.

The middle row is the finding: a **bare `docker buildx build` in a `run:`
step silently gets no GHA cache** — `type=gha` needs `ACTIONS_RUNTIME_TOKEN`/
`ACTIONS_CACHE_URL`, which only `docker/build-push-action` wires implicitly;
any direct buildx invocation (step **or** in-code — the `shipit wf test`
in-pytest build) needs `crazy-max/ghaction-github-runtime` first. With the
token exposed, the in-code-shaped build hit fully (`CACHED` layers, 1–2s).

**Recommendation:** not worth block complexity today — the image is
~20-something seconds to build from scratch and apt variance rivals the
saving. Record the runtime-token requirement for when CI builds real images
(WF01 build lanes); C/C++ ccache likewise deferred until a tauri/native lane
onboards.

### 4. uv in consumer CI: env-carried uv works; the private-repo token is the real gap

The gap (structural, confirmed on canary): a consumer's `bin/shipit` rides
`uv tool run --from git+.../shipit@<pin>` (ADR-0033) and wf-checks installs
only pixi — no lane that touches `./bin/shipit` can run.

**Env-carried uv validated:** `uv = "0.11.*"` in the consumer's default
feature (conda-forge solved 0.11.28 — exactly the Layer 0 `UV_PIN`) puts uv
on PATH inside every `pixi run`, keeps the block setup-pixi-only, rides the
lockfile pin and the setup-pixi cache. The pin resolve through the launcher:
**5s cold, 0–3s warm** — uv builds the shipit wheel from git faster than
expected, and a dedicated `actions/cache` over `~/.cache/uv` (31 MB) moved 5s
to 3s. **Skip the extra cache step**; it does not pay for its block surface.

Two consequences for the epic:

- **Cross-repo credentials.** shipit is private, so the consumer job needs a
  token that can read it (a consumer's `GITHUB_TOKEN` cannot). The spike used
  an operator-token secret + `git config url.insteadOf`; the fleet needs a
  durable answer (fine-grained PAT or App installation token) delivered via
  `gh-setup`'s `[secrets]` machinery.
- **Second uv pin.** The conda-forge spec duplicates Layer 0's `UV_PIN` —
  wants the same lockstep drift test `PIXI_PIN` already has
  (tests/test_install.py pattern).

### 5. Config home: planner-emitted cache descriptors — proven

The spike's `plan` job emitted a hardcoded planner-shaped matrix in which each
lane carries a nested cache descriptor
(`"caches": { "rust": true, "sccache": false, "uv": false }`), and the run
job's **static** steps gate on it (`if: matrix.caches.rust`). `fromJSON`
matrix include + nested-object dot access + step gating all work as intended,
across seven lanes and two rounds.

**Recommendation:** `shipit ci plan` derives cache descriptors (and the
env-set for the cache key, per decision 1) from `.shipit.toml [toolchains]` +
lane declarations and emits them as matrix fields; the block carries the
static gated steps. Logic stays in the fixture-tested planner, the block stays
routing-only — exactly how `runner`/`required` flow today (ADR-0040 intact).
The ADR for the descriptor contract belongs to the epic that implements it.

## What the wf-checks block changes to (the epic's worklist)

1. setup-pixi: add `environments: <lane env-set>` + `cache-key: pixi-<env-set>-`
   (both planner-supplied); keep `cache: true`, `cache-write: true`, the
   `pixi-version` lockstep comment, and `locked: true`.
2. Gated rust steps: PATH export + `Swatinem/rust-cache` behind
   `matrix.caches.rust`. No sccache step.
3. uv: managed `uv = "0.11.*"` dep rolled to consumers via the install
   reconcile; UV_PIN lockstep drift test; the cross-repo read-token story via
   `gh-setup [secrets]`.
4. Planner: cache descriptors + env-set in the emitted matrix; fixture tests.
5. Deferred, recorded: docker gha caching (runtime-token note above), sccache,
   ccache, macOS beyond the smoke (osx-arm64 caching verified working).

## Cost and cleanup

One full spike round wrote ~1.9 GB of cache across 15 entries (dominated by
the tripled 496 MB rust env — the per-env keying above is the fix). Repo
quota is 10 GB with LRU eviction; write-everywhere is sustainable for
fleet-sized envs, watch it for repos with several large env-sets.

Spike residue, all deleted at close: canary scaffolding (workflows, fixture
crate, docker/, uv dep + rust-spike feature in the manifest), the
`spike/ci-cache-v2` branch, the `SHIPIT_READ_TOKEN` secret (operator token —
rotated out), and the spike cache entries (evicted naturally).
