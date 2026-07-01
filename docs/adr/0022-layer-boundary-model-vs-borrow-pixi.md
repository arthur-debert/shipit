# Layer boundary: what shipit models vs what it borrows from pixi

> **Status: Proposed.** Core Model epic (COR01). Complements ADR-0021 and
> architecture.lex §1 ("pixi as the substrate").

shipit **borrows pixi's environment/path model through pixi's JSON** and **owns** the
git, GitHub, and agentic layers as its own value objects — so it neither reinvents pixi
nor reaches underneath it.

## Context

shipit is a thin layer over pixi. pixi is a **Rust CLI consumed via subprocess + JSON**;
there is no pixi Python library. The underlying **rattler** library *does* have Python
bindings (`py-rattler`), but that is the conda-primitives layer *beneath* pixi, not
pixi's workspace/env model. Core Model needs a stated boundary for where shipit's value
objects stop and pixi's model begins.

## Decision

A four-layer boundary:

- **Env / paths / activation → BORROW pixi, via its JSON.** shipit reads
  `pixi info/list/shell-hook --json` and the on-disk `conda-meta/pixi`; its value objects
  *mirror* those shapes and it **never re-derives activation**. Execution routes
  **through `pixi run`** so pixi owns activation (the #197 pattern); shipit only *scrubs*
  (a pure env-snapshot transform) and reads pixi's JSON for env identity. **Do not import
  `py-rattler`** — reaching under pixi fights the abstraction pixi was chosen for.
- **git identity → OWN.** `Repo` / `Owner` / `WorkingDir` / revision (ADR-0024). pixi
  models none of it — clean greenfield.
- **GitHub → OWN.** `PR` / `Reviewer` / funnel / checks. pixi has nothing here; the bulk
  of new modeling.
- **agentic → mostly OWNED already.** `Run` / `Role` / `Variant` / `Backend` / `Model` /
  `Invocation` (ADR-0025).

The **single clash surface** — where both pixi and shipit want to define "the environment
a process runs in" — is env/activation, resolved by consuming pixi's output rather than
computing a rival.

## Considered options

- **Import `py-rattler` for in-process conda handling** — rejected: wrong layer; couples
  shipit to conda primitives beneath pixi. Only revisited under a Rust rewrite (ADR-0023).
- **Re-derive activation in shipit** — rejected: the reinvention Core Model removes.

## Consequences

WS-pixi-activation reads pixi JSON / `conda-meta/pixi` for env identity and moves the
sccache env into pixi `[activation.env]`; no shipit code models conda packages. Env
handling stays a thin pixi-JSON consumer.
