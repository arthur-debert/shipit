# Spec — Cross-repo artifacts: collapse to conda-direct

**Status:** draft for review · **Supersedes:** the accreted parts of ADR-0064/0067/0070/0071 (see §7) · **Reframes:** ARF02 (#999, #1059, #1078, #1079, #1087)

## 1. Problem

Getting one repo's artifact produced and consumed by another has taken **three sessions and ~8M tokens**. The root cause is not the task — produce→consume is a solved problem — it is that **shipit built a second package manager on top of pixi/conda**, which already is one. Every recurring "bug" is one of two things:

- **Restatement drift:** a fact (name, version, payload) authored on two sides with no single source of truth, so it drifts and is caught late. Evidence, today: `lexd-lsp` live at `0.17.0` / `0.19.10-rc.1` / `0.19.10` at once; `tree-sitter` consumers pinned `v0.11.0` while the channel serves `0.11.4` (and the `.wasm` is missing from the tarball); `lexd` at `0.19.10` (conda) vs `v0.14.1` (gh-release) *inside one repo*; phos corpus/fixtures `v0.21.0` vs crates `v0.22.0`; **five different names** for the one tree-sitter artifact.
- **Wrapper defects:** bugs in layers that re-implement what conda already does correctly.

The one axis that has **never** drifted is the channel *location* — the only fact that is derived from a single source rather than restated. That is the whole thesis in one data point.

## 2. Target model (conda-direct)

- **Producer** packages its build output **directly** as a `.conda` and publishes it to the channel (`rattler-build` — one command). One name, carried *in* the package.
- **Consumer** writes a **normal conda dependency** (`channels = [...]` + `pkg = "ver"`) in `pixi.toml`. `pixi lock` resolves, pins, sha256-verifies. **The resolver is the agreement** — a wrong name/version fails locally, before any release.
- **Channel** = a GCS bucket (authless-read, per-producer-repo layout) holding both the `.conda` files and the `repodata.json` index. Necessary infrastructure; it exists and works.
- **Staging** (only for app-type consumers): after conda extracts into the env prefix, a tiny generic file-copy moves the embedded files into the app's shipped bundle (`resources/`). Tools used from the env (e.g. `lexd`) need nothing.

## 3. Decision (the one fork)

The `[artifact-deps]` DSL, the managed-block pin ownership, and Cascade are **one coupled bundle** whose only load-bearing justification is **cross-repo pin governance** (auto-bump on release + enforced fleet-uniform pins).

**Decision: hand pin governance to pixi + a generic bot (Renovate/Dependabot).** `pixi.lock` is the record; a standard bot opens version-bump PRs. This deletes all three layers and lands us at conda-direct.

**One carve-out:** the `lexd` **lint-tool** uniformity (every repo must run the identical linter version) *is* genuine fleet governance — a different concern from *consuming an artifact*. Keep the managed block **for that one tool only**; it does not drag the DSL along.

## 4. The cut line (from the layer-by-layer pass)

**Kernel — keep (necessary, survives regardless):**

| Layer | Why it stays |
| --- | --- |
| `rattler-build` build → publish → index `.conda` to the channel | The irreducible producer (incl. the load-bearing `binary_relocation: false` / no-relink insight) |
| conda as one row in the release fan-out | "release once → gh-release + npm + conda" is a genuine need |
| tier→URL glue (`public→https`, `private→s3 + [s3-options]`) | ~30 lines pixi genuinely can't derive |
| two-tier bucket provisioning (`store_provision`) | authless-public / private-creds is a real access model |
| RC-guard + `--endpoint` selector | protects the *other* endpoints from co-firing on a conda seed |

**Accretion — delete:**

| Layer | Replace with |
| --- | --- |
| gh-release → conda **repackage hop** (re-downloads its own asset, reverse-engineers the filename) | Package the build output **directly** as a first-class `.conda` |
| `[artifact-deps]` **DSL** | A plain conda dep + channel in `pixi.toml` (keep the tier-URL helper) |
| managed-block/hash **ownership over pins** | `pixi.lock` (already pins + verifies) — *except* the `lexd` lint-tool carve-out |
| **Cascade** (a bespoke Renovate) | `pixi update` / a generic update bot |
| readiness-gate / served-subdir / pause **bookkeeping** | `pixi lock` — a missing subdir is a failed resolve (fail-closed, which is what the win-64 pause wants anyway) |

## 5. Essential dimensions & the reference table

Dimensions that matter: **name** (key), **arch** (noarch vs per-platform), and — consumer-side — **in-place vs staged**. Everything else is automatic or single-sourced: integrity (repodata + `pixi.lock`), transitive deps (conda), channel URL (derived from repo), and **version** (dynamic — lives in `repodata.json` and `pixi.toml`, **never tabulated**).

**Rule: the table captures structure, never state.** If a fact changes on its own schedule and already has an authoritative home, it never enters a maintained document.

Reference table (stable — changes only when an artifact or consumer is added/removed):

`producer repo | package name | noarch/arch | consumers | in-place or staged`

## 6. Migration — one by one, all local

- **Producer, per artifact:** build the `.conda` directly; verify name / version / subdir / contents against the channel — **local** (the Gate 0 harness, `tools/conda_channel_roundtrip.py`, already does build → `file://` → resolve).
- **Consumer, per repo:** write a normal conda dep; `pixi lock` (that resolve **is** the agreement check) — local; run the staging copy; run the app — local.
- **The only two things that need the outside world:** compiling a Linux-native binary on a Mac, and a *live* publish to crates.io/npm/marketplace. Everything else is local **and independent** — each artifact and each consumer verifiable on its own, no release, no N×N. That independence is why it converges.

## 7. What this supersedes

- **ADR-0064** — keep the conda-channel concept; supersede the `[artifact-deps]` DSL and the derived-after-gh-release repackage requirement.
- **ADR-0067** — supersede (Cascade removed).
- **ADR-0070/0071** — the selector/RC-guard survive (they protect the fan-out); the readiness-gate/served-subdir bookkeeping is superseded by resolve-time availability.
- **ADR-0065** — unchanged (two-tier buckets).
- **ARF02:** #1078 stays (build the grammar `.conda` directly — now simpler); #1079 stays (staging copy — simpler, generic); **#1087 dropped** (`channel verify` is itself accretion — `pixi lock` is the verify); #999/#1059 re-scoped to this PRD.

## 8. Definition of done

Every step is verified by a **real local run** (a real `.conda`, a real `pixi lock`, a real staging copy, the real app), not a fixture. Only Linux-native compilation and live third-party publish are release-gated.

## 9. Task list (for the epic issue)

1. Producer: package build output directly as `.conda`; delete the gh-release→conda hop + asset-name reverse-engineering.
2. Consumer: replace `[artifact-deps]` with a plain conda dep + channel; keep the tier-URL helper.
3. Remove Cascade; adopt `pixi update` / a generic bump bot.
4. Remove the readiness-gate/served-subdir bookkeeping; `pixi lock` is the availability oracle.
5. Retain the managed block **only** for the `lexd` lint-tool uniformity; drop it for artifact consumption.
6. Migrate each producer, then each consumer, one by one — each verified by a real local run.
