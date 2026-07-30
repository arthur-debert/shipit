# Spec — The component model: Components, Artifacts, Endpoints

**Status:** grilled 2026-07-30 (decisions folded in) · **Part of** [Suburbia](suburbia.md)
Phase 3 · **Builds on** ADR-0007 (path→toolchain map), ADR-0039 (Tools as verbs),
ADR-0084 (container Substrate), ADR-0085 (GH Release asset transport).

This Spec defines what a repo *is made of* in shipit's eyes. It is the substance behind
Suburbia's Profiles: a Profile composes the units defined here.

## Context

The fleet fine-comb (2026-07-30, all 22 Portfolio repos on origin/main) established that
the fleet is not 22 bespoke setups: six-ish component kinds plus a docs-site and a
static-site case cover everything; the task layer is already uniform one-line dispatch
into shipit; the e2e surface is four harness shapes; the true one-off list fits on one
hand. A six-group prior-art survey (Bazel/Buck2, Gradle, Nx/Turborepo/moonrepo/
Pants, Buildpacks/devcontainers, task-runners/Nix flakes, Cargo/npm/Maven) supplied the
patterns worth stealing and the failure modes to avoid — chiefly: a small **fixed verb
vocabulary with a typed result shape** buys generic tooling (Nix flakes); a central
(verb × kind) → implementation registry with a low-ceremony, *inside-the-contract*
escape valve is how every registered-implementation system survives one-offs (Pants,
CNB); and a closed, ordered lifecycle with implicit chaining is the documented disaster
(Maven).

What exists today and is kept: the `.shipit.toml` path→toolchain map (promoted, not
replaced), the Tool verbs and their drivers, `[artifacts]` bundle declarations,
Distribution endpoints with endpoint adapters (ADR-0007; the build/sign/publish
barrier is ADR-0009), managed pixi task indirections.

## Problem

The model these mechanisms imply was never written down, so every new consumer shape
was negotiated ad hoc: packaging regimes leaked into per-repo scripts (electron
`build:release` self-provisioning), odd toolchains got bespoke config keys
(tree-sitter's `build = [script]`), e2e setups were designed per repo, and cross-repo
dependencies were restated on both sides until they drifted. The absence of a declared
model is what made "adopted" unknowable and standardization unenforceable — Suburbia's
central diagnosis.

## Goals

1. **One declared model**: a repo is a composition of Components; Artifacts are built
   from them; Endpoints distribute them. All three declared in `.shipit.toml`,
   declarations are the source of truth, reconcile enforces against them.
2. **Runtime-driven verification**: every executing Artifact declares its runtime, and
   the runtime determines its e2e harness. Nobody designs a test setup per repo.
3. **Decoupled subsystems**: shipit's own architecture mirrors the discipline it
   imposes — the build orchestrator, the per-kind builders, changelog, and
   distribution are standalone subsystems sharing data types, never code, coupled
   only by process contracts and declared inputs/outputs on disk.
4. **Declaration-level agreement**: producer/consumer artifact declarations are
   symmetric and fleet-checkable, killing the restatement-drift and platform-mismatch
   defect classes (#1126, #1141) statically.
5. **Kinds, not hooks**: new needs are sanctioned as component kinds or menu options in
   shipit's owned surface; the extension mechanism exists but ships with zero uses.

## Non-Goals

- **No build graph, no caching, no inference** (Nx/Turbo territory): ordering is
  explicit-or-derived, execution is sequential in v1. Parallelism is enabled by the
  architecture but not built.
- **No lifecycle phases** (Maven): invoking one Tool never implicitly runs another.
- **No autodetection as authority**: probes only cross-check declarations.
- **No seams in lint, test execution/reporting, release orchestration, or endpoint
  publishing** — owned surface, sealed.
- **No cross-repo auto-bump** (Cascade stays dead; bots bump versions).

## Proposed Shape

### The three buckets, plus runtime

- **Component** — a toolchain-bearing unit: code at a declared mount point, bringing
  the Tool implementations for its kind. v1 kinds (from the fine-comb):
  `rust` (workspace at `crates/`), `npm`, `python`, `go`, `lua`, `grammar`
  (tree-sitter; sunsets the legacy build-script config), `docs-site` (mkdocs),
  `static-site`. Adding a kind is a sanctioned change inside shipit, small and
  reviewed — the answer to odd toolchains is a kind, not a hook.
- **Artifact** — a built deliverable, bound to a component, bundled by a packaging
  kind (archive/tarball, vsix, electron, tauri-bundle, zed-ext, wasm-pack, service
  image). An artifact that executes declares a **runtime**:
  `native-cli | browser | electron | tauri | nvim-host | vscode-host | zed-host |
  service | static-web | data`. Libraries/registry packages have **no runtime** — they
  are consumed at build time and verified by unit tests alone.
- **Distribution endpoint** — where artifacts ship: the GH Release spine (also the
  artifact-dep transport, ADR-0085) plus crates.io, npm, pypi, brew, deb, stores,
  firebase — each an adapter owning its auth (doppler via `[secrets]`). Orthogonal to
  runtime: one tauri app may ship via brew, an app store, and a windows store without
  touching its component, artifact, or harness. Deferring a distribution = deferring
  an adapter.

### Declarations (illustrative shapes)

Producer:

```toml
[components.core]
kind  = "rust"
mount = "crates"

[artifacts.lexd]              # key derives as <owner>/<repo>/lexd (ADR-0085)
component = "core"
runtime   = "native-cli"
platforms = ["linux-x86_64", "darwin-arm64"]
path      = "target/release/lexd"          # promised output; platform-keyed

[artifacts.lex-wasm]
component = "core"
runtime   = "browser"
universal = true                            # XOR with platforms; satisfies any consumer
path      = "crates/lex-wasm/pkg"
```

An artifact's `path` is its **promised output** — where its bytes land after the
owning component builds (platform-keyed when platform-dependent). These declared
paths are what the orchestrator verifies. `universal` and `platforms` are mutually
exclusive; a universal artifact satisfies every consumer platform.

Consumer:

```toml
[artifact-deps."lex-fmt/lex/lexd-lsp"]
version = "0.19.10"                 # WHAT and WHICH version — repo-level pin

[components.app]
kind = "npm"
mount = "."
requires = { "lex-fmt/lex/lexd-lsp" = "app-bin/" }   # WHERE — placement
```

`dest` lives in exactly one place: the `requires` mapping of whatever consumes the
input — `[artifact-deps]` pins what and which version, `requires` says where. The
same rule covers both sources (intra- and inter-repo keys), and the same mapping is
available on an artifact for bundle-time inputs (staged app resources).

Composition (multi-component; ordering usually *derived*):

```toml
[components.wasm]
kind = "rust"
mount = "crates"

[components.ui]
kind = "npm"
mount = "."
# requires maps a key to the dest where the input is placed before this
# component builds; an intra-repo key also derives the ordering edge.
requires = { "phos-editor/core/phos-wasm" = "public/wasm/" }
# build.after = ["wasm"]                    # manual edge only when no artifact mediates
```

**Version binds identity; sha verifies transport.** The consumer pins only the version;
at fetch time the GH asset digest is the integrity checksum for that version's bytes.
In-place asset replacement is prohibited except bug fixes, where the new bytes are what
every consumer wants. This is a deliberate owner decision with the threat model stated:
a single-owner portfolio where producer and consumer are one trust domain — a
compromised account publishes infected *new* releases that consumers auto-update into,
so per-artifact tamper-evidence buys nothing here; it would be too loose for
multi-party or enterprise infrastructure. Consequence accepted: same-version bytes may
legitimately change, so byte-reproducibility over time is not guaranteed — the build's
pure-function property is about build *logic*, not about refetches being
byte-identical forever. No lock sidecar exists. This amends ADR-0085's "consumer
declares the sha of each fetched file" clause, and supersedes ADR-0077's prohibition
of a `version` key in `[artifact-deps]` — that prohibition was an artifact of the
conda realization, where the version had to live in `[dependencies]` for pixi to
resolve; under the GH-asset transport, `[artifact-deps]` *is* the one consumer-owned
place (`docs/dependencies.md` carries the same update).

### The build subsystem: arch-bound, contract-coupled

**A build — one component or the whole orchestrated repo — is a pure function of
(repo state, target platform) → outputs, and always binds exactly one target**
(`linux-x86_64`, `darwin-arm64`, …, or `universal`). The build subsystem has no
concept of a platform matrix; matrices are release orchestration (next section).
This single property is what lets the same code run unmodified for local dev on
macOS, in CI workflows, and inside the container — the invocation context never
matters to build logic.

The orchestrator iterates components in order (derived from `requires`, plus manual
`build.after` edges), and for each one:

1. **Resolve inputs**: every `requires` key is satisfied intra-repo (an artifact
   produced earlier in this run, copied from its promised `path`) or inter-repo (a
   staged fetch of the `[artifact-deps]`-pinned version) — same key syntax, same
   resolution, two sources, one placement rule: the `requires` mapping's dest.
   Inputs are in place before the builder runs. The resolver — not the builders —
   owns the read credential for private inter-repo fetches.
2. **Invoke the builder agnostically**: a per-kind program run at the component's
   mount, passed the target platform explicitly — the builder never infers it from
   the host. The contract is a process boundary — exit 0 is success, non-0 is
   failure — and nothing else. Orchestrator and builders share **data types
   (Component, Build, Artifact), never code**. This decoupling is paramount and
   must be stretched as far as it will go.
3. **Verify promised outputs**: the declared output paths must exist. A builder that
   lies fails on the spot — the build-level twin of the trust kernel's
   "delivered means delivered".
4. Move to the next component.

`shipit build` (bare) builds all components for the **current platform**, no
bundling — the fast dev loop. `shipit build <artifact>` / `--all` additionally runs
the named/all artifacts' bundlers. Output paths are platform-keyed where the
artifact is platform-dependent. Because every artifact binds to a component and
every kind has a builder, `build --all` is total by construction.

### Release: orchestration of standalone subsystems

The arch-bound build is what makes the case for a separate release system. Release
does not *do* the work — it orchestrates four subsystems that do, each standalone
under the same rule as the builders (shared data types, never code):

- **Build** (above): invoked once per leg with that leg's target, the matrix
  derived by release from the artifacts' declared `platforms`.
- **Changelog**: fragment compilation and version accounting — the existing
  `CHANGELOG/` machinery, formalized as its own subsystem.
- **Signing**: the separate post-build stage (ADR-0040's seam): bundlers emit
  signable outputs and never self-sign; Darwin signing/notarization legs ride the
  Mac exception (ADR-0084).
- **Distribution**: the endpoint adapters publishing the aggregated outputs, with
  completeness — every declared platform present, built *and signed* — checked at
  the publish barrier (ADR-0009).

This maps directly onto the existing per-stage dispatch (ADR-0054): prepare =
changelog, build legs = arch-bound builds, sign = signing, publish = distribution.
The interface between release and build is the same builder contract — there is no
second protocol, and release logic never leaks into builders nor vice versa.

**Terminology guard**: "Component" is reserved for consumer-repo units. shipit's
own internal units — build, changelog, distribution, the orchestrator — are
**subsystems**, never components.

### Tool contracts and the harness matrix

Each Tool has one fleet-wide contract: shared setup, invocation, machine-readable
result shape (test deposits JUnit XML at the standard path; human formats are
presentation overrides). The e2e harness is a function of the artifact's runtime:

| runtime | harness | lifecycle |
| --- | --- | --- |
| native-cli | bats + released binary | synchronous |
| browser | playwright (browser) | synchronous |
| electron | playwright-electron (+xvfb or macOS runner) | synchronous |
| tauri | tauri-driver | synchronous |
| *-host | plugin smoke (per host) | synchronous |
| service | suite harness + backing service (e.g. Firestore emulator) | **service** (start once per suite) |
| static-web | none — build-output verification only | — |
| data | none — staged fetch + unpack verification | — |

**Verification is total across artifact classes**: execution runtimes get the
harness above; `static-web` and `data` are verified by the orchestrator's
output-verification and the staging path respectively; libraries/registry packages
are verified by unit tests plus the endpoint adapter's publish-time validation
(dry-run/install-smoke where the endpoint supports it). Runners (GPU, macOS-arm64)
are orthogonal menu options. The `lifecycle` attribute makes supage's emulator a
modeled shape, not a one-off. (To verify while implementing: all non-service
harnesses are genuinely synchronous.)

### Menu and extension mechanism

The Menu v1: the harness shapes above, runners (gpu, macos-arm64), and heavy image
layers (ADR-0084). Options carry a small schema (enum/default, devcontainer-features
style). The extension mechanism — a project-contributed implementation behind a
shipit-defined interface, declared in `.shipit.toml`, invoked inside the contract —
is **defined but ships with zero uses**: everything real dissolved into kinds,
declared inputs, or menu attributes. The graduation rule binds from the first future
use: a contribution that grows becomes a menu item or dies.

### Validation (reconcile + fleet checks)

Intra-repo: every artifact names an existing component; ordering edges acyclic; dests
inside the repo; declared mounts have manifests (and probes flag undeclared ones —
detection as advisory cross-check only). Fleet-level (shipit owns the Portfolio):
every consumed key resolves to a declared producer artifact at an existing version;
consumer platform needs ⊆ producer `platforms`, with `universal` satisfying every
consumer platform (`universal` and `platforms` mutually exclusive — declaring both
is invalid); orphaned artifacts and dangling consumers reported. The declarations are the agreement; the fetch verifies bytes.

## User / Agent Stories

1. As an agent onboarding a repo, I read its `.shipit.toml` and know its components,
   artifacts, runtimes, and deps without reading a single script.
2. As the owner, I add a browser build to an electron app by adding one artifact row —
   the harness follows from the runtime; the component is untouched.
3. As the orchestrator, I run any builder without importing its code, and I fail the
   build the moment a promised output is missing.
4. As a producer, I rename or drop an artifact and the fleet check tells me every
   consumer that breaks — before anything releases.
5. As a shipit developer, I unit-test the orchestrator with fake builders (scripts
   that exit 0 and drop files) and conformance-test every kind against the contract.
6. As the release engine, I schedule per-platform legs only for artifacts whose
   declared platforms include that leg — the #1126 class is unrepresentable.

## Risks And Rabbit Holes

- **Rebuilding a build system.** The guard: ordering-only composition, sequential v1,
  no caching, no inference. The moment someone proposes input-hashing, stop.
- **Kind proliferation.** A kind requires a real toolchain, not a variant; variants
  are options on an existing kind. The fine-comb's inventory (8 kinds) is the
  budget; exceeding it needs a grill.
- **Contract erosion via convenience coupling** — an orchestrator "just importing"
  one builder helper. The no-code-coupling rule is absolute; it is what keeps the
  system testable and parallelizable.
- **Bundlers absorbing component logic** (the old electron `build:release` shape
  re-forming inside a bundler). Bundlers package declared inputs; they do not build.
- **The empty extension mechanism inviting premature generality.** It ships as an
  interface definition only; building plumbing for hypothetical contributions is the
  overbuild this Spec exists to prevent.

## Cross-Cutting Concerns

- **Substrate**: builders run inside the container (ADR-0084); Darwin bundle legs ride
  the Mac exception; the builder contract is substrate-agnostic by construction.
- **Secrets**: split by direction. *Read*: the orchestrator's input resolver owns the
  portfolio read credential for private inter-repo fetches (via `[secrets]`/doppler,
  locally and in CI); public fetches are authless. *Write*: endpoint adapters own
  their publish credentials. Builders never receive credentials of either kind.
- **Observability**: orchestrator steps (resolve/invoke/verify) are logged as normal
  structured file-log records with lifecycle narration — not dev-cycle events, whose
  registered vocabulary stays reserved for session/PR/epic milestones (ADR-0032). A
  failed output-verification names the component and the missing path.
- **Migration**: existing `[toolchains]` maps promote mechanically to `[components]`;
  the tree-sitter build-script config and electron self-provisioning sunset during
  each repo's convergence sweep. ADR-0085's sha clause is amended by this Spec.

## Testing / Verification

- Orchestrator unit suite against fake builders: ordering (derived + manual), input
  resolution (intra/inter), output verification failure, `build --all` totality.
- Per-kind conformance suite: every builder satisfies the process contract on a
  fixture repo of its kind.
- Fleet-check tests over fixture portfolios: unresolved keys, platform mismatch,
  orphans.
- The generated Canary instances (Suburbia Phase 5) are the end-to-end proof: each
  profile's canary declares components/artifacts and must build, verify, and release
  through the real pipeline.

## Workstream Hints

Natural seams: (1) data types + declaration parsing/validation; (2) orchestrator
(loop, resolution, verification); (3) kind builders (migrate existing toolchain
drivers to the process contract); (4) artifact bundlers; (5) ADR-0085 stage/fetch
transport; (6) fleet checks; (7) harness matrix + lifecycle attribute. (1)→(2) is the
tracer bullet with fake builders; (3) can proceed per-kind in parallel after (1).

## Out Of Scope

Distribution adapters beyond spine + crates.io/npm/pypi · windows execution ·
parallel build execution (enabled, not built) · the Profile/Creation-profile fold
(documented follow-up) · caching/incrementality of any kind.

## Further Notes

- Decisions here were grilled 2026-07-30 in-session; the durable ones that meet the
  ADR bar (builder process contract / no code coupling; version-binds—sha-verifies
  and the mutation doctrine; runtime-on-artifact) should be minted as ADRs with the
  implementing epic, linking back here.
- Amends ADR-0085's consumer-sha clause and supersedes ADR-0077's `[artifact-deps]`
  version-key prohibition (see Declarations); `docs/dependencies.md` carries the
  matching update.
- ADR-0008's content-key (build-once reuse) is dormant: absent from the current
  pipeline's code, and caching is out of scope here. Reintroducing it is a
  deliberate future decision, not an implied obligation — ADR-0009's barrier
  semantics stand independently of it.
- Verify during implementation: all non-service harnesses are synchronous (supage is
  the only service-lifecycle case).
- Evidence and prior art: the 2026-07-30 fleet fine-comb and the six-group survey
  (session artifacts; survey raw findings preserved in the session transcript).
