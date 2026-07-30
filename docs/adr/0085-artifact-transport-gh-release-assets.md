# Artifact channel transport moves to GH Release assets

ADR-0077 collapsed cross-repo artifacts to conda-direct. Its load-bearing
content was never conda — it was the invariants: the location is derived from
the producer repo (never restated), the version is stated in exactly one
consumer-owned place, and integrity is verified so a wrong name or version
fails locally. With image-owned toolchains (ADR-0084), conda/pixi stops being
ambient in consumer repos, so a conda channel is no longer the natural
transport, and the channel infrastructure (buckets, rattler-build, repodata)
would exist to serve nothing else.

## Decision

The transport becomes the producer's **GH Release assets** under the
publishing spine's standardized naming. The invariants carry over verbatim:

- The consumer declares, in exactly one consumer-owned place: producer repo,
  **artifact name** (a producer releases many Artifacts, ADR-0007), version,
  and the sha of each fetched file. For platform-dependent artifacts the
  consumer's needed platform/arch selects the variant.
- The asset URL is **derived, never typed**: repo + the spine's standardized
  asset naming (`<name>-<version>-<platform>-<arch>`, universal artifacts
  unsuffixed) determines the file deterministically. The naming convention is
  the derivation — there is no listing step.
- `shipit stage` fetches, verifies the sha, and unpacks/stages — a wrong
  name, version, or hash fails loudly, locally, before anything ships.
- **Access follows the producer repo's visibility.** Assets of a private repo
  are private (ADR-0065's closed tier — phos stays closed); consumers fetch
  them with the portfolio GitHub credential delivered through the existing
  `[secrets]` machinery, since a consumer workflow's own `GITHUB_TOKEN` is
  repo-scoped. Public repos' assets are fetched authless. Nothing becomes
  public by the transport change.

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
