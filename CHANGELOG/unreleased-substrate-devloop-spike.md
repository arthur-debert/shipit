- docker, lab: the container-substrate dev-loop spike (#1198, ADR-0084's
  Suburbia Phase 2 probe) landed its surviving artifacts: the fleet rust
  baseline image definition (`docker/rust-baseline.Dockerfile` — rust
  toolchain, the fleet-pinned linters, gh and shipit itself baked from apt +
  native toolchains, no pixi), the runnable measurement harness
  (`lab/substrate-spike/spike.sh`), and the findings report
  (`docs/dev/substrate-devloop-spike.md`). Headline: full parity — `shipit
  lint` / `shipit test` / `shipit build` inside the container produce
  identical findings, test results and artifact sets to today's host pixi
  path on the same rustloc commit — with the honest ergonomics numbers
  (cold-start, target-dir-on-mount vs named-volume build caching, file-watch
  round-trip, gh credential passthrough) recorded for the Substrate design's
  open items.
