# Frontend scaffolds are distinct Creation profiles

`shipit repo new` gains a Svelte/Tailwind frontend scaffold (`svelte-app`). The
question is not WHETHER to offer it but how to MODEL it: as its own Creation
profile, as a composition of stacks (`--stack node --stack svelte`), or as a
base×flavour axis on the Node profile (`--stack node:svelte`). This ADR records
that a frontend scaffold is its OWN opinionated `Profile` in the closed
registry, exactly as `rust` and `node` are.

## Decision

- **`svelte-app` is a distinct Creation profile with its own registry entry.**
  It scaffolds an SPA-only project — Vite + Svelte + Tailwind + TypeScript, one
  smoke test — declares its Artifact with `toolchain="npm"`, and builds via
  `npm run build` (a `vite build`). It is a scaffold, deliberately NOT SvelteKit
  or any SSR/app framework: a `--stack svelte-app` selection produces a minimal,
  non-production single-page-app starting point, not an application
  architecture.

- **This mirrors the existing profile pattern.** `RustProfile`
  ([src/shipit/repocreate/profiles.py](../../src/shipit/repocreate/profiles.py))
  is already ONE opinionated scaffold, not a configurable generator: it hard-owns
  the two-member Cargo workspace, the source, and the single test. `svelte-app`
  is the same kind of thing for a frontend — an opinionated set of owned files
  and a `Contribution` — so it is a peer profile, not a new mechanism.

- **Rejected: multi-stack composition (`--stack node --stack svelte`).** The
  `--stack` flag is repeatable so a request can later compose ORTHOGONAL
  toolchains (a Rust binary plus a Python sidecar). A Svelte frontend is not
  orthogonal to Node — it SPECIALIZES it: Svelte is a Node project with an
  opinionated dependency set and build. Modeling it as composition breaks on
  three counts: (a) both a `node` and a `svelte` contribution would claim
  `package.json`, and the planner composes profiles that own DISJOINT files
  (ADR-0057); (b) the specialization dependency ("svelte requires node") is not
  expressible in a flat set of independently-composed stacks; (c) it would
  invite a `--stack svelte` selection with no `node`, which is meaningless. The
  frontend is one profile, not two composed ones.

- **Rejected: a base×flavour axis (`--stack node:svelte`).** Encoding the
  frontend as a flavour of the Node base introduces new selection machinery — a
  `base:flavour` grammar, a base×flavour resolution matrix — for a combinatorial
  space shipit does not need. The closed registry (ADR-0063) is a flat keyed map
  of opinionated profiles; `svelte-app` is one more key in it. A whole flavour
  axis to avoid one extra registry entry is machinery for its own sake.

- **A frontend profile may exceed the "minimal library and CLI" shape.** The
  Spec's Non-Goal against "a production application architecture beyond a minimal
  working library and CLI" was written for the Rust tracer. A frontend scaffold
  is legitimately fuller than a lib+CLI pair (an index page, a component, a
  Tailwind config, a Vite config) while still being a minimal, non-production
  scaffold. The Spec is amended to carve `svelte-app` out of that limit: a
  frontend profile may exceed the lib+CLI shape and remains bound only by
  "minimal, non-production scaffold," not by the lib+CLI template.

- **The closed-registry decision (ADR-0063) stands.** This is polymorphism over
  shipit-OWNED, reviewed profiles — adding `svelte-app` is a reviewed shipit
  change with packaged resources and fixtures — NOT runtime plugin discovery,
  remote templates, or user-supplied profile directories. Nothing here reopens
  ADR-0063.

## Consequences

- The closed registry grows a third key, `svelte-app`, alongside `rust` and
  `node`. Making the registry polymorphic over a `Profile` protocol (it is typed
  `dict[str, RustProfile]` today) is the seam work in shipit#1083; this ADR
  decides the MODELING (distinct profile, not composition or flavour), #1083
  provides the machinery.
- `docs/spec/repo-new.md` is amended to list `svelte-app` as a supported stack,
  to describe its shape, and to carve it out of the minimal-lib+CLI Non-Goal.
- Because `svelte-app` declares `toolchain="npm"` and tracks a `package.json`,
  it rides the same existing `npm` dispatch leg and managed `nodejs`/`pnpm`
  provisioning as the Node profile (ADR-0077), and depends on the same #1083
  dependency-materialization step before its smoke test can run.
- Future frontend or specialized scaffolds follow the same rule: a new
  opinionated scaffold is a new profile key, not a flavour axis or a composition.
