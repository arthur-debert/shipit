# Spec — Project Suburbia: fleet standardization

**Status:** grilled 2026-07-30 (decisions folded in below) · **Named for** the little boxes of American sprawl: the
Portfolio's repos should look near-identical wherever identical is possible.

This Spec is the program-level definition for finishing shipit's fleet adoption. It exists
to be the sanity check during execution: if work in flight stops serving these goals, the
work is off course, not the Spec.

## Context

shipit is the second pass at the portfolio-release problem and will replace the legacy
`arthur-debert/release` manager. Most of it is done and working: the agent/Tree/review/eval
core, fleet lint/provisioning/install, and CI check workflows across 20 of 22 Portfolio
repos. Release workflows are genuinely migrated for the rust and go repos; the remaining
tail is the lex-fmt one-offs (vscode, zed-lex), the GUI apps (lexed, simple-gal-ui,
phos-editor/app), and stragglers carrying legacy files (lex, phos-core, simple-gal's
deliberate windows leg).

Two events shape this Spec:

- The 2026-07-28 conda-direct fleet migration ran shipit's install/release/managed-content
  surfaces against real consumers at scale for the first time and surfaced ~35 defects in
  72 hours. Issue #1122 named the root cause: shipit cannot see what it ships — nothing
  between shipit's own repo and the live fleet exercises the shipped surfaces.
- The adoption bookkeeping (ADP02/#422 era) was closed as overtaken; its green-checkmark
  matrix no longer described reality. `docs/dependencies.md` proved the fix pattern: a
  terse doctrine page that declares "this file wins" and kills a confusion class outright.

Constraining prior decisions: ADR-0077/`conda-direct.md` (the dependency model — kept),
ADR-0040 (signing as a separate seam), ADR-0033 (each repo pins its shipit). The
CMD01/MOD01/SPL01 reorganization epics exist but act on code organization, which this Spec
judges is not the binding constraint; they stay parked.

## Problem

The binding constraint on finishing is not missing features. It is the long tail of
per-repo variation and historical accretion: each Portfolio repo is a unique snapshot of
whenever it was last touched, carrying workarounds whose justifying issue has closed
(16 of 22 repos, #1138), retired hooks still enforcing dead discipline (#1117), stranded
conventions from the legacy tooling that look identical to live ones, and fixes that
landed in 2 of the 6 repos that needed them.

This entropy has compounding costs:

- Adoption state is unknowable — status trackers drift from repo reality, so "adopted"
  means little without a fresh audit.
- Every newly exercised surface detonates fleet-wide instead of failing in one
  representative place; a canary cannot be representative of entropy.
- The iteration loop is unsustainable: shipit change → review → release → pin bump →
  consumer error → repeat.
- The instruments that certify success lie: the PR state engine has reported Ready with a
  required reviewer never requested (#1111), install has reported gitignored writes as
  delivered (#1139) and silently rewritten a Shipit pin backwards (#1184).

Underneath sits one costly founding assumption: shipit was designed to run hermetically on
the owner's macOS dev box, and a large share of its complexity (provisioning, activation,
the 700-line lint orchestration that is conceptually a lefthook config) is the tax of that
assumption. Isolated execution environments were not planned for; agentic development made
them the norm.

## Goals

1. **Legacy release scrapped fleet-wide.** Every Portfolio repo releases through shipit;
   no repo carries legacy `release.yml` machinery. This is the payoff that ends the
   two-system straddle.
2. **Prescribed profiles, converged fleet.** shipit prescribes a canonical layout,
   task set, and configuration per stack; repos converge to their profile. Variation
   exists only in a legitimate-variation registry where each entry is named, owned, and
   justified. Reconcile reports divergence from profile, so drift is visible by
   mechanism, not by audit.
3. **Trustworthy instruments.** Engine verdicts (Ready, delivered, reconciled) are
   load-bearing again: when shipit says a thing happened, it happened.
4. **A representative canary.** shipit-canary (and later one instance per profile) is a
   real consumer running shipit-managed install/lint/release, catching ship-class defects
   before a release reaches the fleet.
5. **The substrate decision made and recorded.** The execution substrate becomes an
   isolated environment (container by default) with code mounted from the host; macOS is
   the scoped Mac exception. Decided on paper with one spike; profiles are written against
   the target so the fleet is stamped once.
6. **Doctrine pages that kill recurring bad information** — short standalone
   "this file wins" docs on the topics agents repeatedly get wrong.

## Non-Goals

- **Executing a fleet-wide substrate replatform inside this program.** The substrate is
  decided (ADR + one spike); migration happens with/after convergence per that ADR's
  direction, not as a prerequisite gate for the adoption tail.
- **Windows support execution.** Doctrine only (see below); shipit code stays portable so
  the door stays open.
- **Distribution beyond the spine.** Brew, Debian packages, app/windows stores, snap:
  out of scope for this push. GH Releases + tags + changelog + crates.io/npm publishing
  are the spine and are in scope.
- **The modular lint architecture** (baseline + stack composition). Kitchen-sink linting
  for everything is accepted for now; the 700-line orchestration should shrink toward a
  deployed config, not grow toward the modular design.
- **CMD01/MOD01/SPL01** (code reorganization) stay parked until legacy release is
  scrapped. With the substrate decision pending, reorganizing today's code is doubly
  premature.
- **phos-editor/app's exotica** (GPU-dependent e2e, its heaviest testing needs) is
  sequenced last and its specifics are not designed here.
- **Modeling current fleet variation in the canary.** The canary models profiles, never
  the entropy; standardize first, then model the standard.

## Proposed Shape

Six phases. 0–2 are short and partially parallel; 3–5 are the body of the program.

**Phase 0 — Trust kernel.** Fix the instruments the rest of the program stands on:
the shipit-canary skeleton as a real consumer (#1122), Ready-with-unreviewed-head
(#1111), delivered-but-gitignored (#1139), backwards pin rewrite (#1184), splice
self-verification (#1137), silent skip on key collision (#1116). The canary skeleton is
deliberately shape-agnostic: a minimal consumer (a pin, managed blocks, a gitignore) is
sufficient to catch engine-honesty defects, which do not depend on repo shape.
Representative shape coverage is Phase 5's per-profile canary — it cannot exist before
Phase 3's profiles and is not attempted here. Bounded list; not a general bug-clearing
campaign — the remaining open bugs are triaged against the phases they belong to, and
many managed-content defects die incidentally in convergence.

**Phase 1 — Doctrine pages.** Mint the standalone docs (the `dependencies.md` pattern),
largely by transcription from settled decisions:

- *Windows*: historically some projects shipped it; it returns as mandatory for GUI apps
  and editor extensions eventually; rust libs/CLIs forgo it for now; shipit code stays
  os-portable (paths via `os.path`, no unix-slash assumptions) so adding it later is not
  a refactor.
- *Publishing spine*: releases are CI-driven and workflow-triggered (never tag-triggered —
  the release itself creates commits), runnable locally for development, produce GH
  Releases matching tags with the compiled changelog and standardized platform/arch
  artifact naming; stack-manager publishing (crates.io, npm) is a done deal; distribution
  beyond the spine is out of scope.
- *Safety/breakage license*: all projects are internal or small-friendly-userbase;
  layouts and commands may break; patch releases are free; only externally immutable
  surfaces (crates.io versions, extension registries) need care.
- *Changelog*: `CHANGELOG/` fragments compiled at release — already the standard, declared
  so it stops being re-derived.
- *Standard task names and structure* (lint, test, release, build, …), *standard test
  outputs* (one machine-readable format, e.g. JUnit XML, consumed uniformly by
  workflows), *docs dirs* (`docs/dev`, `docs/adr`, `docs/spec`), *secrets* (standardized
  env var names, stored/fetched via doppler, same names locally and in CI), *GH repo
  rulesets* (standardized — ruleset drift creates havoc in the PR/review/branch flow).

**Phase 2 — Substrate (decided; spike pending).** Crystallized in ADR-0084 and
ADR-0085: code lives on the host filesystem (owner and agents read/write it there);
execution happens inside a container Substrate whose layered images own the toolchain —
one fleet baseline layer (rust, python/uv, node, linters) plus sanctioned heavy layers
selected from the menu. shipit owns the Dockerfiles; images publish via shipit's own
release, and a repo's image reference is derived from its Shipit pin — one pin axis,
never typed. The Mac exception is the sole carve-out (Darwin build/bundle release
legs; CI signing/notarization; locally launching the GUI apps), licensed to be crude —
pixi may survive solely to serve it. The Artifact channel's transport moves to GH Release assets with conda-direct's
invariants preserved verbatim, under a standing guard: no index, no resolution, no
transitivity. Remaining Phase 2 work is the spike proving dev-loop ergonomics (a rust
repo's edit-on-host, lint/test/build-in-container loop end-to-end). The decision must
not become a replatform-before-adoption gate: the convergence sweeps deliver it;
nothing fleet-wide waits on it.

**Phase 3 — Profiles.** Two prescription modes:

- *Top-down* for commodity stacks — rust (crates/workspace layout), python, go (supage
  only), electron. shipit knows better than any individual repo what a standard instance
  looks like; write the profile from shipit's needs and let each repo's diff from it fall
  out mechanically.
- *Two-way for lex-fmt* — everything except lex-fmt/lex (a vanilla rust workspace) is
  one distributed system, not seven quirky repos: lex produces lexd/lexd-lsp,
  tree-sitter-lex produces the grammar, comms rides as a shared submodule, and
  nvim/vscode/zed/lexed are four packaging regimes consuming the same artifacts. A
  read-only working-model investigation extracts the system as built (artifact flows,
  packaging constraints, what comms is, which deviations are load-bearing), and the
  profile is prescribed from that. Expected collapse: one artifact-consumer/extension
  profile with a packaging-regime parameter, not one profile per repo. For nvim/vscode/
  zed, third-party platforms cap how much can be standardized; prescribe up to that cap.

The profile's substance is defined by a dedicated **component-model spec** — the next
planning artifact after this one. Its shape, settled here: a repo is a composition of
**Components** (a toolchain + a dir layout at a mount point + the Tool implementations
it brings) under one fixed **Tool contract** (the Tools lint/test/build/release with a
shared setup/execution/result shape; every `test` implementation deposits JUnit XML at
the standard path — human-facing formats like TAP are presentation overrides, never a
second machine contract). A repo **master task** composes Component implementations of a
Tool into the repo outcome; single-component repos are passthrough. The fleet fine-comb (2026-07-30, all
22 repos read on origin/main) confirmed the premise: six component kinds plus a
docs-site component (mkdocs — currently mid-migration, itself a convergence target) and
one static-site case cover the entire fleet; the task layer is already uniform one-line
dispatch into shipit; the e2e menu is four shapes; the true one-off list fits on one
hand.

Governance is two-tier — this replaces any enforce-versus-advise spectrum. Shipit-owned
operations (provision, lint, build, release, CI plumbing) are strictly enforced and
identical fleet-wide. Project specifics enter only through shipit-defined **extension
points**: declared interfaces (e.g. a fetch-deps seam) where a project contributes an
implementation — a shell script at the low end, a registered plugin at the high end —
always invoked inside shipit's contract, never as a bypass. Anything neither owned nor
contributed through an interface is a violation, mechanically detectable. Contributions
that grow past a threshold graduate into sanctioned menu items or die (the graduation
rule every surveyed build ecosystem converged on independently).

Layout standardization doctrine: shoot for standard paths everywhere; where a config can
point to the standard path, change the config; where a third-party tool hardcodes a path
(e.g. tauri's `src-tauri` — spike whether it is movable), keep the standard path and test
symlinks (the `.agents/skills` → `.claude/skills` precedent), accepting that symlinks are
a tactic to verify per packaging regime, not a magic bullet (#1114, #1143). Known layout
deviants are convergence work, not registry entries: phos-core reshapes into `crates/`,
standout's nested workspace flattens, and `src-tauri` gets a renameability spike. Every
*surviving* deviation is a registry entry with a name, an owner, and a reason; reconcile
reports divergence from profile as drift.

**Phase 4 — Convergence sweeps (= the adoption push).** Migrating a repo and
standardizing it are the same act. Order: the fleet reconcile pass that confirms the
provision-lexd self-heal and unblocks updates; changelog fragments where releases are
blocked on empties; rust sweep first (nearly standard already — cheap validation of the
model); then lex-fmt through its profile (vscode → lexed → zed-lex); simple-gal-ui via
the binary-optional e2e redesign (#985); phos-editor/app last (first Tauri
sign/notarize fire — the program's biggest unknown, deliberately fed by everything
learned before it). comms enters the Portfolio and converts from git submodule to a
released tarball consumed via `shipit stage` (ADR-0085). GUI unknowns are solved once
into the profile, never per-repo.

**Phase 5 — Canary per profile, generated.** As each profile stabilizes, the canary
grows one instance of it — *generated* from the creation profile (`shipit repo new`
output plus the real managed workflows), never hand-maintained; first instances: rust,
then npm-electron. Fidelity holds by construction: the canary is the profile
materialized, regeneration is free, and every regeneration doubles as an end-to-end
test of repo creation itself — "a generated repo has working shipit templates" becomes
a fact maintained by the design, not another thing kept in sync by hand. Because the
fleet converges *to* the profiles, canary fidelity stops decaying.

**Operating rules** (the asymmetry fix): fleet-shaping work is driven from shipit's
context — agents briefed with the program vision execute consumer-side edits; consumer
repos retain near-zero shipit-relevant freedom (shipit controls both ends and prescribes
accordingly); prescribe first, then burn down diffs — never census the entropy.

## User / Agent Stories

1. As the owner, I want every repo releasing through shipit, so that I develop against
   one system's design ideas instead of straddling two and working around legacy bugs.
2. As the owner, I want to cut a vulnerability patch release from any machine via a
   CI workflow trigger, so that being away from my dev box never blocks a fix.
3. As an agent working in a consumer repo, I want doctrine pages that win over stale
   folklore, so that I stop resurrecting dead facts (windows support, retired
   conventions) with per-repo consequences.
4. As an agent migrating a repo, I want a profile to converge to and a registry of
   legitimate deviations, so that "adopted" is a diff I can compute, not a judgment call.
5. As the shipit maintainer (human or agent), I want the canary to fail before a release
   ships, so that defect discovery costs one CI run instead of a fleet-wide incident.
6. As a coordinator, I want reconcile to report drift from profile, so that workarounds
   die when their justification does instead of outliving it silently.
7. As the owner, I want release workflows runnable locally, so that developing them does
   not mean push-and-pray round trips through GitHub Actions.
8. As an agent debugging a consumer failure, I want the execution substrate to be the
   same isolated environment everywhere, so that "works in CI, breaks on the host" (and
   vice versa) stops being a defect class.

## Risks And Rabbit Holes

- **The eternal almost-there.** The failure mode this Spec exists to prevent. Its known
  forms here: the substrate decision ballooning into a replatform gate; the trust kernel
  growing into a clear-all-41-bugs campaign; CMD01/MOD01 starting "since we're pausing
  anyway"; canary work modeling fleet entropy. Each phase has a bounded exit.
- **Censusing the entropy.** Cataloguing 22 repos' every variation is a census of
  accidents. The only question needing investigation is which variations are
  load-bearing; everything else is a diff to delete. lex-fmt is the one place the
  investigation is genuinely required first.
- **Symlinks as magic bullet.** Already broke `vsce` packaging once. Every symlink move
  gets verified in the affected packaging regime before it enters a profile.
- **Prescribing lex-fmt blind.** Its working system embodies undocumented decisions;
  top-down prescription there relocates whack-a-mole into the profile spec.
- **Immutable external surfaces.** crates.io versions, published extension versions, and
  similar cannot be deleted; convergence there must be forward-only.
- **The Mac exception creeping.** The exception is licensed to be crude precisely because
  it is small; if it starts accumulating general machinery, the substrate decision is
  being silently reversed.

## Cross-Cutting Concerns

- **Secrets**: standardized env var names across local and CI, stored and fetched via
  doppler; no per-repo naming invention.
- **Observability**: dev-cycle events and JSONL logging (ADR-0029/0032) apply to
  convergence sweeps like any other work; sweep progress is derivable from logs, not
  hand-maintained matrices (the ADP02 lesson).
- **CI/release**: the publishing-spine doctrine governs all release workflow work in the
  sweeps; GH rulesets standardize as doctrine because ruleset drift breaks the PR engine's
  assumptions.
- **Migration/compatibility**: the safety doctrine licenses breakage; no
  backwards-compatibility shims in consumer repos during convergence.
- **Portability**: shipit code remains os-portable throughout, keeping the windows door
  open without paying for it now.
- **Performance of the loop**: the program's own iteration speed is a first-class
  concern — trust kernel and canary come first because they shorten every subsequent
  cycle.

## Testing / Verification

- **Trust kernel**: each fix lands with a regression test; the canary skeleton is itself
  the verification vehicle for the install/delivery fixes.
- **Substrate spike**: one rust repo's dev loop (edit on host, execute in container)
  demonstrated end-to-end, including lint/test/build parity with today's results.
- **Convergence sweeps**: per-repo verification is a reconcile run reporting zero drift
  plus green checks; per-stack verification is one real release fired from a converged
  repo (run-the-real-feature: a release counts when real artifacts ship).
- **Live-fire proofs**: first Electron release through shipit (simple-gal-ui), first
  signed+notarized Tauri release (phos-editor/app) — these are acceptance evidence for
  the GUI profiles, not optional extras.
- **Canary instances**: each profile instance runs the full shipit-managed surface
  (install, lint, release) in CI; a canary that cannot fail a bad shipit release does not
  count as an instance.

## Workstream Hints

Phases map naturally to epics: trust kernel (one epic, the bounded bug list); doctrine
pages (small, mostly transcription); substrate ADR + spike; lex-fmt working-model
investigation (read-only, output is a document); one profile+convergence epic per stack;
canary growth rides along with each profile's completion. Phases 0–2 can run
concurrently; 3 depends on 1–2; 4 depends on 3 per-stack (rust can start while lex-fmt is
still under investigation); 5 trails 3/4 per profile.

## Out Of Scope

Windows execution · distribution channels beyond the spine (brew, deb, stores, snap) ·
CMD01/MOD01/SPL01 code reorganization · the modular lint architecture ·
phos-editor/app's GPU/e2e specifics (sequenced last, designed then) · canary modeling of
non-profile variation · any backwards-compatibility machinery in consumer repos.

## Further Notes

- This Spec supersedes the ADP02-era adoption matrices as a statement of intent; issue
  #1100 remains the live migration tracker until Phase 4's sweeps absorb it.
- Grill outcomes (2026-07-30): the substrate and transport crystallized in
  [ADR-0084](../adr/0084-container-substrate-image-owned-toolchains.md) (container
  Substrate, image-owned toolchains, layered images, one derived pin, Mac exception)
  and [ADR-0085](../adr/0085-artifact-transport-gh-release-assets.md) (GH Release
  asset transport, the no-index/no-resolution/no-transitivity guard, comms as first
  user). The component model gets its own spec before its ADRs — define what to build,
  then the dos and don'ts. The doctrine page set (Phase 1) remains to be minted.
- Resolved from the open list: comms joins the Portfolio (tarball artifact); canary
  instances are generated per profile; the test-output machine contract is JUnit XML.
  Still open: `src-tauri` renameability (spike scheduled with Phase 3 layout work).
