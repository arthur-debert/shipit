# Substrate dev-loop spike — edit on host, execute in container

> Spike report for
> [shipit#1198](https://github.com/arthur-debert/shipit/issues/1198) —
> deliverability spike #2 of `docs/design/delivery-system.md` and the Suburbia
> Phase 2 probe (ADR-0084). Run 2026-07-31 on the owner's darwin-arm64 host
> (Apple M3, 8 cores, 16 GB) against Docker Desktop 4.82.0's linux-aarch64
> VM (8 CPUs, 7.75 GB, VirtioFS mounts).
> Test repo: `arthur-debert/rustloc` at `84467c9`, cloned fresh.
> Everything here is reproducible from `lab/substrate-spike/spike.sh`
> (`build-image` / `parity` / `ergonomics`); the image definition is
> `docker/rust-baseline.Dockerfile`. All numbers are wall-clock measurements
> from the script, never eyeballed. linux-arm64 pins only — the published
> fleet image adds the other platforms.

## The setup

The fleet baseline image follows ADR-0084 literally: the image owns the
toolchain — apt + native toolchains (rustup, node tarball, uv-managed
python), **no pixi anywhere in the container**. It bakes the rust toolchain
(cargo/clippy/rustfmt 1.96.1 + cargo-nextest), every linter the fleet's rust
verbs route to (shellcheck, shfmt, actionlint, yamllint, ruff, prettier,
markdownlint-cli), `gh`, and shipit itself, installed as a `uv tool` from a
wheel built out of the working Tree — so the container runs the **real
`shipit lint` / `shipit test` / `shipit build` verbs**, not raw cargo.
Version pins mirror the fleet's managed pixi pins, so a host run and a
container run resolve the same tool versions.

The dev loop under test: code stays on the host filesystem, the checkout
mounts at `/work`, and each verb is one `docker run --rm -v <checkout>:/work
<image> shipit <verb>`. The host side of every comparison is today's real
path: `pixi run --locked` in the rustloc checkout (warm pixi envs), with the
launcher's sanctioned `SHIPIT_EXEC` override pointing at the same shipit
build the image carries, so the substrate is the only variable.

## Parity — the DoD: PASS

Same rustloc commit, same shipit code (build `49b42ff` on both sides),
host and container:

| verb  | host rc | container rc | result diff (normalized)                  |
| ----- | ------- | ------------ | ----------------------------------------- |
| lint  | 0       | 0            | **0 lines** — all 12 checks, same files   |
| test  | 0       | 0            | **0 lines** — 266/266 passed both sides   |
| build | 0       | 0            | same artifact set; Mach-O vs ELF expected |

- Lint runs the identical 12-check plan (3 clippy crates, 3 rustfmt crates,
  shell ×3 files, yaml ×6, actions ×2, web ×4, markdown ×21) with identical
  verdicts. After normalizing run-varying noise (absolute config paths,
  durations, compile progress) the diff is empty.
- `shipit build` produces the same artifact names; the binary is ELF
  aarch64 in-container vs Mach-O arm64 on the host — that platform
  difference is the substrate's point, not a break.
- No verb hard-blocked in-container. The image bakes
  `git config --system --add safe.directory '*'` for the host-owned mount
  (lint scopes via `git ls-files`); on Docker Desktop the mount appears
  root-owned in-container so the setting was not load-bearing in this run —
  it covers uid-mismatched setups (Linux hosts, non-root containers).
- The parity legs also carry timings (host→container on a bare mount:
  lint 24.5 s→73.2 s, test 26.0 s→84.9 s) — performance is the ergonomics
  section's subject, with cache mitigations.

## Ergonomics — the numbers

### Cold-start latency

| measurement                                     | ms                   |
| ----------------------------------------------- | -------------------- |
| image build, fully cold (`--no-cache`)          | 84 673               |
| image build, warm layer cache                   | 319                  |
| image size (disk usage / compressed content)    | 2.24 GB / 544 MB     |
| `docker run --rm <image> true` (×5)             | 202–419, median 225  |
| `docker run --rm <image> shipit --version` (×5) | 904–1019, median 968 |
| host `pixi run ./bin/shipit --version` (×3)     | 309–525, median 350  |

Container start overhead is a non-issue: ~0.2 s to a process, ~1 s to a
shipit verb — the same order as today's pinned-launcher path (~0.35 s).

### Incremental build cache across container invocations

`shipit build` (cargo release build) trios: cold (no `target/`), warm
(immediate no-op re-run), incremental (after touching one `.rs`):

| scenario                                 | cold ms | warm ms | incr ms |
| ---------------------------------------- | ------- | ------- | ------- |
| host (today's pixi path)                 | 11 188  | 620     | 553     |
| target on mount, ephemeral registry      | 153 148 | 32 010  | 7 943   |
| target on mount + registry volume        | 123 900 | 35 396  | 1 452   |
| target in named volume + registry volume | 77 074  | 1 321   | 1 200   |

(The host cold number varies: the first-ever release build of the checkout
took 51.3 s in the parity sequence; two clean re-measurements gave 11.2 s
and 10.9 s — identical 256-crate compiles, no downloads — so ~11 s is the
representative host cold and the ratios below use it.)

Three load-bearing observations:

1. **The target dir must live in a named volume, not on the mount.**
   Compiling through VirtioFS adds ~60% to the cold build (124 s vs 77 s)
   and, worse, target-on-mount does not settle: the no-op run right after a
   cold build spends 32–35 s re-verifying — VirtioFS mtime behavior keeps
   cargo re-fingerprinting — and only later invocations reach ~1.5 s.
   Target-in-volume is clean at once (1.3 s no-op right after cold).
2. **The cargo registry must persist too.** With an ephemeral registry every
   fresh container refetches the index + crate sources: ~6.5 s added to even
   a one-crate incremental (7.9 s vs 1.5 s).
3. **Cold compile in the VM is the honest headline cost:** 77 s vs 11 s on
   the host (7×) with everything else mitigated. Steady-state loops are
   fine (no-op 1.3 s vs 0.6 s; leaf-crate incremental 1.2 s vs 0.55 s —
   both ~2.2×), so the penalty concentrates on full rebuilds. Suspects: the
   default 8 GB VM, source reads through VirtioFS, VM scheduling — not
   decomposed further in this spike.

### File-watch latency

Host-write → container-visible → host-sees-response round trip, measured 10×
through the mount (container polling at 10 ms): **median 13 ms** (9–14 ms).
No barrier for watch-triggered tooling.

### gh credential passthrough

| mechanism                        | verdict   |
| -------------------------------- | --------- |
| `-e GH_TOKEN="$(gh auth token)"` | **works** |
| mount `~/.config/gh` read-only   | fails     |

Env passthrough authenticates fully (`gh auth status` + API calls). The
config-mount route fails on macOS because the token lives in the keychain,
so the mounted `hosts.yml` carries no valid secret.

The sane mechanism is env passthrough of `gh auth token` at `docker run`
time; nothing needs to be baked or written.

## What feeds the Substrate design

- **Parity holds** on a real repo with the real verbs. The container legs can
  replace host execution for rust lint/test/build without changing results.
- **Image mechanics that worked:** pins mirrored 1:1 from the fleet's managed
  pixi pins (rust 1.96.1, shellcheck 0.10.0, shfmt 3.13.1, actionlint
  1.7.12, yamllint 1.38.0, prettier 3.8.5, markdownlint-cli 0.49.0, ruff
  0.15.20, nextest 0.9.140); shipit installed as a `uv tool` from a wheel of
  the working Tree — exactly the ADR-0084 Revision-pin shape (the definition
  travels with the pin, the dev loop builds locally); `safe.directory '*'`
  baked in for the host-owned mount.
- **The run shape needs two named volumes per repo** (target dir, cargo
  registry) or an equivalent derived cache location — the mitigation that
  turns a 14–52× steady-state loop into a ~2× one. Whatever invokes the
  container (the future substrate runner) should own creating/keying them;
  developers must not hand-manage volume names.
- **Ergonomic residue to accept:** cold/full rebuilds ~7× slower than
  native in the default Docker Desktop VM; worth re-measuring on a tuned VM
  before declaring it structural.
- **Scope honesty:** linux-arm64 only; single small rust repo; Docker
  Desktop/VirtioFS specifically — colima/OrbStack may move the VirtioFS
  numbers; the Mac exception legs (ADR-0084) were out of scope.

## Fail criteria verdict

No parity break. The unmitigated dev loop *is* materially slower
(target-on-mount, ephemeral registry: 14–52× on steady-state operations),
but the mitigations are cheap, mechanical, and measured above — with them,
steady-state edit-build-test is ~2× of host, container start costs are
negligible, and only full rebuilds keep a real penalty (~7×). Spike
passes; the open design items are image build/publish mechanics and the
runner owning the cache volumes.
