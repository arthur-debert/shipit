# Execution moves to a container Substrate; images own the toolchain

shipit was designed around a founding assumption that no longer holds: that it
runs hermetically on the owner's macOS dev box, protecting and being protected
from the host. A large share of its complexity is the tax of that assumption —
per-repo tool pinning, activation ceremony, the lint orchestration that is
conceptually a deployed config but carries hundreds of lines of hermeticity
engineering. Agentic development made isolated execution environments the norm,
not the exception. (Suburbia Spec: `docs/spec/suburbia.md`.)

## Decision

- **The Substrate is a container by default.** Code lives on the host
  filesystem, where the owner and agents read and write it; execution — lint,
  test, build, every task — happens inside the container. Local dev loops and
  CI legs use the same model.
- **The image owns the toolchain.** Toolchains and system deps are baked into
  the image (apt + the language toolchains), not provisioned per-repo by pixi.
  Hermeticity is the container; no machinery defends the host, because the host
  never executes.
- **Images are layered.** One fleet baseline layer carries the common
  toolchains (rust, python/uv, node, linters). Heavy needs (GPU stacks and
  similar) are sanctioned menu layers a repo selects — never per-repo snowflake
  images.
- **shipit owns the images, and there is one pin.** The Dockerfiles are shipit
  content; images are built and published by shipit's own release. A consumer
  never states an image tag: its image reference is derived from the Shipit pin
  it already has, plus its selected layers. Toolchain bumps ship as shipit
  releases, delivered by the same pin-bump reconcile as everything else.
- **The Mac exception** is the sole carve-out: work that physically requires
  macOS runs natively on it — the physically macOS-bound build legs (the
  `xcode` kind: Apple SDK / `xcodebuild` targets, e.g. lexed's QuickLook
  appex) and locally launching the GUI apps. The exception is licensed to use
  cruder pinning (pixi may survive solely to serve it) because its blast
  radius is a few repos' GUI legs and inner dev loop. It must not accumulate
  general machinery, and no leg joins it without a physical macOS requirement.
  (Amended 2026-07-31: CI signing/notarization and `.app`/`.dmg` packaging
  left the exception — the deliverability spike (shipit#1197, PR #1199) ran
  the full electron pipeline from a Linux container against lexed including
  its nested appex: `@electron/packager` assembly, `rcodesign` scoped
  bottom-up signing, dmg assembly + signing, notarization (Accepted) and
  staple. Only the appex `xcodebuild` leg needed Darwin.)
- **Revision pins stay total.** A Version pin (ADR-0080) uses the published
  image. A Revision pin falls back to the latest released image; when the
  revision changes the image definition itself, the dev loop builds the image
  locally from the revision's own Dockerfiles — the definition travels with
  the pin, so image derivation never has a hole.

## Considered options

- *Keep pixi running inside the container as the toolchain layer.* Rejected:
  the convergence sweeps rewrite every repo's managed content anyway, so
  carrying the pixi interface through convergence schedules a second
  fleet-wide sweep to remove it later — the fleet must be stamped once.
- *A separate infra repo with independently versioned images.* Rejected: it
  mints a second pin axis every repo must keep coherent with the first, which
  is exactly the restatement-drift failure mode (ADR-0077) that the one-pin
  rule exists to kill.

## Consequences

- pixi demotes from substrate to, at most, the Mac exception's pinning tool;
  the hermetic-on-host machinery retires as convergence lands.
- conda-direct's dependency-model invariants are unaffected; its *transport*
  changes separately (ADR-0085).
- Profiles (Suburbia Phase 3) are written against this target substrate so the
  fleet is stamped once.
