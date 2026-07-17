# Repo New

## Context

shipit can create a complete local repository through `shipit repo new`, adopt
an existing repository through `shipit install`, reconcile its managed files,
provision detected toolchains through pixi, and expose generic Tool verbs such
as `shipit test` and `shipit build`. Repository creation currently stops at the
local boundary: it does not deliberately choose whether to leave the Repo local,
attach an existing GitHub Repo, or create and push a new private GitHub Repo.

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
- External commands cross one Exec seam and GitHub operations belong to the
  existing `gh` Tool adapter, which uses the authenticated `gh` CLI
  ([ADR-0028](../adr/0028-one-exec-seam-tool-adapters.md)).

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

The completed creator also exposes an accidental remote dependency. For example,
`shipit repo new --stack rust --no-remote hekka` can create and commit the Repo
successfully while its managed `post-commit` path emits `No such remote 'origin'`.
The hook is running before any remote exists, but a missing optional remote is
rendered like a creation failure.

Most new Repos should then be pushed so their generated CI can run, but Shipit
does not currently offer an explicit, safe remote policy as part of creation.
Users must arrange GitHub creation or reuse, attach `origin`, push `main`, and
discover the first Actions runs themselves. Local correctness and remote
publication also need distinct outcomes: a GitHub failure after local creation
must not invalidate or roll back the usable local Repo.

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
- Require every creation request to choose exactly one remote mode: remain local,
  reuse an existing GitHub Repo, or create a new private GitHub Repo.
- Keep local creation as the authoritative success boundary, completed before
  any remote operation and preserved regardless of later remote failure.
- Push `main` to `origin` for both remote modes without force-pushing or
  reconciling existing remote history.
- After a successful push, show the initial commit's visible GitHub Actions runs
  without waiting for them or making their discovery part of success.
- Make a missing `origin` a quiet, expected state for the managed post-commit
  path so a deliberately local Repo produces no remote lookup error.

## Non-Goals

- Waiting for the initial GitHub Actions runs to start or finish, or interpreting
  their eventual verdict as part of repository creation.
- Running `shipit gh-setup`, configuring branch protection, or otherwise
  applying post-creation GitHub policy.
- Creating public GitHub Repos or supporting a repository host other than
  GitHub.
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
shipit repo new --stack rust \
  (--no-remote | --remote-reuse OWNER/REPO | --remote-create OWNER/REPO) \
  <name> [parent]
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

Exactly one remote mode is also mandatory. `--no-remote` completes the local
Repo and performs no GitHub or remote operation. `--remote-reuse OWNER/REPO`
attaches the named existing GitHub Repo as `origin` and makes a normal
upstream-setting `git push -u origin main`. `--remote-create OWNER/REPO` creates
the named GitHub Repo as private, attaches it as `origin`, and pushes local
`main` with the same upstream-setting behavior.
The GitHub slug is explicit and authoritative: it must have the full
`OWNER/REPO` shape but its repository name may differ from the local project
name. Omission or combination of remote modes is a usage error.

Remote modes use shipit's existing `gh` Tool adapter and require an installed,
authenticated `gh` CLI for the GitHub API operations (Repo creation and lookup).
Attachment and push run through ordinary Git rather than the `gh` API, so they
additionally require a working Git credential for `origin`: an authenticated
`gh` API session does not by itself authorize `git push`. `origin` is attached
from the `OWNER/REPO` slug using the user's preferred Git protocol—the one
`gh config get git_protocol` reports, HTTPS or SSH—so the push is satisfied by
whatever credential that protocol needs: an existing SSH key, or an HTTPS
credential helper such as `gh auth setup-git` or another configured helper on a
laptop, or a token-bearing credential on a runner. A push that fails because
`gh` API authentication is present but no such Git credential is available is
reported as a remote-publication failure at the push stage, with recovery
guidance, exactly like any other push rejection. `--no-remote` requires neither
`gh`, GitHub authentication, nor a Git credential. Reusing a remote never
force-pushes, merges, rebases, or otherwise reconciles remote history; a normal
push rejection is reported as a remote-publication failure.

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
failed Check or local Git operation during this staged phase returns non-zero,
must not claim successful local creation, removes its temporary sibling on a
handled failure, and leaves the requested destination in its preflight state:
absent remains absent and an existing empty directory remains empty. A cleanup
failure is reported but never permits local publication.

Only after that atomic local publication succeeds does Shipit apply the selected
remote mode. Remote creation, attachment, push, and Actions discovery are never
part of the temporary staging transaction and never roll back or damage the
completed local Repo. Shipit also never deletes a GitHub Repo it created when a
later remote step fails. A remote failure produces a prominent warning naming
the failed stage and giving recovery commands, but the overall command exits
zero because local creation succeeded.

After a successful push, Shipit makes one best-effort query for Actions runs
associated with the initial commit and lists the runs currently visible,
including queued or running state. It does not wait or poll. GitHub may not have
registered a run yet, so no visible runs is informational; an unavailable or
failed query is a warning. Neither changes the successful push or command
outcome.

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
27. As a maintainer, I want every creation request to choose exactly one remote
    mode, so that automation never silently guesses whether or where to publish.
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
32. As a maintainer, I want `--no-remote` to complete without requiring `gh` or
    querying `origin`, so that a deliberately local Repo is a first-class result.
33. As a maintainer, I want to reuse an explicit `OWNER/REPO` as `origin` and
    push `main` normally, so that existing remote history is never overwritten
    or silently reconciled.
34. As a maintainer, I want to create an explicit private `OWNER/REPO` as
    `origin` and push `main`, so that generated CI can begin without separate
    GitHub setup steps.
35. As a maintainer, I want the remote slug to be independent of the local
    project name, so that GitHub naming does not constrain the local package.
36. As a maintainer, I want a GitHub or push failure to preserve local success,
    exit zero, and show recovery commands, so that I can finish publication
    without recreating the project.
37. As a maintainer, I want visible Actions runs listed after a push without
    waiting, so that I know CI is still progressing and retain control of the
    shell.
38. As a maintainer, I want an absent or not-yet-visible Actions run to be
    informational, so that GitHub registration latency does not turn a
    successful push into a false failure.
39. As a shipit operator, I want a no-origin post-commit event to stay quiet, so
    that optional remote state is not rendered as a local creation error.

## Design Decisions

- `repo` is a new top-level command group and `new` is its creation verb. The CLI
  module remains a thin parser and renderer over the repository-creation and
  post-creation remote-publication modules.
- The repository-creation module is deep: its small interface accepts the name,
  parent, and selected stacks and returns a typed creation plan/result. Callers
  do not coordinate validation, rendering, Git, install, verification, or commit
  steps themselves.
- Post-creation remote publication is a separate deep boundary over a completed
  local Repo and an explicit remote policy. It returns a typed publication and
  Actions-discovery result without changing the local creation result.
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
- Exactly one of `--no-remote`, `--remote-reuse OWNER/REPO`, or
  `--remote-create OWNER/REPO` is required. The explicit slug is validated as a
  full GitHub owner/repository pair and is not derived from the local project
  name.
- `--remote-reuse` attaches the existing GitHub Repo as `origin` and performs a
  normal upstream-setting push of `main`; it never force-pushes or reconciles
  remote history. `--remote-create` creates a private GitHub Repo, attaches it as
  `origin`, and performs the same push. `--no-remote` performs neither operation.
- Remote publication begins only after local atomic publication. Its failures
  never roll back the local Repo or a GitHub Repo, and are rendered as warnings
  with recovery commands while the creation command exits zero.
- A successful push is followed by one best-effort listing of Actions runs for
  the initial commit. Shipit neither waits for runs nor treats absence, query
  failure, or a later run verdict as repository-creation failure.
- GitHub API operations continue through the existing `gh` Tool adapter and its
  authenticated `gh` CLI, while attachment and push run through ordinary Git and
  additionally require a working Git credential for `origin`. Both are a
  dependency only of the two post-creation remote modes, not of local creation or
  `--no-remote`.
- Managed post-commit behavior treats a missing `origin` as an expected optional
  state and emits no user-facing error, preserving the validity of local-only
  Repos beyond this command.
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
- A local-success/remote-warning exit contract can surprise automation. Output
  and typed results must distinguish local completion, remote publication, and
  Actions discovery without implying that exit zero certifies all three.
- Existing GitHub Repos may contain history, branch rules, or naming conventions
  incompatible with the local root commit. Reuse must leave resolution to the
  user rather than expanding into force, merge, rebase, or branch-policy logic.
- GitHub Actions run registration is asynchronous. One best-effort read must not
  grow into polling, CI orchestration, or an unreliable assertion that no
  visible run means no workflow will run.
- Remote creation can succeed before attachment or push fails. Automatic
  deletion would risk destroying external state, so recovery must make this
  partial outcome explicit instead of attempting rollback.

## Cross-Cutting Concerns

- **Secrets and privacy:** creation never writes GitHub credentials. Remote modes
  rely on the user's authenticated `gh` session and existing Git credential for
  `origin`; `--remote-create` always creates
  a private Repo before pushing project content. Local environment files are
  ignored.
- **Git identity:** creation relies on normal user Git configuration and never
  writes identity or signing overrides into the generated Repo.
- **Observability:** local failure output identifies the creation stage and
  underlying Check or Git failure. Local success reports the destination and
  initial commit. Remote output separately reports creation/reuse, attachment,
  push, recovery after failure, and any Actions runs currently visible.
- **Reproducibility:** the generated pixi manifest and lockfile are tracked; CI
  and local commands use the Repo's pinned shipit launcher and locked
  environment.
- **Compatibility:** existing Repos and `shipit install` behavior remain valid.
  Adding `repo new` is additive; its consumer-owned build task and Rust test
  dependency do not alter the managed catalog reconciled into existing Repos.
  Internal reuse refactors are compatible only when regression tests preserve
  the behavior and output of every existing command they touch. Requiring an
  explicit remote mode is an intentional CLI contract change for `repo new`;
  existing local behavior remains available as `--no-remote`.
- **CI:** the caller is generic and uses the existing Lane/Tool model. The new
  Repo follows the existing pattern with required lint and test lanes only.
  Build support is available through the canonical build entry point and later
  artifact/release flows; creation verifies it, but no default PR build lane or
  Cargo command is added to workflow YAML. Remote modes push the initial commit
  and report the resulting Actions runs when visible, but do not wait for CI or
  judge its result.
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
- Assert the CLI requires exactly one remote mode, rejects omission and every
  combination, accepts a full `OWNER/REPO`, rejects malformed slugs, and permits
  the remote repository name to differ from the local project name.
- Test post-creation remote publication through its typed interface. For
  `--no-remote`, assert no `gh` lookup or remote mutation occurs. For reuse,
  assert `origin` is attached and `main` receives a normal upstream-setting push
  with no force or history-reconciliation path. For creation, assert the GitHub
  Repo is private, becomes `origin`, and receives the same push.
- Exercise failure at GitHub creation, remote attachment, and push, including a
  push that fails because `gh` API authentication is present but no Git credential
  for `origin` is available. In every case, assert the local Repo's `main` HEAD,
  committed contents, and working tree are unchanged and that no local or GitHub
  rollback is attempted; only successfully completed earlier bootstrap stages are
  retained rather than reverted—a creation failure leaves no new local state, an
  attachment failure retains any newly created GitHub Repo but does not require
  `origin` to exist, and a push failure leaves the successfully attached `origin`
  in place—while the command exits zero and output identifies the failed stage
  with actionable recovery commands that resume from it.
- After a successful push, cover visible queued/running Actions runs, no runs yet
  visible, and a failed status query. Assert Shipit makes no wait/poll request and
  that status discovery never changes the successful command outcome.
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
  Exercise content created concurrently before local publication and verify that
  shipit refuses to replace it.
- Exercise missing author identity and failing signing configuration through the
  normal commit seam; neither case may bypass Git policy or locally publish the
  Repo.
- Make a real commit in a generated Repo with no `origin` and assert the managed
  post-commit path is quiet. This regression must cross the installed hook seam,
  not merely mock the identity lookup that produced the original error.
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
- Add explicit post-creation remote policy, private GitHub creation/reuse,
  ordinary initial push, and best-effort Actions discovery through the existing
  GitHub and Git boundaries.
- Correct the shared no-origin post-commit path and verify it through a real
  generated-Repo commit.

These are decomposition hints only. `/to-tickets` will choose the actual epic
and Work Stream topology from the settled Spec and ADRs.

## Out Of Scope

- Interactive GitHub owner selection or inferred remote slugs; the full slug is
  always supplied explicitly.
- Public or otherwise configurable GitHub visibility; created Repos are private.
- Force-pushing, merging, rebasing, importing, or otherwise reconciling an
  existing remote's history.
- Waiting for Actions, interpreting workflow results, rerunning CI, or making CI
  green.
- Branch protection and other `shipit gh-setup` policy.
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

The Repo New Remote Bootstrap amendment extends the original local-only contract.
[ADR-0059](../adr/0059-repo-creation-publishes-by-atomic-rename.md) now limits
atomic publication to the completed local Repo, and
[ADR-0075](../adr/0075-repo-remote-bootstrap-is-post-creation-and-best-effort.md)
records why the required remote bootstrap follows local success, never rolls
back local or external state, and warns with recovery while exiting zero on
remote failure.
