# The Delivery System — target design

**This is the target design (the aspiration). This file wins for design questions.**
It covers the whole non-agentic half of shipit — tasks, components, build, packaging,
signing, release, distribution, consumption, provisioning — including the interfaces,
the shared data model, and the third-party tools adopted for leaf implementation.

It deliberately ignores implementation: no reuse-of-current-code decisions, no
migration ordering, no incremental strategy. Those are worked separately, against this
document. The reasoning: the scars of shipit-v1 and legacy-release trace to designing
mid-implementation — special cases and works-sometimes workarounds accreting where
pre-work, validation, and design were missing. A design born wrong only degrades from
there; this document exists to be born right, then stay fixed while implementation
iterates toward it.

**Two validators keep it honest:**

1. **Deliverability spikes** — every load-bearing third-party claim is verified
   hands-on or from primary sources before the design depends on it (§ Spikes).
2. **The paper-fit test** — every fleet repo (the 22 Portfolio members plus comms,
   joining — 23 rows) must map onto this tree with zero special cases (checked
   against the 2026-07-30 fleet fine-comb). A repo that cannot be expressed here
   falsifies the design, not the repo.

A subsystem section is **done** when it has: purpose, owned responsibilities, its
contract, its data types, its adopted tools, and an empty open-questions list.

Status of each decision: **[settled]** (spec/ADR merged), **[design]** (decided here,
spec amendment to follow), **[pending-spike]** (blocked on a named spike).

## The tree

```text
Delivery System
├── Substrate                      container default, layered images, Mac exception
├── Consumer surface
│   ├── Task veneer                managed Taskfile: 4 verbs → shipit; local include
│   └── Tools                      lint / test / build / release verb contracts
├── Component system
│   ├── Components                 kind + mount + Tool implementations
│   ├── Builder contract           arch-bound process boundary
│   └── Build orchestrator         resolve → invoke → verify, sequential
├── Artifacts                      named build products, promised output paths
├── Packaging                      artifact → N packagings (formats); bundlers
├── Signing                        owns policy+credentials; format-woven execution
├── Changelog                      fragments compiled at release
├── Release orchestration          version + matrix + barrier + GH Release core
├── Distribution                   endpoint adapters; push + reconcile sweep
├── Consumption                    artifact-deps + requires placement; fetch/verify
├── Provisioning                   image layers + Menu options
└── Fleet validation               declaration symmetry checks, drift reporting
```

## Substrate [settled — ADR-0084]

Execution happens in a container; code is mounted from the host (the host edits, the
Substrate runs). Images are layered — one fleet baseline (rust, python/uv, node,
linters) plus sanctioned heavy layers from the Menu — owned by shipit, published by
shipit's own release, referenced by derivation from the Shipit pin (one pin axis).
The **Mac exception** covers only work physically requiring macOS: local GUI launch,
physically macOS-bound build legs (the `xcode` kind — lexed's appex), and Darwin
signing/notarization legs — with the signing half expected to move into the container
if the rcodesign spike passes. The build legs and local launch remain regardless: the
exception shrinks, it does not vanish. Toolchain identity comes from the image, never
from per-repo env machinery.

## Consumer surface

**Task veneer [design]** — a shipit-managed `Taskfile.yml` at the repo root: the four
standard verbs as one-line delegations (`cmds: ["shipit test {{.CLI_ARGS}}"]`), `--`
passthrough for ad hoc flags, explicit `TARGET=` var for the platform parameter
(never Task's host-inferring `platforms:` field). A consumer-owned optional include
(`Taskfile.local.yml`) is the canonical home for repo-local dev tasks (dev, watch,
preview) — interior freedom with a declared address, invisible to shipit.

**Tools [settled — ADR-0039 + components spec]** — lint, test, build, release: each a
fleet-wide contract (shared setup, invocation, machine-readable result — test deposits
JUnit XML at the standard path; human formats are presentation overrides). shipit is
always the implementation; the veneer is convenience and discoverability.

## Component system [settled — components spec]

A repo is a composition of **Components**: kind (rust, npm, python, go, lua, grammar,
docs-site, static-site, xcode) + mount point + the Tool implementations the kind
brings. The `xcode` kind (xcodebuild-driven native macOS targets — lexed's QuickLook
`.appex` is its first instance) builds only on Darwin legs and is a components-spec
amendment candidate alongside the packaging refactor. New toolchains become kinds in
shipit's owned surface — kinds, not hooks; the extension mechanism exists but ships
with zero uses.

**Builder contract**: a per-kind standalone program invoked at the mount with the
target platform as an explicit argument (`linux-x86_64` … or `universal`; never
host-inferred). Exit 0/non-0. Promised outputs at declared paths. Orchestrator and
builders share data types, never code.

**Build orchestrator**: iterates components in `requires`-derived order (manual
`build.after` for the rare unmediated edge): resolve inputs (intra-repo from promised
paths, inter-repo via staged fetch; placement = the consumer's `requires` key→dest
mapping; the resolver owns the read credential, builders get none) → invoke builder →
**verify promised outputs exist** → next. Sequential v1; parallelism enabled by the
architecture, not built. A build — component or whole repo — is a pure function of
(repo state, target platform) → outputs; the build system has no concept of a matrix.

## Artifacts [settled] and Packaging [design]

An **Artifact** is a named build product bound to one component, with a derived key
(`<owner>/<repo>/<name>`), `platforms` XOR `universal`, a promised output `path`, and
a **runtime** when it executes (native-cli, browser, electron, tauri, *-host, service,
static-web, data; libraries have none). Runtime selects the e2e harness
(lifecycle: synchronous | service); verification is total across artifact classes.

**Packaging [design — supersedes the artifact↔bundler 1:1 coupling in the components
spec; amendment to follow]**: one artifact fans out to **N packagings** — formats
applied to the built thing (`.app` → dmg + zip; linux binary → deb + rpm + tar;
exe → zip + msix). A packaging declares its format and rides the same `requires`
mapping for bundle-time inputs. Packagers package declared inputs; they never build
and never fetch. A packaging declares its **platform selector** (a subset of the
artifact's platforms — dmg is darwin-only, deb linux-only; defaults to all) and,
where one format yields multiple outputs, a **variant name with packaging-time
metadata** (the two vsix outputs). An artifact's raw output is additionally
addressable as its **identity packaging** — the output passed through as-is —
materialized only when an adapter consumes it directly (a static site dir deployed
to its host); an artifact nothing consumes raw has no identity packaging.
Distribution consumes packagings, not artifacts. Adopted leaf tools:
**nfpm** (verified: apk/archlinux/deb/ipk/**msix**/rpm/srpm; msix signing is
delegated through nfpm's PFX mechanism under the Signing subsystem's ownership —
see Signing), **@electron/packager** + dmg/zip leaf tools (the packager that genuinely takes
a prebuilt app dir in — electron-forge rejected on verification: its `package` always
runs the full build, forge#3053, and `make --skip-package` only reuses forge's own
output), **`tauri bundle`** (verified bundle-only flow: `build --no-bundle` then
`bundle`, same `TAURI_SIGNING_*` env, own `--no-sign`). No product/deliverable noun
above Artifact: a
cross-repo final app is an ordinary Artifact whose `requires`/artifact-deps closure
spans repos; the owning repo is its home.

## Signing [design, pending-spike]

Restates the components spec's "separate post-build stage" wording; rides the same
components-spec amendment as the packaging refactor.

A standalone subsystem that owns signing **policy and credentials** — that is
ADR-0040's seam: ownership, not a strict ordering slot. Execution points are
format-defined and woven into the packaging sequence: for macOS, sign the packaged
`.app` bottom-up (helpers/frameworks before the outer bundle, never `--deep`), *then*
assemble the DMG from the signed app, then notarize + staple the DMG (rcodesign's
verified constraints — it cannot sign inside a DMG or mutate flat `.pkg` contents —
force exactly this order). For MSIX, the signature is applied through nfpm's native
PFX mechanism as a **delegated step**: the certificate is injected by the signing
subsystem at invocation and never lives in packager config. The invariant packagers
must honor is therefore: they never *own* signing decisions or credentials — even
when a format's tooling is the mechanical signer. Target implementation:
**rcodesign** — sign, notarize, staple from Linux (verified: real projects run it
from Linux containers; App Store Connect API key supported). Until the hands-on
spike passes on a real fleet `.app`/`.dmg`, Darwin signing legs remain on the Mac
exception; a pass moves the CI signing/notarization legs into the container (the
Mac exception keeps local GUI launch and the xcode build legs).

## Changelog [settled]

`CHANGELOG/` fragments, one per change, compiled into the changelog at release;
`release prepare` refuses an empty release. Steal from changesets: the PR-level
"blocked without a fragment" gate UX. Nothing adopted — the machinery exists.

## Release orchestration [settled + design]

Release does no work; it orchestrates the standalone subsystems — build, changelog,
signing, distribution — over **release data** (version, tag, compiled changelog). It
derives the platform matrix from the artifacts' declared platforms, invokes one
arch-bound build per leg (same builder contract — no second protocol), fans out
packagings per leg, and enforces the **all-or-nothing barrier**: every declared
platform built, packaged, and signed before anything publishes (ADR-0009); publish is
ordered, fail-fast, idempotent-resumable. The spine output is a GH Release matching
the git tag with standardized asset naming (`<name>-<version>-<platform>-<arch>`,
universal unsuffixed). Workflow-triggered, never tag-triggered (the release creates
commits); runnable locally by construction (the same orchestrator call with a local
target). Stage boundaries are externally addressable (GoReleaser's `--skip` design,
ADR-0054's per-stage dispatch). Intra-pipeline wiring is a `needs:` DAG — matrix jobs
plus a merge/barrier job; no eventing inside the pipeline (no prior art, and the
barrier requires synchronous fan-in).

## Distribution [design]

Endpoint adapters (crates.io, npm, pypi, brew, deb repos, stores, firebase; the GH
Release spine is produced by release itself) each owning publish auth via
`[secrets]`/doppler. Execution model: **orchestrated push + reconcile sweep** — push
jobs `needs:`-gated in the release workflow (with an App/PAT token; the default
`GITHUB_TOKEN` suppresses downstream `release` events by design), plus a periodic
per-target reconcile sweep as the replay/backstop, because dispatch events have no
delivery log, no ordering, and no retry. The decoupling property ("adding a
distribution touches nothing else") comes from the adapter seam and the sweep, not
from event transport. Deferring a distribution = deferring an adapter.

## Consumption [settled]

`[artifact-deps."<owner>/<repo>/<name>"] { version }` — the one consumer-owned
version place — plus the consuming component's/packaging's `requires` key→dest
mapping. **Version binds identity; sha verifies transport**: the GH asset digest is
the integrity check for that version's bytes; in-place replacement is prohibited
except bug fixes (single-owner trust domain, stated threat model; no lock sidecar).
Fetch implementation: **`gh release download`** + digest verification; private repos
resolve the one known release by derived tag and match the exact derived filename
(bounded retrieval, not discovery — no index, no resolution, no transitivity, ever).
**`gh attestation verify`** is an optional provenance strengthening — verified
available for private repos on personal plans (the Enterprise-only restriction was
beta-era and lifted at GA), so the phos repos are covered.

## Provisioning [settled — ADR-0084]

Provisioning collapses into the Substrate: the image layers carry toolchains; Menu
options (heavy layers, runners: gpu, macos-arm64; e2e harnesses) are the sanctioned
selection surface with a small options schema. No per-repo tool installation exists.

## Fleet validation [settled]

Declarations are the agreement; reconcile and fleet checks enforce: artifact↔component
binding, acyclic ordering, mounts have manifests (detection is advisory cross-check
only), every consumed key resolves at an existing version, consumer platform needs ⊆
producer platforms (universal satisfies all), orphans and dangling consumers reported,
drift-from-Profile reported. `build --all` totality is a theorem of the declarations.

## Shared data model

The nouns (glossary-anchored, one line each — CONTEXT.md wins on meaning):
**Repo** · **Component** (kind, mount, requires) · **Tool** / **Tool contract** ·
**Builder** (per-kind program) · **Target** (platform | universal) · **Artifact**
(key, component, platforms/universal, path, runtime) · **Packaging** (artifact,
format, platform selector, optional variant + metadata, requires; every artifact has
an implicit identity packaging) · **Release** (version, tag, changelog, artifact
set) ·
**Distribution endpoint** / **Endpoint adapter** · **Artifact-dep** (key, version) ·
**Menu** / **Profile** / **Canary instance**.

The contracts (all process boundaries; shared data types, never code):
**builder contract** (target in, exit code + promised outputs out) · **packager
contract** (declared inputs — which may include previously signed outputs — plus
format in, output out; never owns signing decisions or credentials) · **signer
contract** (signable in → signed/notarized/stapled out; may execute by delegation,
injecting credentials into a format tool it supervises) — packager and signer steps
interleave at format-defined points within one packaging sequence · **adapter
contract** (packaging in, published endpoint state out, idempotent) · **verb
contract** (per Tool: setup, invocation, machine-readable result).

## Adopted tools

| Seam | Tool | Status |
| --- | --- | --- |
| Task veneer | Taskfile (go-task) | verified (schema stable; v4 is opt-in-flags, no break planned) |
| linux/windows packaging | nfpm | verified (incl. msix, PFX-signed) |
| electron packaging | @electron/packager + dmg/zip leaf tools | **pending-spike (hands-on)** |
| tauri packaging | `tauri bundle` | verified (bundle-only flow documented) |
| macOS signing/notarization | rcodesign | **pending-spike (hands-on)** |
| fetch/verify | `gh release download` (+ `gh attestation verify`) | verified (attestation incl. private repos on personal plans) |
| everything else | built in shipit | settled |

Rejected with reasons on file: GoReleaser & cargo-dist (orchestrator-grabbers; steal
`--skip` stage boundaries), electron-builder (fused pack/sign/make) and
electron-forge (`package` always builds — forge#3053 — so it cannot sit behind the
packager contract; its `import` migration is still useful reference), ubi (no
verification), semantic-release family (fragments already cover it), event transport
for distribution (no replay, token suppression), dagx (revisit iff the orchestrator
is ever rewritten in Rust). Survey reports: session scratchpad
(`adopt-survey-{release,eventing,taskrunner}.md`, `adopt-claims-verification.md`).

## Spikes (deliverability gate)

Hands-on, must pass before the dependent design point is final:

1. **Electron packaging + signing pipeline from Linux** — test article: **lexed
   including its QuickLook `.appex`**, the fleet's hardest signing case (nested
   bundle, sandbox entitlements): appex built on a Darwin leg (xcode kind) → npm
   builder output → `@electron/packager` → appex placed into `Contents/PlugIns/`
   via the packaging's `requires` → rcodesign scoped, bottom-up signing (appex with
   its own entitlements, then helpers/frameworks, then the outer `.app`) → dmg
   assembly → notarize + staple via App Store Connect API key (doppler) → nfpm
   deb and zip of the same build. Pass ⇒ the electron packaging row is settled; the
   Mac exception's CI-signing half shrinks away (ADR-0084 amendment; lexed keeps
   its appex Darwin *build* leg either way). Fail ⇒ Darwin signing legs stay on
   macOS runners and the electron packager choice is revisited.
2. **Container dev-loop ergonomics** (Suburbia Phase 2 spike, already planned) — one
   rust repo, edit-on-host / execute-in-container, lint/test/build parity.
3. **`src-tauri` → `crates/` renameability** (already planned with Phase 3 layout).

Doc-level verifications: complete (2026-07-30 claims scout —
`adopt-claims-verification.md`): nfpm msix TRUE; private-repo attestation SUPPORTED
(beta-era restriction lifted at GA); forge package-only OVERSTATED (led to the
@electron/packager revision); tauri bundle-only TRUE; rcodesign constraints TRUE
(ordering recipe above); Taskfile schema stable.

## Open questions

- None at the tree level. Per-subsystem: empty except items marked pending-spike /
  pending-verification above, which resolve to design changes only if a spike fails.

## Paper-fit appendix

Every Portfolio repo (plus comms, joining) mapped onto the tree — components,
artifacts (+ runtime), packagings, endpoints, menu selections. Source: the
2026-07-30 fleet fine-comb, mapped to the **target** state (crates/ moves and
workspace flattening from the filed pre-work applied; conda retired). Any repo that
did not fit would falsify the design; none does.

Registry-publishing note (adapter detail, not a model exception): npm tarballs and
python sdist/wheel are ordinary packagings the npm/pypi adapters publish. For
crates.io, the crate packaging runs **pre-barrier** as `cargo package` (producing
and verifying the `.crate` so the all-or-nothing barrier covers it); the adapter's
post-barrier `cargo publish` re-derives that same packaging at upload — a tool
constraint of cargo's fused package+upload, safe because the source state is
identical and the barrier already verified packagability.

| Repo | Components | Artifacts (runtime) | Packagings | Endpoints | Menu / notes |
| --- | --- | --- | --- | --- | --- |
| burgertocow | rust @ crates/ | cli (native-cli) | archive | GH | — |
| clapfig | rust @ crates/ | lib crates (—) | crate | crates.io, GH | — |
| dodot | rust @ crates/ · docs-site | cli (native-cli) | archive, deb | GH, brew | bats e2e |
| lookma | rust @ crates/ | cli (native-cli) | archive | GH | — |
| padz | rust @ crates/ | cli (native-cli) | archive | GH, crates.io, brew | bats e2e |
| rustloc | rust @ crates/ | cli (native-cli) | archive | GH, crates.io | — |
| standout | rust @ crates/ · docs-site | lib crates (—) | crate | crates.io, GH | — |
| simple-gal | rust @ crates/ · static-site @ static/ | cli (native-cli) · site (static-web) | archive · identity (site dir) | GH · site host | windows leg deferred by doctrine |
| supage | go @ root | service image (service) | service image | deploy target | Firestore harness (service lifecycle) |
| shipit | python @ src/ | package (—) | sdist/wheel | GH (+pypi) | the producer itself |
| shipit-canary | (generated per profile) | per profile | per profile | per profile | Canary instance, not hand-modeled |
| simple-gal-ui | npm @ root | app (electron) | .app→dmg/zip, exe→zip | GH | playwright-electron, macos-arm64 runner; dep: simple-gal cli |
| lexed | npm @ root · xcode @ quicklook/ | app (electron) · LexQuickLook.appex (darwin, intra-repo input) | dmg/zip/deb; appex placed into Contents/PlugIns via the packaging's `requires` | GH | playwright+xvfb; deps: lexd-lsp, comms, grammar; keeps one Darwin build leg (appex) regardless of signing outcome |
| mkdocs-lex | python @ mkdocs_lex/ | package (—) | sdist/wheel | pypi, GH | — |
| lex | rust @ crates/ | lexd, lexd-lsp (native-cli) · lex-wasm (browser, universal) | archives, wasm-pack | GH | fleet keystone producer; sandbox-tests = consumer-owned extra lane |
| nvim | lua @ lua/lex | none — the repo itself is the plugin, consumed from git by plugin managers | none | GH spine only (notes-only; zero distribution adapters) | degenerate release: tag+changelog, empty artifact set — the spine is produced by release itself, not an adapter |
| tree-sitter-lex | grammar @ root · npm @ root | grammar wasm (browser, universal) · npm pkg (—) | wasm-pack, npm tarball | GH, npm | grammar kind's first instance |
| vscode | npm @ root | extension (vscode-host) | vsix ×2 — Marketplace and Open VSX variants (same build, different packaging-time metadata) | GH · VS Marketplace (vsce) · Open VSX (ovsx) | vsix-smoke; deps: lexd-lsp, comms, grammar; the same-format/different-metadata case of packaging 1→N |
| zed-lex | rust @ root | extension (zed-host, universal wasm) | zed-ext | GH · zed registry (human-gated adapter) | — |
| phos-core | rust @ crates/ | binaries (native-cli) · wasm pkgs (browser, universal) · corpus, fixtures (data, universal) | archives, wasm-pack, tarballs | GH (private) | GPU lane (registry, until durable runner) |
| phos-app | npm @ root · rust @ crates/ (tauri shell) | app (tauri) | tauri→dmg/deb/msi | GH (private) | tauri-driver, GPU lane (registry); deps: phos-core wasm, corpus, fixtures |
| phos.photo | static-site @ site/ | site (static-web, universal) | identity (site dir as-is) | firebase | no toolchain — correct |
| comms | docs-site (+ data content) | tarball (data, universal) | tarball | GH | ADR-0085 first user; submodule retired |

**Verdict: fits.** Zero unexpressible repos. The model's degenerate cases are each
exercised exactly where expected — empty-artifact release (nvim), no-toolchain repo
(phos.photo), data-only producer (comms), generated instance (shipit-canary). The
true variation registry shrinks to: the two GPU lanes (hardware-gated), the zed
registry's human-gated adapter, and lex's sandbox-tests extra lane — three entries
for a 23-repo fleet.
