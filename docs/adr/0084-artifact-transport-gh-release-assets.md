# Artifact channel transport moves to GH Release assets

ADR-0077 collapsed cross-repo artifacts to conda-direct. Its load-bearing
content was never conda — it was the invariants: the location is derived from
the producer repo (never restated), the version is stated in exactly one
consumer-owned place, and integrity is verified so a wrong name or version
fails locally. With image-owned toolchains (ADR-0083), conda/pixi stops being
ambient in consumer repos, so a conda channel is no longer the natural
transport, and the channel infrastructure (buckets, rattler-build, repodata)
would exist to serve nothing else.

## Decision

The transport becomes the producer's **GH Release assets** under the
publishing spine's standardized naming. The invariants carry over verbatim:

- The consumer declares producer repo + version + sha in exactly one
  consumer-owned place; the asset location is derived from the repo name.
- `shipit stage` fetches, verifies the sha, and unpacks/stages — a wrong
  name, version, or hash fails loudly, locally, before anything ships.

**The guard: this mechanism gets no index, no resolution, no transitivity.**
One repo, one version, one sha. The moment a repodata-equivalent or a resolver
is proposed here, shipit is rebuilding a package manager for the third time —
that proposal is the mistake ADR-0077 documented, and this line exists so it
is recognized on sight.

First user: **comms**, which becomes a Portfolio member releasing a tarball
artifact that consumers expand via `shipit stage`, retiring its git-submodule
consumption (legacy from before any transport existed; see #1125 for the
defect class the submodule caused).

## Consequences

- The conda channel realization retires with the migration: GCS channel
  buckets, rattler-build packaging, repodata indexing, and the `[s3-options]`
  projection.
- ADR-0077's transport half is superseded by this ADR; its invariants and its
  pin-governance handoff (generic bot, consumer-owned version) stand.
