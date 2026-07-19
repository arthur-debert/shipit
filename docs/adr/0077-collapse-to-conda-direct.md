# Collapse cross-repo artifacts to conda-direct

ADR-0064 built the Artifact channel — a downstream repo consumes another repo's
released build artifacts as version-pinned conda dependencies. In practice,
getting one artifact produced and consumed has cost three sessions and ~8M
tokens. A layer-by-layer audit found why: **around the necessary conda kernel,
shipit accreted a second package manager** — a mechanism that re-implements or
wraps what pixi/conda already does. Every recurring failure is either
*restatement drift* (a name/version/payload authored on two sides with no single
source of truth) or a defect in one of those wrapper layers. The one axis that
has never drifted is the channel *location* — the only fact derived from a
single source rather than restated.

## Decision

Collapse the cross-repo artifact system to **conda-direct**:

- The producer packages its build output **directly** into a `.conda` and
  publishes it to the channel (`rattler-build`), instead of round-tripping
  through a gh-release asset it then re-downloads and reverse-engineers.
- The consumer's contract splits by ownership. **Location is derived:** a
  minimal `[artifact-deps.<pkg>] { repo = "owner/name" }` reference is the sole
  input from which shipit projects a managed `channels` (+ private-tier
  `[s3-options]`) block — the channel URL is never restated. **Version is
  consumer-owned:** a plain pinned `[dependencies] <pkg> = "ver"` line, recorded
  by `pixi.lock`, bumped by `pixi update` / a generic bot. No pin lives in a
  shipit-managed block. `pixi lock` resolves, pins and sha256-verifies — **the
  resolver is the agreement**, and a wrong name/version fails at lock time,
  locally, before any release.
- The channel (a GCS bucket holding the `.conda` files and `repodata.json`) and
  the two-tier access model (ADR-0065) are unchanged: they are the necessary
  kernel.

**Pin governance moves off shipit.** The `[artifact-deps]` DSL, the
managed-block ownership of pins, and the Cascade auto-bump are one coupled
bundle whose only load-bearing justification is shipit owning cross-repo pin
governance (auto-bump on release + enforced fleet-uniform pins). That is a
convenience a generic dependency bot (Renovate/Dependabot) already provides, not
a correctness need. We hand it off: `pixi.lock` is the record; a standard bot
opens bump PRs.

**One carve-out:** the `lexd` **lint-tool** uniformity — every repo running the
identical linter version — is genuine fleet governance, a different concern from
consuming an artifact. The managed block is retained **for that one tool only**;
it does not carry the artifact-deps DSL with it.

## The cut line

**Kernel — kept:** `rattler-build` build/publish/index of a `.conda` to the
channel (including the load-bearing `binary_relocation: false` no-relink recipe
insight); conda as one row in the release fan-out; the tier→URL glue
(`public→https`, `private→s3 + [s3-options]`) pixi cannot derive; two-tier
bucket provisioning; the RC guard and `--endpoint` selector (which protect the
*other* endpoints from co-firing on a conda seed).

**Accretion — removed:** the gh-release→conda repackage hop and its
per-composition asset-name reverse-engineering (package the build output
directly); the **pin-governance** half of `[artifact-deps]` — the declaration
shrinks to its `{ repo }` channel-derivation input (kept above), while the
version becomes a plain consumer-owned dep; the managed-block pin ownership
(pixi.lock already pins/verifies — except the `lexd` carve-out);
Cascade (use `pixi update` / a generic bot); the readiness-gate / served-subdir /
pause bookkeeping (a missing subdir is a failed `pixi lock` — fail-closed, which
is what the win-64 pause wants anyway).

## Consequences

- **Supersedes** the pin-governance half of `[artifact-deps]` (the block shrinks
  to its `{ repo }` channel-derivation input) and the derived-after-gh-release
  requirement of **ADR-0064**; **supersedes ADR-0067** (Cascade removed);
  supersedes the readiness-gate/served-subdir bookkeeping of **ADR-0070/0071**
  (their RC-guard/selector survive to protect the fan-out). **ADR-0065**
  (two-tier buckets) is unchanged.
- **Reframes ARF02:** the producer grammar work (#1078) and the consumer staging
  copy (#1079) survive and get simpler; **`shipit channel verify` (#1087) is
  dropped** — it is itself accretion, since `pixi lock` is the verify; #999/#1059
  are re-scoped to this decision.
- Migration is **producer-by-producer, then consumer-by-consumer, each proven by
  a real local run** (build a real `.conda`, run a real `pixi lock`, run the real
  staging + app). The only steps that need the outside world are compiling a
  Linux-native binary on a Mac and a live third-party publish; everything else is
  local and independently verifiable — which is why it converges instead of
  spiralling.
- The full design, dimensions, reference-table shape (structure, never state),
  and task list live in [`docs/spec/conda-direct.md`](../spec/conda-direct.md).
