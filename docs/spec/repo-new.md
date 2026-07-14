# Repo New

## Context

shipit can adopt an existing repository through `shipit install`, reconcile its
managed files, provision detected toolchains through pixi, and expose generic
Tool verbs such as `shipit test` and `shipit build`. It does not currently create
a new repository and prove that the repository is usable before its first
commit.

The existing domain model constrains this feature:

- A **Repo** is not a single project Kind. Its build-bearing paths form a
  path-to-toolchain map, and shipit dispatches each Tool per entry
  ([ADR-0007](../adr/0007-repo-as-path-toolchain-map.md)).
- Test and build are generic shipit Tool verbs. Toolchain-specific producing
  commands sit behind those verbs, while pixi provides their runtime
  ([ADR-0039](../adr/0039-tools-as-verbs.md)).
- The managed install set is reconciled from shipit's packaged desired state;
  consumer templates must not become a second copy of managed AGENTS content,
  skills, hooks, launchers, or toolchain blocks
  ([ADR-0003](../adr/0003-install-reconciliation-pull-not-push.md)).
- CI workflows are routing surfaces over declarations and generic Tool behavior,
  rather than places to reimplement stack logic
  ([ADR-0040](../adr/0040-workflow-blocks-invariants-in-blocks.md)).

This Spec defines the requirements and accepted design shape of repository
creation. Durable architectural rationale is recorded in
[ADR-0055](../adr/0055-repo-creation-orchestrates-install.md) through
[ADR-0063](../adr/0063-creation-profiles-are-a-closed-registry.md).

## Problem

Creating a new project today requires manually combining several independent
concerns: source layout, Cargo workspace configuration, pixi provisioning,
shipit policy, the managed agent harness, hooks, generic CI callers, Git ignore
rules, and the initial Git history. A missed step produces a repository that
appears initialized but fails its first lint, test, build, agent Run, or CI run.

The existing shipit Repo contract already defines how lint, test, build, CI, and
managed installation work. What is missing is a creator that writes the ordinary
consumer-owned declarations that contract expects—for example the Rust test
dependency, build task, and thin CI caller—then applies the existing managed
baseline. The feature is complete when the generated directory behaves like any
other working shipit Repo; creation must not invent parallel behavior for those
commands.

## Goals

- Provide one command that creates a new local Repo with a complete,
  shipit-managed development baseline.
- Make the creation interface multi-stack even though the first supported stack
  is Rust.
- Generate a Rust workspace containing one CLI crate and one library-only crate
  with deterministic names and working dependency wiring.
- Keep lint, test, build, and CI generic; stack-specific behavior belongs behind
  the Rust toolchain entry.
- Initialize Git on `main` and create exactly one initial commit containing the
  verified project, managed baseline, and reproducible pixi lockfile.
- Refuse any request whose destination is not absent or an empty directory, or
  whose derived names are invalid, before modifying existing content.
- Consider creation successful only after the generated Repo passes its lint,
  test, and build Checks.

## Non-Goals

- Creating a GitHub Repo, configuring a remote, pushing, or running
  `shipit gh-setup`.
- Configuring publishing, distribution endpoints, release secrets, or a release
  schedule.
- Supporting stacks other than Rust in the first release.
- Designing a general-purpose template marketplace or user-provided template
  language.
- Loading external Creation profiles, remote templates, plugin directories, or
  arbitrary template paths.
- Replacing `shipit install` or duplicating its managed catalog in scaffold
  templates.
- Generating a production application architecture beyond a minimal working
  library and CLI.
- Diffing a proposed scaffold against a non-empty destination or incrementally
  applying creation changes; that may be added as a separate later capability.
- A public `repo new --dry-run` mode. The internal plan remains directly
  testable, but v1 either creates and verifies the Repo or fails without
  publishing it.

## Proposed Shape

The public command is:

```text
shipit repo new --stack rust <name> [parent]
```

`parent` defaults to the current directory. The destination is always
`<parent>/<name>`; the command never guesses whether the positional path is a
parent or an exact destination. The parent must already exist as a writable
directory; a symlink resolving to such a directory is accepted, but creation
never creates missing parent directories. The destination may be absent or an
existing empty directory. Files, symlinks, and directories containing any
entry, including a hidden one, are refused. `--stack` is repeatable so the
creation request can later compose several toolchains, but at least one
selection is mandatory. V1 accepts only one effective profile, `rust`;
omission, unknown values, and duplicate selections are usage errors.

`<name>` uses canonical lowercase kebab-case: it begins with an ASCII lowercase
letter and continues with lowercase alphanumeric segments separated by single
hyphens. The destination, CLI package, and executable keep that spelling. The
library package is `lib<name>`; Rust source refers to it through Cargo's normal
hyphen-to-underscore crate identifier conversion. Names rejected by the managed
Cargo toolchain are also refused rather than silently rewritten.

The command creates a virtual Cargo workspace root—the root manifest is not a
package—with two members whose paths mirror their package names:

- `crates/<name>/`: the `<name>` CLI package and executable;
- `crates/lib<name>/`: the `lib<name>` library-only package used by the CLI.

The workspace uses Cargo resolver `3`, Rust edition `2024`, initial version
`0.1.0`, and inherited MIT package metadata. The CLI's path dependency on
`lib<name>` is the only runtime dependency; the scaffold adds no argument,
error, logging, or test framework crates.

Pixi is the sole Rust provisioning authority. The unchanged install reconciler
detects the generated Cargo manifest and delivers its managed Rust toolchain;
the Rust Creation profile adds `cargo-nextest` to the consumer-owned pixi data,
and the committed lockfile resolves both. The scaffold contains no
`rust-toolchain.toml`, rustup bootstrap, `cargo install`, or other runtime tool
installation path.

The Rust Creation profile also declares the CLI as the Repo's Artifact, with one
Rust build target naming package `<name>`. It declares no endpoint, Bundle,
signing, or release policy; the declaration only gives the existing build and
future artifact flows an unambiguous primary product.

The universal scaffold includes a minimal `README.md` naming the project and
listing `pixi run lint`, `pixi run test`, and `pixi run build`. It also includes
the canonical MIT text in `LICENSE`, using the creation year and the user's
resolved Git author name; Rust package metadata declares `MIT`. V1 offers no
license selection, badges, or repository URLs.

The library supplies the hello-world value and the CLI prints it. The generated
project contains one black-box test that runs the CLI and asserts its output,
thereby exercising the binary, its library dependency, and the configured Rust
test runner together.

Creation combines two ownership layers:

1. The repository-creation module writes consumer-owned source, manifests,
   policy declarations, the thin generic CI caller, and baseline ignore rules.
2. The existing install module writes and records the shipit-managed catalog,
   including agent guidance, skills, launchers, hooks, lint configuration, pixi
   integration, and conditional Rust provisioning.

The resulting Repo tracks `pixi.toml`, `pixi.lock`, `.shipit.toml`, source,
tests, CI configuration, and managed Claude/Codex configuration. It ignores
build environments and outputs rather than the configuration needed to
reproduce them.

The universal consumer-owned `.gitignore` seed contains:

```text
.DS_Store
*.swp
*~
.pixi/
.direnv/
.env
.env.*
!.env.example
.claude/worktrees/
.todos.db
node_modules/
.npm/
.pnpm-store/
coverage/
__pycache__/
*.py[cod]
.venv/
*.egg-info/
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage
htmlcov/
```

The Rust Creation profile adds `/target/`. Ecosystem lockfiles are never
ignored. Broad product-output directories such as `dist/` are not guessed; the
existing install module adds its own managed release-output block.

Creation builds the complete Repo in a temporary sibling under the requested
parent, keeping staging and destination on one filesystem. It installs managed
state, generates the lockfile, runs the canonical lint, test, and build Checks,
and creates the `Initial commit` there. Only after every step succeeds does one
atomic rename publish the temporary Repo at the requested destination. Any
failed Check or Git operation returns non-zero, must not claim successful
creation, removes its temporary sibling on a handled failure, and leaves the
requested destination in its preflight state: absent remains absent and an
existing empty directory remains empty. A cleanup failure is reported but never
permits publication.

Certification runs from a clean child shell whose working directory is the
staged Repo and whose inherited pixi project-selection state cannot point back
to the invoking checkout. The child materializes the staged Repo's pixi
environment and lockfile, then invokes `pixi run lint`, `pixi run test`, and
`pixi run build` exactly as a user would. `pixi run` provides non-interactive
environment activation; creation does not call Tool implementations directly or
depend on an interactive `pixi shell`.

## User / Agent Stories

1. As a maintainer, I want to create a new shipit-managed Repo with one command,
   so that the project is complete before development begins.
2. As a maintainer, I want to supply a project name and optional parent
   directory, so that the destination is predictable.
3. As a maintainer, I want the parent required to exist and be writable, so that
   repository creation never invents surrounding directory structure.
4. As a maintainer, I want the destination to always be `parent/name`, so that a
   path is never interpreted ambiguously.
5. As a maintainer, I want an absent or empty-directory destination accepted and
   every other existing path refused, so that an intentionally prepared empty
   location is usable without risking existing content.
6. As a maintainer, I want invalid project and derived crate names refused with
   an actionable error, so that creation cannot leave an unusable Cargo
   workspace.
7. As a maintainer, I want stack selection to be repeatable, so that the command
   can later describe a multi-toolchain Repo without a breaking interface
   change.
8. As a maintainer, I want unsupported stacks rejected explicitly, so that the
   command never silently omits requested project capabilities.
9. As a maintainer, I want at least one Creation profile required and duplicate
   selections rejected, so that the creation request is explicit and accidental
   repetition is not hidden.
10. As a Rust developer, I want a workspace whose CLI package is `<name>`, so that
    the installed executable has the project name.
11. As a Rust developer, I want the library-only package to be `lib<name>`, so
    that its role is explicit and stable.
12. As a Rust developer, I want the CLI to consume the library, so that the
    generated crates demonstrate the intended dependency direction.
13. As a Rust developer, I want one black-box hello-world test, so that the
    minimal project proves the executable and library wiring without placeholder
    test noise.
14. As a contributor, I want pixi to provision every tool required by the
    generated Checks, so that no ambient Rust or test installation is assumed.
15. As a contributor, I want the CLI declared as the Repo's Artifact, so that
    `shipit build` targets the primary product without implying publication.
16. As a contributor, I want `pixi run lint`, `pixi run test`, and
    `pixi run build` to work immediately, so that local and CI entry points are
    discoverable and consistent.
17. As a contributor, I want the pixi lockfile committed, so that a fresh clone
    resolves the environment selected at creation time.
18. As a contributor, I want generated build and environment directories
    ignored, so that running the Checks does not dirty the Repo.
19. As a shipit operator, I want release-stage output ignores to remain managed
    by `shipit install`, so that repository creation does not fork managed
    policy.
20. As a shipit operator, I want agent definitions, skills, hooks, and launchers
    sourced from the install catalog, so that later reconciliation has one
    authority.
21. As a CI maintainer, I want a thin stack-neutral workflow caller, so that Rust
    behavior remains in declarations and Tool adapters rather than YAML.
22. As an agent, I want the generated Repo to carry shipit's Role guidance and
    enforcement from its first commit, so that its first development cycle uses
    the normal delegated lifecycle.
23. As a maintainer, I want Git initialized on `main`, so that the new Repo starts
    with the portfolio's expected primary branch name.
24. As a maintainer, I want one `Initial commit` only after all Checks pass, so
    that the root commit is known-good evidence rather than an unverified
    scaffold.
25. As a maintainer, I want Git identity or commit failures reported clearly, so
    that the command never mistakes an uncommitted tree for a completed Repo.
26. As a maintainer, I want a minimal README and an MIT license attributed to my
    resolved Git author name, so that the initial Repo is documented and licensed
    without placeholders or interactive choices.
27. As a maintainer, I want creation to remain local, so that choosing a GitHub
    owner, visibility, and remote policy can be a separate future operation.
28. As a future stack author, I want project-specific source generation separate
    from the universal managed baseline, so that a new stack composes with
    shipit instead of copying it.
29. As a future multi-stack user, I want one Repo-level test/build interface to
    fan out over declared toolchains, so that adding a stack does not multiply
    top-level workflows.
30. As a maintainer, I want the finished Repo published at its destination in
    one atomic filesystem operation, so that an interrupted creation never
    exposes a partial project at the requested path.
31. As a future maintainer, I want diff-based creation into a non-empty path
    treated as a separate capability, so that v1's safety contract remains
    simple and unambiguous.

## Design Decisions

- `repo` is a new top-level command group and `new` is its creation verb. The CLI
  module remains a thin parser and renderer over the repository-creation module.
- The repository-creation module is deep: its small interface accepts the name,
  parent, and selected stacks and returns a typed creation plan/result. Callers
  do not coordinate validation, rendering, Git, install, verification, or commit
  steps themselves.
- The public test seam and the module interface are aligned. Tests may inspect a
  plan without effects and exercise completed creation through the same module;
  effectful command tests observe outcomes rather than internal helper calls.
- The CLI does not expose the internal plan as a dry-run preview in v1; such a
  preview could not certify dependency solving, installation, hooks, or Checks
  and would offer weaker evidence than atomic creation.
- A Repo remains a path-to-toolchain map. `--stack` describes creation input; it
  does not add a persistent whole-Repo Kind used for Tool dispatch.
- Creation profiles form a closed, shipit-owned registry keyed by the accepted
  `--stack` values. Adding a profile is a reviewed shipit change with fixtures,
  not runtime plugin discovery or user-supplied template execution.
- The Rust workspace contributes one Rust Tool leg at the workspace root. The
  two Cargo members are not separate Rust legs.
- The Rust workspace root is virtual rather than a package. Member paths mirror
  their package names at `crates/<name>/` and `crates/lib<name>/`, keeping the
  layout deterministic and leaving the root available for additional members.
- Cargo resolver `3`, edition `2024`, version `0.1.0`, and MIT licensing live in
  workspace metadata and are inherited by both members. The CLI-to-library path
  dependency is the only runtime dependency in the generated graph.
- The Rust Creation profile declares one Artifact named `<name>` whose Rust
  build target names package `<name>`. The Artifact has no distribution endpoint
  or release behavior in v1.
- Project names use lowercase kebab-case. The external name is preserved for the
  destination, CLI package, and executable; `lib` prefixes the library package,
  and only Rust's import identifier applies Cargo's normal hyphen-to-underscore
  conversion. Creation never performs any other silent normalization.
- Shared configuration is composed once. Stack contributions must not append
  competing textual definitions of the same `.shipit.toml` or `pixi.toml`
  tables.
- Consumer-owned scaffolding and shipit-managed installation remain distinct
  ownership classes. Managed files and blocks are obtained through the existing
  install module and remain reconcilable after creation.
- Repository creation consumes the existing shipit Repo contract; it does not
  change Tool commands, Lane or workflow semantics, install behavior, or the
  managed units delivered to existing Repos. Internal modules, registries, and
  data may be refactored or extracted where that lets creation reuse existing
  Rust and Toolchain knowledge rather than copy it, provided existing commands'
  observable behavior and installed output remain identical.
- Test and build stay generic Tool interfaces. Rust's default producing commands
  remain the Rust toolchain behavior, and pixi must provision every binary those
  commands require.
- The consumer-owned universal creation seed declares the generic build pixi
  task, parallel to the existing managed lint and test entry points. Repository
  creation does not broaden the fleet-wide managed task catalog.
- The Rust Creation profile contributes `cargo-nextest` to the generated Repo's
  default pixi environment, where the existing managed test task runs. It does
  not change Rust provisioning for existing Repos.
- Pixi and `pixi.lock` are the only Rust provisioning authority. The profile
  relies on the existing install reconciler's managed Rust block and contributes
  only the test runner missing from consumer data. It generates no rustup
  configuration or runtime self-install command, preventing a second toolchain
  version from competing with the environment used by shipit.
- The generated CI caller delegates to shipit's reusable checks workflow and
  carries no Rust-specific commands.
- Creation initializes the local Repo on `main` and creates a root commit named
  `Initial commit` only after canonical verification passes.
- The initial commit uses the user's normally resolved Git author identity,
  signing configuration, and installed hooks. Creation does not synthesize an
  author, disable signing, or bypass hooks; any resulting Git failure prevents
  publication and is reported unchanged with creation context.
- The universal scaffold renders a minimal README and the canonical MIT license
  text. The license copyright holder is the resolved Git author name and its year
  is the local creation year; Rust package metadata declares `MIT`. Missing Git
  identity is a preflight failure, not a template placeholder.
- Verification crosses the user's public shell seam: a clean child rooted in
  the staged Repo materializes that Repo's pixi environment and runs its public
  lint, test, and build tasks. Inherited activation from the invoking Repo must
  not select a different manifest or environment.
- Creation stages the entire Repo in a temporary sibling and publishes it with
  one same-filesystem atomic rename. The requested destination must be absent or
  an empty directory at preflight and must still satisfy that condition at
  publish time; creation never merges into or replaces content.
- The consumer-owned ignore seed covers, at minimum, Rust `target/`, `.pixi/`,
  local environment files, common OS files, agent worktrees, and common
  cross-stack dependency/cache paths. The universal set includes
  `node_modules/`, Python `__pycache__/`, bytecode, virtual environments, and
  standard test/type/lint caches; the Rust profile adds `/target/`. Lockfiles
  remain tracked, broad product-output patterns such as `dist/` are not guessed,
  and the install module remains responsible for its managed release-output
  ignore block.
- Rendering technology, rollback mechanics, the verification seam, and internal
  profile representation follow ADR-0055 through ADR-0063; lower-level effect
  injection remains an implementation detail within those boundaries.

## Alternatives Considered

- **Persist a Repo Kind such as `rust-project`.** Rejected because it contradicts
  the existing path-to-toolchain model and prevents natural multi-stack
  composition.
- **Generate Rust-specific workflows.** Rejected because CI is a routing surface;
  stack behavior already belongs behind Tool declarations and adapters.
- **Copy the managed shipit baseline into a project template.** Rejected because
  copied files would drift from the install catalog and lose reconciliation
  ownership.
- **Treat the path argument as an exact destination.** Rejected because an
  explicit project name plus an ambiguous path makes automation harder to read;
  `parent/name` has one interpretation.
- **Create the GitHub Repo as part of v1.** Rejected to keep local project
  correctness separate from owner, visibility, authentication, and remote
  policy.
- **Generate many example tests.** Rejected because one black-box hello-world
  test proves the intended wiring with less placeholder code to delete.

## Risks And Rabbit Holes

- Rust package names, executable names, and Rust import identifiers have related
  but non-identical validation and normalization rules. Silent normalization
  could violate the promised crate names.
- A workspace with several paths mapped independently to the same toolchain can
  make artifact target selection ambiguous. The generated workspace must remain
  one Rust leg.
- Running a test command that depends on `cargo-nextest` without provisioning it
  recreates the ambient-machine failure this feature is intended to remove.
- A pixi task name defined in multiple enabled environments becomes ambiguous.
  Adding build or Rust test provisioning must preserve unambiguous bare commands.
- Git initialization, hook activation, dependency solving, verification, and
  committing cross several effectful systems. ADR-0059 requires handled failures
  to clean up the temporary sibling without weakening the promise that the
  requested destination retains its absent-or-empty preflight state on failure.
- Template extensibility can become a product of its own. V1 needs only the
  universal seed and one Rust contribution.
- Live environment verification is more expensive than fixture-only tests, but
  fixture-only confidence cannot prove a fresh machine is provisioned correctly.

## Cross-Cutting Concerns

- **Secrets and privacy:** creation writes no credentials, creates no remote, and
  sends no project content to GitHub. Local environment files are ignored.
- **Git identity:** creation relies on normal user Git configuration and never
  writes identity or signing overrides into the generated Repo.
- **Observability:** failure output identifies the creation stage and underlying
  Check or Git failure. Success reports the destination and initial commit.
- **Reproducibility:** the generated pixi manifest and lockfile are tracked; CI
  and local commands use the Repo's pinned shipit launcher and locked
  environment.
- **Compatibility:** existing Repos and `shipit install` behavior remain valid.
  Adding `repo new` is additive; its consumer-owned build task and Rust test
  dependency do not alter the managed catalog reconciled into existing Repos.
  Internal reuse refactors are compatible only when regression tests preserve
  the behavior and output of every existing command they touch.
- **CI:** the caller is generic and uses the existing Lane/Tool model. The new
  Repo follows the existing pattern with required lint and test lanes only.
  Build support is available through the canonical build entry point and later
  artifact/release flows; creation verifies it, but no default PR build lane or
  Cargo command is added to workflow YAML.
- **Performance:** creation may perform a cold pixi solve and Rust compilation.
  Correctness and reproducibility take precedence over optimizing the first run;
  subsequent runs should use ordinary pixi and Cargo caches.

## Testing / Verification

- Test the repository-creation module through its interface: valid planning,
  existing/writable parent validation and symlink resolution, derived
  destinations and crate names, required/repeatable stack input, duplicate and
  unsupported stacks, invalid names, acceptance of absent and empty-directory
  destinations, and refusal of files, symlinks, and non-empty directories.
- Cover the naming grammar at its edges: one-letter names, digits after the first
  letter, multi-segment kebab names, and rejection of uppercase, underscores,
  whitespace, leading digits, empty segments, and Cargo-reserved names.
- Test the public command in a temporary parent directory. Assert the generated
  workspace and consumer-owned configuration, managed install results, Git
  `main` branch, one root commit named `Initial commit`, and a clean working
  tree.
- Assert the generated Artifact selects the CLI package and that no publishing,
  Bundle, signing, or endpoint configuration is present.
- Assert Rust and `cargo-nextest` resolve from the generated pixi environment and
  that no rustup or Cargo self-install surface is present.
- Assert ignore behavior through Git rather than string matching alone: Rust and
  pixi outputs plus representative Node and Python dependency/cache paths are
  ignored, while manifests, ecosystem lockfiles, source, and managed agent
  configuration are tracked.
- The scaffold contains exactly one Rust test. It executes the generated CLI and
  asserts `Hello, world!`; the CLI obtains that value from `lib<name>`.
- Verify the generated Repo with its public commands:
  `pixi run lint`, `pixi run test`, and `pixi run build`.
- Run those commands from a clean child process rooted in the staged Repo and
  seed the test process with conflicting parent pixi activation variables to
  prove they are removed or overridden rather than accidentally reused.
- Exercise failure paths for install, each Check, lockfile generation, and Git
  commit. Every failure returns non-zero, emits no success result, and leaves the
  requested destination absent or preserves its original empty directory.
  Exercise content created concurrently before publication and verify that
  shipit refuses to replace it.
- Exercise missing author identity and failing signing configuration through the
  normal commit seam; neither case may bypass Git policy or publish the Repo.
- Assert the README names the project and its canonical commands, the MIT text
  carries the creation year and Git author name, and Rust metadata declares the
  same license. No badge, remote URL, or alternate-license prompt is generated.
- Reuse the existing temporary-consumer install tests and CLI runner patterns as
  prior art. Add fresh-environment acceptance evidence so mocked execution cannot
  hide a missing generated dependency such as `cargo-nextest`.

## Workstream Hints

- Establish the creation plan/result and thin CLI seam.
- Add the Rust workspace contribution and consumer-owned configuration
  rendering.
- Generate the ordinary consumer-owned build/CI declarations and Rust test
  dependency expected by the existing Repo contract.
- Integrate Git initialization, canonical verification, and the initial commit;
  finish with a fresh-environment acceptance pass.

These are decomposition hints only. `/to-tickets` will choose the actual epic
and Work Stream topology from the settled Spec and ADRs.

## Out Of Scope

- GitHub owner selection, repository visibility, remote creation, push, and
  branch-protection setup.
- Release artifact publication or endpoint configuration.
- User-selectable licenses, CI providers, repository hosting providers, or
  application frameworks.
- Interactive prompting; v1 is explicit and automation-friendly.
- Updating existing Repos through `repo new`; reconciliation remains
  `shipit install`.
- Additional stack implementations, even though the creation interface must not
  preclude them.

## Further Notes

This Spec intentionally preceded its ADR grill as an experiment in locking the
requirements and observable shape before selecting hard-to-reverse
implementation details. ADR-0055 through ADR-0063 explain the chosen
architecture without turning this Spec into an implementation transcript. If a
later ADR changes an observable requirement, the Spec must be amended explicitly.
