# Artifact-pinned updates propagate by push with a derived fan-out

With artifact-pinned dependencies (ADR-0064), a downstream pins a version; when
the upstream releases a newer one, the pin must be bumped for the new bits to
flow. The glossary's **Cascade** said "opens version-bump PRs"; the code's
`notify-downstreams` fired `repository_dispatch` to trigger rebuilds. We needed
one coherent update model.

## Decision

- **Push with a derived fan-out.** On an upstream's stable release, the Cascade
  propagates *immediately* (no poll latency). The target set is **derived** —
  computed from which repos declare an `[artifact-deps]` on the upstream — not a
  producer-maintained downstreams list, so the consumer's declaration stays the
  single source of truth and cannot drift.
- **Transport:** reuse the `notify-downstreams` dispatch rail; fire
  `repository_dispatch` at each derived target carrying `{upstream repo, new
  version}`. Each consumer's managed **receive-workflow** opens its *own* draft
  bump PR (permissions stay per-repo; no upstream write-token into downstreams).
  The PR bumps every matching `[artifact-deps]` version → normal review loop →
  `pixi.lock` re-resolves → new bits from the channel.
- **Stable-only auto-bump:** rc versions are published to the channel
  (ADR-0064) for manual opt-in pin-testing but are **never** auto-bumped.
- **Two Dependency modes, one push:** an artifact-pinned downstream gets a
  version-bump PR; a source-pinned downstream gets a rebuild
  (`notify-downstreams`' existing effect). Same rail; the effect is chosen by
  the downstream's Dependency mode.

### Alternatives rejected

- **Consumer-poll (Dependabot/Renovate shape)** — a scheduled per-consumer
  check diffing the pin against the channel's `repodata.json`. More decoupled
  and needs no cross-repo read at release time, but propagation is not instant.
  Rejected for the instant-propagation requirement.
- **A producer-declared downstreams list as the fan-out** — a second
  declaration of the same edge (the consumer already declares the dependency),
  which drifts when a consumer is added but the producer's list is not updated.
  Deriving the set eliminates the drift.

## Consequences

- Deriving the target set needs a **release-time portfolio scan** (or a
  maintained who-depends-on-whom index) and a cross-repo **read** token on the
  release job — heavier than reading a local list, accepted as the price of
  zero drift; `fleetsweep` already does portfolio scans.
- Reconciles the glossary and the code: the Cascade is **one push** whose effect
  follows the downstream's Dependency mode (CONTEXT.md updated accordingly).
