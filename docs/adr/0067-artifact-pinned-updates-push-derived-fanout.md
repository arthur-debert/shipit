# Artifact-pinned updates propagate by push with a derived fan-out

With artifact-pinned dependencies (ADR-0064), a downstream pins a version; when
the upstream releases a newer one, the pin must be bumped for the new bits to
flow. The glossary's **Cascade** said "opens version-bump PRs"; the code's
`notify-downstreams` fired `repository_dispatch` to trigger rebuilds. We needed
one coherent update model.

## Decision

- **Push with a derived fan-out (artifact-pinned).** On an upstream's stable
  release, propagation to *artifact-pinned* consumers is *immediate* (no poll
  latency). Their target set is **derived** — computed from which repos declare
  an `[artifact-deps]` on the upstream — not a producer-maintained list, so the
  consumer's declaration stays the single source of truth and cannot drift.
- **Transport:** reuse the `notify-downstreams` dispatch rail; fire
  `repository_dispatch` at each derived target carrying `{upstream repo, new
  version}`. Each consumer's managed **receive-workflow** opens its *own* draft
  bump PR (permissions stay per-repo; no upstream write-token into downstreams).
  The PR bumps every matching `[artifact-deps]` version → normal review loop →
  `pixi.lock` re-resolves → new bits from the channel.
- **Stable-only auto-bump:** rc versions are published to the channel
  (ADR-0064) for manual opt-in pin-testing but are **never** auto-bumped.
- **Two Dependency modes, two fan-out sources, one push:** an artifact-pinned
  downstream gets a version-bump PR, its target set derived from `[artifact-deps]`
  (above); a source-pinned downstream gets a rebuild via `notify-downstreams`'
  existing **producer-declared `Artifact.downstreams`** list, unchanged by this
  ADR — there is no consumer-side source-pinned declaration to derive from. Same
  dispatch rail; both the effect and the fan-out source follow the downstream's
  Dependency mode.

### Alternatives rejected

- **Consumer-poll (Dependabot/Renovate shape)** — a scheduled per-consumer
  check diffing the pin against the channel's `repodata.json`. More decoupled
  and needs no cross-repo read at release time, but propagation is not instant.
  Rejected for the instant-propagation requirement.
- **A producer-declared downstreams list for the *artifact-pinned* fan-out** —
  a second declaration of the same edge (the artifact-pinned consumer already
  declares the dependency in `[artifact-deps]`), which drifts when a consumer is
  added but the producer's list is not updated. Deriving the artifact-pinned set
  eliminates the drift. Source-pinned rebuilds keep the producer-declared
  `Artifact.downstreams` list, since there is no consumer-side source-pinned
  declaration to derive from.

## Consequences

- Deriving the target set needs a **release-time portfolio scan** (or a
  maintained who-depends-on-whom index) and a cross-repo **read** token on the
  release job — heavier than reading a local list, accepted as the price of
  zero drift; `fleetsweep` already does portfolio scans.
- Reconciles the glossary and the code: the Cascade is **one push** whose effect
  follows the downstream's Dependency mode (CONTEXT.md updated accordingly).
