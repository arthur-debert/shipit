- install/artifact-deps: the consumer contract collapses to conda-direct
  (ADR-0077, #1092). **Location is derived:** `[artifact-deps.<pkg>] { repo }`
  is the sole input from which shipit projects the managed `channels` (+
  private-tier `[s3-options]`) block — the URL is never restated. **Version is
  consumer-owned:** the version is pinned as an ordinary pixi dependency in the
  SAME feature that carries the derived channel —
  `[feature.shipit-artifacts.dependencies].<pkg>` (or
  `[feature.shipit-artifacts-<feature>.dependencies].<pkg>` for a named
  `feature`) — so pixi resolves the pin against the channel, `pixi.lock` records
  it, and a generic bot (`pixi update` / Renovate) bumps it. `shipit install`
  fails loud if a declared artifact has no such pin, naming the exact table to
  add (never a silent resolve-nothing).
- **No backwards compat (ADR-0077):** the legacy `[artifact-deps.<pkg>]`
  `version` key is refused at parse with a migration-pointing message; the
  `version` field is removed from the typed `ArtifactDep`.
- **Cascade removed (supersedes ADR-0067):** the bespoke artifact-dep version
  bump — the consumer-side receive/bump workflow (`channel receive`) and the
  producer-side fan-out (`release cascade`) — is deleted. Cross-repo version
  bumps now happen via plain pixi / a generic dependency bot editing the
  consumer-owned pin. The source-rebuild `notify-downstreams` rail is unaffected.
