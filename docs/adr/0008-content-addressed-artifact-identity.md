# Content-addressed artifact identity

The largest lever on the fleet's CI cost (agents waiting the majority of their time
on CI) is to **build once and reuse** — across lanes within a run, across workflows,
and across git revisions when the inputs that determine a binary are unchanged (a
docs-only commit must not rebuild a Tauri app). GitHub's per-branch-isolated cache
cannot do this, and keying reuse on the commit SHA busts on every commit. So an
**artifact** needs an *identity* — a hash of the inputs that actually determine it.
The whole correctness/reuse trade-off lives in defining that input set.

## Decision

- Every **artifact** is identified by a **content-key**: a hash of its determining
  inputs — toolchain identity, lockfiles, the artifact's **declared input globs'**
  contents, build profile, and any **bundle** inputs. The store is the durable
  GCS backend (the same one sccache already uses), **not** the GitHub branch cache.
- A pipeline's first step is **resolve-or-build**: a content-key hit downloads the
  prior build; a miss builds and uploads. Every consumer — a test **lane**, a
  package/sign stage, a **Release**, an **artifact-pinned** downstream — consumes by
  content-key, so the binary built in PR CI is reused at release time unchanged.
- **Safety on under-declaration is correctness-first:** an artifact that declares no
  input globs falls back to the whole-tree commit SHA (always rebuild). Under-
  declaring inputs therefore costs a *rebuild*, never a *stale ship*. Precise globs
  are opt-in where the payoff is large (the rust/Tauri builds); sccache absorbs the
  residual compile cost whenever a rebuild does fire.

### Alternatives rejected

- **Whole-tree commit SHA as the key** — trivial and always-correct, but rebuilds on
  every commit, defeating the cross-revision reuse that is the entire point.
- **Build-tool-derived dependency graph** (ask cargo what inputs matter) — most
  precise, but per-toolchain, complex, and not cheaply emitted; deferred, not
  foreclosed (an artifact could later compute its globs from the build tool).
- **Reuse keyed on inputs with a default of "trust the declaration"** — would let an
  under-declared artifact silently ship a stale binary; the safe fallback inverts
  this so the failure mode is a wasted rebuild.

## Consequences

- Artifacts declare input globs in `.shipit.toml`; the content-key spans the CI and
  release pipelines and (aspirationally) repos, which is what makes **artifact-
  pinned** cascade consumption a cross-repo build-once reuse.
- The "build once → stage → reuse" pattern already proven narrowly in the release
  repo (`share-debug-binary`, the OS-scoped WASM cache keyed on `Cargo.lock`)
  generalizes to one mechanism keyed on the content-key.
