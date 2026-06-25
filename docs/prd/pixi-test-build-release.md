# pixi test / build / run + changelog / release

> Status: **postponed** — not yet started. Planned successor work; see `docs/prd/FUTURE_WORK.md`.
> Origin: scope sketched in the retired roadmap §5, summarized below.

## Intended scope

Encapsulate each project's test and build behind pixi tasks: the consumer
supplies `test`, while `build` runs the real builder (cargo, tauri,
electron-builder — pixi provisions and runs them, it is never the build backend
itself, per `architecture.lex §3`). On top of those task encapsulations, build the
`changelog` and `release` subcommands ON pixi tasks — NOT on the preview-grade
pixi-build backend.

This step is designed to run ALONGSIDE the existing release workflows; nothing is
retired yet (the second hard rule — do not retire release-core until shipit cuts
one real release — still holds here).

Nothing is built yet. The verification target, once started, is: `pixi run test`
and `pixi run build` work locally and in CI on at least two different project
Kinds, and `shipit changelog` coalesces unreleased fragments into a version and
feeds the tag + release notes.
