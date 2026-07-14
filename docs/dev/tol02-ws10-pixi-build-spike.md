# TOL02-WS10 — pixi-build as the unifying build/bundle mechanism: spike + decision

> Spike report for [shipit#786](https://github.com/arthur-debert/shipit/issues/786)
> (TOL02 WS10), run 2026-07-12 against pixi 0.71.0 and the official
> `https://prefix.dev/pixi-build-backends` channel. The question (owner lead on
> the #658 survey): pixi's build work "theoretically covers ALL toolchain
> types" — should shipit's build/bundle stages route through `pixi build`
> instead of (or alongside) the per-toolchain compositions WS11–WS16 are about
> to author? Probes are real builds run locally (osx-arm64); the spike
> fixtures were scratch projects outside the repo and are deleted with the
> spike — this report is what survives.

STATUS: FINAL — verdict **NO-GO** on pixi-build as shipit's build/bundle
backend, for every disputed type. The posture already recorded in
`architecture.lex` §1/§3 and `lessons-learned.lex` ("pixi PROVISIONS and RUNS
the real builders, never IS the build backend") is **confirmed at
pixi 0.71.0** and is now evidence-dated rather than assumed. WS11–WS16
proceed as bespoke per-toolchain compositions in the shipit binary, keyed off
the ADR-0007 path→toolchain map — no ADR change (this spike *confirms*
ADR-0007/0040 posture, so no new ADR is minted).

## The question, made precise

"Route build/bundle through pixi build" can mean two different things:

1. **pixi tasks as the dispatch surface** (`pixi run build|bundle`): already
   the ADR-0007 answer for CI uniformity — and already resolved by the
   no-cross-manifest-task-inheritance constraint (`lessons-learned.lex`): the
   logic lives in the shipit binary, the consumer task is a one-line
   `./bin/shipit …` reference. Nothing to decide; WS11–WS16 inherit this.
2. **`pixi build` (the preview build-backend feature) as the producer of
   shipit's release artifacts**: this is the owner lead this spike tests.

The rest of this note is about (2).

## Probes and evidence

Four probes, all run for real on 2026-07-12 (pixi 0.71.0, osx-arm64):

1. **Representative real build (rust).** A minimal cargo bin package with
   `preview = ["pixi-build"]` and `backend = pixi-build-rust`
   (0.5.4.20260707) built green first try. Output:
   `ws10spike-0.1.0-hea27dcd_0.conda` — a conda archive wrapping
   `bin/ws10spike` plus conda `info/` metadata. The backend works, and what
   it produces is a **conda package**, not a release asset.
2. **Cross-target (the WS11 shape).** `pixi build --target-platform linux-64`
   from osx-arm64 fails in the build-env solve: `No candidates were found
   for rust_linux-64` — pixi-build does not provide cross-compilation; it
   builds for hosts it runs on. The WS11 gap (windows-x86_64, musl,
   darwin-x86_64 lanes) is exactly the part pixi-build does not cover.
   (Bonus churn signal: this invocation warns that `pixi build` with an
   output dir is deprecated in favour of `pixi publish` — the CLI surface is
   being renamed mid-preview.)
3. **Backend ecosystem sweep.** The official channel publishes exactly:
   `pixi-build-rust`, `pixi-build-python`, `pixi-build-cmake`,
   `pixi-build-mojo`, `pixi-build-rattler-build` (plus the api-version /
   interface metapackages). Probed absent: go, node/nodejs/npm, wasm,
   electron, tauri — i.e. **no backend exists for any disputed type**
   (WS12 wasm/npm, WS13 .vsix, WS14 electron, WS15 tauri, WS16
   tree-sitter). Backend versions are date-stamped rolling snapshots
   (`0.5.4.20260707.1429.2caad2a`) — preview-grade release discipline.
4. **The escape hatch.** `pixi-build-rattler-build` (a `recipe.yaml` with an
   arbitrary script) runs any command — a trivial recipe writing a payload
   file built green. But the output is structurally still a `.conda`
   archive. Arbitrary *execution* does not buy arbitrary *artifact shape*.

## Why NO-GO follows

- **Artifact-shape mismatch, structural not incidental.** Every disputed
  type's deliverable is an endpoint-native asset: platform zips and
  installers on gh-release (WS11), an npm tarball (WS12), per-target `.vsix`
  (WS13), `.dmg`/`.AppImage`/`.exe`+blockmaps (WS14), tauri bundles (WS15),
  a generated-parser tarball (WS16). `pixi build`'s output is a `.conda`
  package by construction (probes 1 and 4). Routing through it would mean
  building, conda-packing, then unpacking to recover the real asset — a
  detour that adds a format and deletes nothing.
- **The sign path forbids burying the artifact.** wf-sign-mac's
  reopen→reseal→notarize (ADR-0040, WS14/WS15 sign legs) operates on the
  real `.app`/`.dmg` between build and publish. An artifact entombed in a
  conda archive at build time is exactly the wrong hand-off.
- **Coverage is absent where it matters.** No backend for any disputed type
  (probe 3), and no cross-compilation for the one type that has a backend
  (probe 2). "Covers ALL toolchain types" holds only via the rattler-build
  escape hatch, which is just "run a script" — shipit already has a better
  home for that logic (the binary, per the task-inheritance constraint),
  with tests, without the conda wrapper.
- **Preview-grade churn.** Feature-flagged manifests
  (`preview = ["pixi-build"]`), date-stamped backend snapshots, and a CLI
  rename in flight (probe 2). shipit's own sharp-edge list already says to
  pin pixi surfaces because pre-1.0 churn is real; making a preview feature
  the load-bearing producer of every release artifact inverts that caution.

## The seam (unchanged, now explicit)

- **pixi provisions and runs; shipit composes.** Builders (cargo, wasm-pack,
  vsce, electron-builder, tauri-cli, tree-sitter-cli) are provisioned via
  the managed pixi surface (the #797 pattern: fail loudly when
  unprovisioned), invoked by compositions in the shipit binary, dispatched
  per-entry off the ADR-0007 path→toolchain map. Consumer-side, the only
  pixi coupling stays the one-line managed task.
- **Where pixi-build could earn a place later:** as a *producer for a conda
  endpoint* — if the portfolio ever ships to prefix.dev/conda-forge, `pixi
  build` is the natural builder for that one endpoint adapter, arriving as
  a registry entry per ADR-0007, not as the unifying mechanism.
- **Revisit triggers** (any of): pixi-build leaves preview; backends emit
  endpoint-native shapes (wheels, installers, tarballs) rather than only
  `.conda`; cross-target solves land for the rust backend; a
  backend appears for a disputed type. Until one fires, do not relitigate —
  WS11–WS16 build bespoke.

## WS11–WS16 disposition (the issue-body updates this decision requires)

Each body currently hedges "pixi-routed per WS10 or bespoke"; the decided
mechanism for all six is **bespoke composition in the shipit binary, pixi
for provisioning only**:

- **WS11 #787 (cross-target):** bespoke `--target` plumbing; pixi-build
  refuted for cross-compilation outright (probe 2).
- **WS12 #788 (wasm/npm):** wasm-pack composition; pixi provisions
  wasm-pack + the wasm32 target; npm tarball is the artifact.
- **WS13 #789 (.vsix):** vsce-based per-target composition; no backend, and
  `.vsix` is not a conda payload.
- **WS14 #790 (electron):** electron-builder composition; sign/notarize
  needs the naked `.app`/`.dmg` (see sign-path point above).
- **WS15 #791 (tauri):** tauri-cli composition; same sign-path argument.
- **WS16 #792 (tree-sitter):** tree-sitter generate/corpus/bundle
  composition; tarball artifact.

Applying these edits to the six issue bodies is coordinator work AFTER this
note merges (the merge is what ratifies the decision) — they are listed here
so the edit is mechanical.
