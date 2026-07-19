- install/artifact-deps: the consumer contract now splits by ownership
  (conda-direct, ADR-0077, #1092). **Location is derived:**
  `[artifact-deps.<pkg>] { repo }` is the sole input from which shipit projects
  the managed `channels` (+ private-tier `[s3-options]`) block — the URL is never
  restated. **Version is consumer-owned:** the projection no longer writes a
  version pin into a shipit-managed block; the consumer pins the package as an
  ordinary `[dependencies] <pkg> = "…"` line that `pixi.lock` records and a
  generic bot (`pixi update` / Renovate) bumps, resolved against the derived
  channels. `[artifact-deps]`'s `version` key is now optional and unprojected —
  a bare `{ repo }` declaration is complete; a still-present `version` is the
  per-consumer migration surface (the release-side Cascade keeps bumping it, and
  cleanly skips `{ repo }`-only entries) until Cascade is removed with the field.
