# Language: stay Python; defer a Rust rewrite to a separate spike

> **Status: Proposed.** Records why Core Model (COR01) ships in Python and how the Rust
> question is held.

shipit **stays Python** for Core Model. A Rust rewrite is a legitimate long-term
direction on its own merits, but the "reuse pixi's source to save a lot of code" premise
does **not** hold, so it is captured as a **separate strategic spike**, not folded into
this work.

## Context

pixi and rattler being Rust raised whether shipit should be rewritten in Rust to reuse
their structs. Investigated:

- The reusable Rust code is **rattler** (conda primitives — `rattler_shell` activation,
  conda types, solve), *not* pixi (an application whose internal crates are not reuse
  libraries).
- rattler maps only to shipit's **env/activation layer** — the one layer shipit already
  delegates to pixi via subprocess (~5 files).
- **~85% of shipit** — the PR state machine (~3k lines), the review funnel (~2.8k), the
  agent harness, eval, and the verbs — has **no** pixi/rattler analog. shipit is ~18k
  lines plus ~17.5k lines of tests.

## Decision

Core Model ships in Python. The reuse argument is largely a mirage (overlap is the one
already-delegated layer), so it does not justify a rewrite. Rust's *intrinsic* merits are
real — single-binary distribution, no dependency hell, portfolio consistency, in-process
activation via `rattler_shell` + `octocrab` for GitHub — so Rust remains a **separate,
later strategic spike**, decided on those merits. Core Model's design (the value objects,
the layer boundary, the terminology, these ADRs) is **language-agnostic and transfers** to
a Rust implementation; only the Python *implementation* would be throwaway if Rust later
wins.

## Considered options

- **Rewrite now** — rejected: enormous cost to reuse one thin, already-delegated layer;
  would stall the convergence value.
- **Plan Core Model as a Rust rewrite from the start** — rejected: pre-commits a huge
  pivot on weak (reuse-based) grounds.
- **Drop Rust entirely** — rejected: the intrinsic merits are real and worth a future
  decision.

## Consequences

A future "Rust shipit" spike (drive one PR-state transition via `octocrab`; activate a
Tree env via `rattler_shell`) can test ergonomics. This ADR records that the reuse
argument alone does not carry the rewrite.
