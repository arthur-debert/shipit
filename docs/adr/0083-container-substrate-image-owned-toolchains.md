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
- **The Mac exception** is the sole carve-out: locally launching the GUI apps,
  and CI signing/notarization legs, run natively on macOS. The exception is
  licensed to use cruder pinning (pixi may survive solely to serve it) because
  its blast radius is a few repos' inner dev loop. It must not accumulate
  general machinery.

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
  changes separately (ADR-0084).
- Profiles (Suburbia Phase 3) are written against this target substrate so the
  fleet is stamped once.
