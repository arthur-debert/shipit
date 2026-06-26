# WF02 — Content-addressed artifacts + build-once reuse

> Epic: **WF02** (theme WF — Workflows). Status: **planned**. Blocked by **WF01**.
> Spec source of truth. Glossary: `CONTEXT.md` (build/release). Decisions:
> `docs/adr/0008-content-addressed-artifact-identity.md`; rationale
> `docs/dev/{architecture,workflows}.lex`. Map: `docs/prd/FUTURE_WORK.md`.

## Problem Statement

CI re-does work it has already done. A Tauri build compiled on a PR is recompiled by
the e2e lane, by the next push, and again at release — and a docs-only commit
recompiles the whole binary because reuse, where it exists at all, is keyed on the
commit SHA or a per-OS lockfile cache that GitHub isolates per branch. The result is
the dominant cost in the fleet's CI: agents wait the majority of their time on
runners rebuilding bytes that did not change. There is no notion of *the same
artifact* across lanes, across workflows, or across git revisions.

## Solution

Every **artifact** gets a **content-key**: a hash of the inputs that actually
determine it (toolchain identity, lockfiles, the artifact's declared input-glob
contents, build profile, bundle inputs). A pipeline's first step is
**resolve-or-build** against a durable content-addressed store (the GCS backend
sccache already uses, *not* GitHub's branch-isolated cache): a key hit downloads the
prior build, a miss builds and uploads. Every downstream consumer — a test **lane**,
later a package/sign stage or a **Release** — consumes by content-key, so a binary is
built once and reused across lanes, workflows, and git revisions whenever its inputs
are unchanged. Under-declaration is made safe: an artifact that declares no inputs
falls back to the whole-tree commit SHA (always rebuild), so the failure mode is a
wasted rebuild, never a stale ship. sccache absorbs the residual compile cost when a
rebuild does fire.

## User Stories

1. As a maintainer, I want a docs-only commit to reuse the previously built binary,
   so that trivial changes do not pay a full Tauri compile.
2. As a maintainer, I want the e2e lane to consume the binary the build lane already
   produced, so that the same bytes are not compiled twice in one run.
3. As a maintainer, I want a PR's second push (that touches only unrelated paths) to
   reuse the first push's artifact, so that iteration is cheap.
4. As a maintainer, I want each artifact to declare the input globs that feed it, so
   that reuse is precise where the payoff is large (the rust/Tauri builds).
5. As a maintainer, I want an artifact with no declared inputs to always rebuild, so
   that forgetting to declare inputs can never ship a stale binary.
6. As a maintainer, I want the content-key to fold in toolchain identity, lockfiles,
   and build profile, so that a toolchain bump or a release-vs-debug profile is a
   different artifact and is never wrongly reused.
7. As a maintainer, I want the artifact store to be the durable GCS backend, not the
   GitHub branch cache, so that a fresh PR branch gets warm reuse instead of a cold
   rebuild.
8. As a maintainer, I want sccache wired for the compile itself, so that even a
   genuine rebuild on a cold branch is incremental rather than from scratch.
9. As a maintainer, I want `resolve-or-build` to be the first step of any lane that
   needs a built artifact, so that build-once is the default, not a special case.
10. As a release engineer, I want the binary built in PR CI to be the *same artifact*
    at release time, so that release does not recompile what CI already validated.
11. As a maintainer, I want to inspect why an artifact rebuilt (which input changed
    the content-key), so that I can tighten globs or accept the rebuild knowingly.
12. As a maintainer, I want a build to upload its artifact under its content-key on a
    miss, so that the next consumer anywhere gets a hit.
13. As a maintainer, I want WASM and other expensive intermediate outputs to reuse by
    content-key, so that the OS-scoped WASM cache generalizes into one mechanism.
14. As an agent, I want CI wall-clock to drop measurably once reuse lands, so that the
    "wait 70%" ratio improves.
15. As a maintainer, I want artifact-pinned cross-repo consumption (WF06) to be able
    to reuse an upstream's built artifact, so that the content-key is the same idea
    across repo lines (seeded here, used in WF06).

## Implementation Decisions

- **content-key engine (deep module).** Pure function `(toolchain identity +
  lockfiles + declared-glob contents + profile + bundle inputs) → content-key`.
  Deterministic, fixture-tree testable, no I/O. Declared-glob contents are hashed
  from the working tree; the absence of declared globs yields the whole-tree commit
  SHA (the safe always-rebuild fallback, ADR-0008).
- **artifact resolver (deep module).** Pure decision `(content-key + store
  presence) → {reuse(download-ref) | build-then-upload}`. The store I/O (a
  content-addressed get/put against GCS) is an injected boundary, mocked in tests —
  same pattern as `prstate` injecting its `Acts`/`ghapi` boundary.
- **Store boundary.** A thin content-addressed get/put over the existing GCS bucket
  (shared with sccache); keys are content-keys; values are the staged build outputs
  (binary + any co-staged payload, e.g. a `.app` reseal payload later). Not GitHub
  Actions cache.
- **resolve-or-build wiring.** The generic CI workflow (WF01) gains a first
  resolve-or-build step for any lane whose `consumes` artifact is declared; lanes
  then run against the resolved artifact. Build steps end by uploading under the
  content-key on a miss.
- **sccache.** Wired as the compiler cache for the build step (GCS backend), so a
  content-key miss still compiles incrementally — the cold-branch mitigation that
  complements artifact reuse.
- **Reuse spans pipelines.** Because the key is content-derived and the store is
  durable, the same content-key produced in PR CI is resolvable at release time and
  (aspirationally) across repos for artifact-pinned cascade consumption (WF06).
- **Generalizes existing narrow reuse.** The release repo's `share-debug-binary`
  artifact handoff and the WASM cache keyed on `Cargo.lock` collapse into this one
  content-key mechanism.

## Testing Decisions

- A good test asserts behavior: given a fixture tree + declarations, assert the
  computed content-key is stable, changes only when a determining input changes, and
  falls back to the commit SHA when no globs are declared; given a key + a faked
  store state, assert the resolver decides reuse vs build correctly. No real GCS, no
  real compile.
- **Unit-tested (pure cores):** the content-key engine (stability; sensitivity to
  toolchain/lockfile/glob/profile changes; the no-globs fallback) and the artifact
  resolver (hit→reuse, miss→build+upload, store error handling). Prior art: the
  `prstate` pure-decision tests with injected boundaries and fixture snapshots.
- **Property-style coverage:** changing an undeclared file must NOT change the key
  when globs are declared (reuse holds); changing a declared-glob file MUST change it
  (no stale reuse). These two invariants are the correctness core.
- **Not unit-tested:** real GCS get/put and real sccache behavior — integration,
  validated by the WF02 acceptance run (a docs-only commit reuses; an e2e lane
  consumes the build lane's artifact; measured wall-clock drop on a real repo).

## Out of Scope

- Build-tool-derived dependency graphs (asking cargo what inputs matter) — deferred,
  not foreclosed (ADR-0008); v1 uses declared globs + safe fallback.
- The cross-repo content-key store for artifact-pinned cascade — WF06 (the mechanism
  is seeded here; cross-repo resolution is later).
- Distribution, signing, changelog/release orchestration — WF04/WF05.
- Eviction/GC policy for the store beyond what the GCS bucket already does (a later
  operational concern, flagged in Further Notes).

## Further Notes

- Depends on WF01: it keys on the artifacts WF01's lanes declare and produce, and
  wires resolve-or-build into WF01's generic workflow.
- The biggest single lever on the fleet's CI cost; WF02's acceptance is explicitly a
  *measured* wall-clock improvement on a real repo, not just functional reuse.
- Store growth/eviction is intentionally minimal in v1 (lean on GCS lifecycle rules);
  if the store grows unbounded, a content-key TTL/GC is a follow-up, not a blocker.
