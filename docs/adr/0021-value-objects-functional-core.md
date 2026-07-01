# Value objects and a functional core (over Python's OOP grain)

> **Status: Proposed.** Foundational modeling decision for the Core Model epic (COR01);
> every Core Model work stream is written against it.

shipit models its domain as **thin, composable value objects with logic expressed as
free functions over them**, deliberately resisting Python's gravity toward deep OOP
(rich class hierarchies, logic-in-methods, mutable objects threaded through).

## Context

shipit's domain is state modeling — environment variables, files on disk, git and
GitHub state. Python pulls hard toward object-orientation, and left unchecked that pull
produced the exact debt Core Model exists to cure: three parallel class hierarchies for
the same reviewer/backend agents, mutable module-global caches, and snapshot objects
carrying maybe-false fields. The codebase already *leans* the other way — 34 frozen
dataclasses vs 10 mutable, and `os.environ` read through an injectable boundary at nearly
every site — so this ADR consolidates an existing lean into a stated contract rather than
mandating a rewrite.

## Decision

1. **Thin, composable value objects.** Not flat, not deep hierarchies — a `WorkingDir`
   *holds* a `Repo`. Frozen dataclasses are the default vehicle (for Python's standard
   interfaces — equality, unpacking, iteration) but are not mandatory.
2. **Functions over values.** Logic lives in free functions, not methods carrying
   behaviour. Methods appear only where they pull real weight; a closed registry +
   dispatch is preferred over a class hierarchy for polymorphism.
3. **Pragmatic about I/O — isolate mutable state at the boundaries, don't chase purity.**
   This domain *is* mutable state, so we do not fight Python with side-effect-free
   purity. Instead: **boundary functions** do I/O and return immutable snapshots; a
   **functional core** transforms snapshot → snapshot; a thin **edge** applies effects
   (`subprocess(env=…)`, file writes).
4. **No mutable module-global state.** Caches like the reviewers required/rerun globals
   become passed values or memoized pure functions.

## Considered options

- **Full functional purity** — rejected: fights Python and an inherently stateful domain
  for poor cost/benefit.
- **Idiomatic OOP** — rejected: it is the disease (competing hierarchies, logic-heavy
  classes, latent-trap snapshots) Core Model removes.

## Consequences

Every Core Model WS is written this way and reviewers hold code to it. Testability and
composability improve (pure core, injected boundaries). Because it consolidates an
existing lean, adoption is incremental, not a rewrite.
