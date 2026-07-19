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
  smoke test. It is a **single-root Vite app**, not a nested npm workspace member,
  so it declares its Artifact with `toolchain="npm"` and **no package**, and builds
  via a bare `pnpm run build` (a `vite build`) with no `--workspace` narrowing. It
  is a scaffold, deliberately NOT SvelteKit or any SSR/app framework: a
  `--stack svelte-app` selection produces a minimal, non-production
  single-page-app starting point, not an application architecture.

- **This mirrors the existing profile pattern.** `RustProfile`
  ([src/shipit/repocreate/profiles.py](../../src/shipit/repocreate/profiles.py))
  is already ONE opinionated scaffold, not a configurable generator: it hard-owns
  the two-member Cargo workspace, the source, and the single test. `svelte-app`
  is the same kind of thing for a frontend — an opinionated set of owned files
  and a `Contribution` — so it is a peer profile, not a new mechanism.

- **A root-app Artifact declares no package; that generalization is #1083 seam
  work.** The current Creation Artifact contract makes `ArtifactDecl.package`
  mandatory, the planner always serializes it, and
  [src/shipit/tools/build.py](../../src/shipit/tools/build.py) `_narrow` appends
  the `--workspace <package>` token to the **toolchain build command** (`npm run
  build` today, pnpm-reconciled to `pnpm run build` per ADR-0077) — so a
  single-root Vite app modeled with a package would run an invalid `pnpm run build
  --workspace <package>` (the narrowing token tacked onto the build command, not
  onto `vite` itself). svelte-app is therefore a single-root app that **omits** the
  package: `ArtifactDecl.package` becomes **optional**, the planner skips it when
  absent, and `_narrow` skips the narrowing token when the target has no package,
  leaving a bare `pnpm run build` (a `vite build`). This is also why the pnpm
  reconciliation must be `--filter`-aware for packaged Artifacts, not
  `--workspace` (ADR-0077). Making the package optional is
  prerequisite seam work **owned by #1083** (alongside the pnpm-leg reconciliation
  and dependency materialization ADR-0077 depends on). This ADR decides the
  modeling — root app, no package, bare build — while #1083 provides the machinery.

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
  dependency-materialization step (a frozen `pnpm install --frozen-lockfile`) and
  pnpm-leg reconciliation before its smoke test can run. It additionally depends
  on the optional-`ArtifactDecl.package` generalization above so its packageless
  root-app Artifact builds without `--workspace` narrowing.
- Future frontend or specialized scaffolds follow the same rule: a new
  opinionated scaffold is a new profile key, not a flavour axis or a composition.
