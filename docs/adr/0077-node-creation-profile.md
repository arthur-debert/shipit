# The Node Creation profile scaffolds a TypeScript CLI+library

`shipit repo new` shipped with one Creation profile, `rust` (ADR-0056 through
ADR-0063). The `node` profile is its first peer: a `--stack node` selection
that scaffolds a TypeScript project mirroring the Rust profile's pedagogy — a
CLI package that consumes a local library package, plus one black-box test that
runs the CLI and asserts `Hello, world!`, where the CLI obtains the greeting
from the library. Like the Rust profile it is one opinionated scaffold, not a
configurable generator.

## Decision

- **The Node profile scaffolds a TypeScript CLI + library, not a single flat
  package.** It mirrors the Rust profile's shape: a CLI package that depends on
  a local library package, the library supplies the hello-world value, and the
  CLI prints it. TypeScript — not plain JavaScript — is the scaffolded language,
  matching the portfolio's TS/Svelte repos and giving the generated project a
  build step worth verifying.

- **One black-box test.** The scaffold contains exactly one test, the parallel
  of the Rust profile's single `tests/cli.rs`: it exercises the CLI end to end
  and asserts the greeting the CLI obtained from the library. No example-test
  noise; the one test proves the CLI-consumes-library wiring and the configured
  `npm` test runner together.

- **The profile declares `toolchain="npm"` in its `ArtifactDecl`.** The
  Creation profile's Artifact claim
  ([src/shipit/repocreate/profiles.py](../../src/shipit/repocreate/profiles.py)
  `ArtifactDecl.toolchain`) names the DISPATCH toolchain, and the dispatch
  vocabulary for the Node ecosystem is `npm` — the registry entry
  `NPM = Toolchain("npm", test=("npm", "test"), build=("npm", "run", "build"))`
  ([src/shipit/tools/registry.py](../../src/shipit/tools/registry.py)) — so the
  generated Repo's `shipit test` / `shipit build` dispatch onto the existing
  `npm` toolchain leg with no new Tool machinery. See ADR-0078 for why a
  frontend scaffold is nonetheless its OWN profile rather than a flavour of this
  one.

- **The profile rides infrastructure that already exists.** The Node toolchain
  leg is not new work; this ADR records what is already in place so it is not
  re-litigated during implementation:
  - the `npm` dispatch entry in
    [src/shipit/tools/registry.py](../../src/shipit/tools/registry.py)
    `TOOLCHAINS` (`npm test` / `npm run build` as the default producing
    commands);
  - the `("package.json", "npm")` pair in
    [src/shipit/config.py](../../src/shipit/config.py) `SIGNAL_MANIFESTS`, which
    routes a tracked `package.json` to the `npm` toolchain for verb dispatch;
  - npm build narrowing (`--workspace <package>`) in
    [src/shipit/tools/build.py](../../src/shipit/tools/build.py) `_narrow`, so a
    workspace member Artifact target builds the right package;
  - the managed pixi node block in
    [src/shipit/install/units.py](../../src/shipit/install/units.py)
    (`pixi-node-deps-block.toml`: `nodejs`/`pnpm`), which the install
    reconciler already delivers to any Repo tracking a `package.json` — so the
    generated Repo's Node runtime is provisioned by the existing install
    baseline, exactly as the Rust profile relies on the managed Rust block.

- **Two vocabularies, deliberately.** The dispatch axis says `npm` (the Tool
  registry key and the Artifact's `toolchain`), while the install/provisioning
  axis says `node` (`TOOLCHAIN_NODE = "node"`; the `package.json` → node signal
  that delivers the `nodejs`/`pnpm` runtime block in
  [src/shipit/install/units.py](../../src/shipit/install/units.py)). These name
  the same ecosystem on two different axes and are both pre-existing; the Node
  profile speaks `npm` because it is declaring a dispatch target, not a
  provisioning signal. Reconciling or unifying the two vocabularies is out of
  scope here and belongs to the seam generalization (shipit#1083).

- **Project-dependency materialization is DEFERRED to shipit#1083.** The Rust
  profile's verification works because Cargo resolves path dependencies at build
  time with no separate install step. A Node project instead needs its
  dependencies materialized (`pnpm install` / `npm ci`) into `node_modules/`
  BEFORE the creation Checks (`npm test` / `npm run build`) can run. That
  pre-check materialization step is a base build/install toolchain-seam concern,
  filed separately as **shipit#1083**; this ADR does NOT specify its mechanism,
  where it hooks into the staged-verification flow, or how a profile signals
  that it needs it. The Node profile depends on #1083 landing that step; it does
  not define it.

### What is genuinely new (and what is not)

New, and owned by the Node profile change: the TypeScript CLI + library source,
the `package.json` workspace manifests, the one black-box test, the profile's
`Contribution` (owned files, its `.gitignore` additions, and the `npm` Artifact
declaration).

NOT owned here — it lives in shipit#1083: the profile registry becoming
polymorphic over a `Profile` protocol (today
[src/shipit/repocreate/profiles.py](../../src/shipit/repocreate/profiles.py)
`_REGISTRY` is typed `dict[str, RustProfile]`), profile-owned naming (the Rust
`lib<name>`/hyphen-to-underscore rules generalized so each profile owns its
ecosystem's naming), and the pre-check dependency-materialization step above.
The Node profile is a CONSUMER of that seam, not its author.

## Consequences

- A `--stack node` selection produces a verified TypeScript CLI + library Repo
  whose `pixi run lint`, `shipit test`, and `shipit build` work through the
  existing generic Tool interfaces and the existing `npm` leg.
- The generated Repo tracks a `package.json`, so the install reconciler delivers
  the managed `nodejs`/`pnpm` block automatically — no profile-specific
  provisioning path.
- The Node profile cannot verify until shipit#1083 lands dependency
  materialization; the profile change and #1083 are sequenced accordingly.
- `docs/spec/repo-new.md` is amended in the same planning change to list Rust,
  Node, and svelte-app as the supported stacks and to record the #1083
  dependency.
