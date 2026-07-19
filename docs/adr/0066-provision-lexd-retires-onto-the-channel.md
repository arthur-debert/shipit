# `provision lexd` retires; the lint gate rides the Artifact channel

> **Amended by ADR-0071.** The **stable `lexd` publication** this cutover is
> gated on need not cover every served subdir: the readiness gate is the served
> subdirs that are not owner-paused, so `win-64` is not probed while shipit#895
> holds. (A prerelease seed populates the channel for pin-testing but does
> **not** satisfy this gate — the cutover still requires a stable `lexd`.)
> Windows consumers fail closed after the cutover — the no-fallback decision
> below is unchanged.
>
> **Executed by ARF02-WS06.** The cutover has landed: `lexd` is published to the
> public Artifact channel at **0.19.10** (served on osx-arm64/linux-64/linux-aarch64;
> win-64 paused per ADR-0071), the `provision` module and verb are **deleted**,
> and the pin now lives in a shipit-managed `[feature.shipit-lexd]` pixi block
> (channel `https://storage.googleapis.com/shipit-artifacts-public/lex-fmt/lex`,
> `lexd = "==0.19.10"`) wired into the lint env and resolved through `pixi.lock`.
> No fallback is retained; a win-64 lint solve fails closed by design.

`lexd` is the one lint-gate tool not on conda-forge, so it could not ride
`pixi.lock` like the other linters; `shipit provision lexd` fetched it bespoke
from `lex-fmt/lex`'s GitHub release — pinned in the shipit binary with
trust-on-first-use SHAs — into every managed repo's lint env. Once the Artifact
channel (ADR-0064) exists, `lexd` can be published to it as an ordinary conda
package, which removes the *sole* reason `provision` existed.

## Decision

- **Publish `lexd` to the (public) Artifact channel and retire
  `shipit provision lexd` entirely** — delete the `provision` module and its
  hand-pinned `SHAs`. `lexd` becomes an ordinary conda dependency resolved
  through `pixi.lock`, integrity-checked by pixi's sha256 (stronger than the
  bespoke trust-on-first-use pins).
- **Preserve fleet-uniformity of the gate** by moving the `lexd` pin into a
  **shipit-managed, non-consumer-editable pixi block** in the lint env (the
  managed-file mechanism shipit already uses for other pixi blocks), so
  `shipit install` keeps every repo on one `lexd` version. Uniformity moves
  from a compiled binary constant to a managed manifest block (ADR-0047: some
  things are not consumer config).
- `lexd` is open source, so it lives in the **public** bucket and is served
  **authless** — the ubiquitous gate tool needs no credentials on any laptop or
  runner.
- **Clean cutover, no fallback:** seed the channel with `lexd` once, then
  delete `provision`; no `provision` fallback is retained.

### Alternatives rejected

- **Keep `provision lexd` alongside the channel** — two mechanisms for the same
  job; leaves the bespoke SHA-pinning and the binary-embedded pin in place. The
  channel makes `provision`'s only rationale ("not on conda-forge") obsolete.
- **Keep the pin in the shipit binary for uniformity** — the managed lint block
  gives the same fleet-uniform guarantee through the mechanism shipit already
  uses, without a compiled constant or a bespoke fetcher.

## Consequences

- Linting now depends on the public channel being reachable at `pixi install`
  time — the same shape as the existing conda-forge linters, and authless, so
  no new credential surface.
- **Self-hosting:** `lex-fmt/lex` lints its own code with `lexd` from the
  channel it produces — i.e. it consumes its *prior* release's `lexd`. Normal
  self-hosting bootstrap; stated so it does not surprise.
- **Bootstrap ordering:** the channel must hold `lexd` before any repo can
  lint; a one-time seed precedes the cutover.
