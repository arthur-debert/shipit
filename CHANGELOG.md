<!-- generated - do not edit; fragments live in CHANGELOG/ (`shipit changelog render` regenerates this file) -->

# Changelog

## Unreleased

## 1.7.0 - 2026-07-29

- install: a consumer key shadowing a managed pixi block now **refuses** instead
  of warning and exiting 0 (#1116). A block that could not be **delivered** was
  being treated as a block that did not need delivering — the same shape as #1101
  one layer up — so the repo silently under-delivered its managed set (a pin, a
  task: the guard is not pin-specific) while the reconcile reported success.
  16 of 20 portfolio repos were in exactly that state, all over pins; three
  found it only by hand-inspecting a v1.6.0 bump.
- The supported override is **declaring** it: `[managed.decline].keep` in
  `.shipit.toml` already exists for precisely this, and a declined block is no
  longer a conflict at all (it will never be spliced), so the intent lands in
  version control where it is reviewable instead of in a warning line nobody acts
  on. The refusal names the key, the block, and both remedies — deletion first,
  because that is the right answer for most of them: 13 of the 16 colliding repos
  carried a stale hand-pin whose own comment named the shipit gap the managed
  block has since closed.
- Scoped to KEY conflicts. `PixiTaskConflict` stays warn-only — shipit's own repo
  carries a deliberate, documented `test`-task conflict, so refusing there would
  make shipit refuse to install itself (a dogfood test now pins that) — and
  `PixiTableConflict` stays warn-only with a fleet-wide count of zero rather than
  shipping an untested refusal. Both are stated in the code, not left implicit.
- Every applying mode fails closed, the working-tree refresh included: like the
  `provision lexd` tripwire, the finding is about the state of the consumer's
  manifest rather than about publishing. The guard runs before the no-op
  shortcut, because the common shape is a repo whose managed set is otherwise
  current — the plan carries no work and would otherwise exit 0.
- review: the local-agent reviewers (`codex` / `agy`) now work from a consumer
  repo's pinned shipit. PyJWT — the App-JWT signer the whole App-auth path rests
  on — moves from the optional `review` extra into shipit's base dependencies, so
  it rides every install rather than only a checkout that opted into an extra. The
  `review` extra is gone with it, as is the `pr next` fallback that caught a
  "missing PyJWT" error, sniffed the message for it, and re-ran itself inside a
  `pixi run -e review` environment that exists only in shipit's own repo (in a
  consumer it failed with `unknown environment 'review'`). Consumers could not
  require or drive codex/agy at all; their PRs got a single-reviewer net.
- verify-apps: the verb now tells the three situations apart instead of reporting
  them all as NOT LIVE. A machine with no App credentials — no `doppler`, no
  login, no key — gets `UNVERIFIED` and exit 2, because nothing was ever asked of
  GitHub and the Apps' real state is unknown; a repo whose owner has not installed
  the App (or has not consented to `checks: write`) still gets `NOT LIVE` and exit
  1; all-live is exit 0. The old output claimed the Apps were missing from repos
  where they were in fact live, which is a claim about a repo drawn from a fact
  about a laptop. The expected credential gap also stops spraying a traceback per
  App, and the printed verdict and the exit code are now derived from one decision
  so they cannot drift.
- review: `ReviewAuthError` carries the KIND of failure (credentials unavailable
  here / App not installed there / the probe itself failed) and the HTTP status
  when there was one. The "App is not installed" branch reads that status instead
  of grepping `"HTTP 404"` out of the rendered message, so an unrelated error
  whose body quotes a 404 can no longer be reported as a missing installation.
  An answer that arrives but is unusable — a success response that is not JSON,
  is not strictly valid UTF-8, or whose installation `id` is not a positive
  integer or whose `token` is not a non-empty string — is reported as a failed
  probe too, rather than escaping as a raw decode error or being coerced into a
  usable-looking value; `verify-apps` reports UNVERIFIED for it instead of
  printing a traceback or a verdict drawn from a corrupted credential.
- review: an App-auth failure no longer quotes the body of the endpoint that
  answers with a credential. `POST /app/installations/{id}/access_tokens` returns
  a live `ghs_…` token, and an unusable answer from it — one invalid byte beside
  an intact token, or JSON truncated after one — used to be excerpted into the
  error, which `verify-apps` prints and logs with the traceback attached. Those
  failures now report the body's size and the response's shape instead. Bodies
  that cannot carry a credential (the installation-metadata `GET`, and any error
  body, whose request minted nothing) are still quoted, and every quoted body is
  capped to one line.
- portfolio: `lex-fmt/nvim` and `lex-fmt/zed-lex` join the `[project.portfolio]`
  fleet manifest. Both were excluded as "editor-config / grammar-packaging repos"
  while both in fact carry a `.shipit.toml` with a `[shipit] version` pin and
  managed blocks in their `pixi.toml` — they are adopted consumers, so every
  fleet-wide operation driven off the manifest (the sweep, a reconcile roll)
  silently skipped them. `zed-lex` is the worst case: 14 releases behind, no
  `[lanes]`, no `[artifacts]`, and inside the `provision lexd` refusal set, so it
  is the repo least likely to be noticed drifting. Membership in the manifest is
  ADOPTION — a `[shipit] version` pin — not repo genre; the exclusion note now
  names only `lex-fmt/comms`, which carries neither manifest on `main`.
- install/reconcile: the two pixi guards that judged the consumer's `pixi.toml`
  now judge the manifest the reconcile will **leave behind**, not the one on
  disk (#1127, #1081). Both ran inside `gather`, against the pre-update text,
  before the plan's decisions existed — the same defect in two places.
- The `provision lexd` tripwire was **blocking its own remedy**. 9 of the 13
  known fleet call sites are the `provision-lexd` task line *inside* the
  shipit-managed `[tasks]` span, whose desired content no longer defines it —
  so the reconcile would have deleted them itself. Instead `shipit install`
  refused, and the refusal named a line inside a block marked *"do not edit;
  regenerate via `shipit install`"*, instructing the operator to hand-edit
  managed content. The finding is now decided over the **projected** manifest,
  so those repos self-heal on the pin bump. A call the reconcile cannot reach —
  the consumer's own `feature.lint.tasks.lint-full` lane wiring, or a call
  inside a **declined** block — still fails closed, unchanged.
- The managed-key exemption behind the `rattler-build` migration (#1071) now
  exempts only keys that **depart** a managed block this plan actually rewrites.
  Before, any key in a present managed span was exempt — including one in a
  block the consumer **declined**, which `reconcile()` drops entirely. That let
  the conda-packager block splice a second `rattler-build` into
  `[dependencies]`, producing an unparseable `pixi.toml`. The declined donor now
  keeps its key a conflict and the receiving block is skipped.
- `ConsumerState` carries the raw `pixi.toml` text; the detections moved into
  `reconcile()`, following the `_plan_lefthook_conflicts` precedent. The prose in
  `reconcile.py`, `apply.py` and the `provision` tombstone that asserted every
  call site was consumer-authored and outside every managed block is corrected —
  it was false for 9 of 10 repos, and it is why the guard was built this way.
- install: the `.shipit-skills/*` files shipit delivered before the skills
  projection are now **retired** — `shipit install` deletes a pristine copy on
  the reconcile that crosses the pin (#1115). The projection (#1088) repointed
  the managed set at `.agents/skills/` but left the previously-delivered copies
  tracked in every consumer, owned by nobody: two divergent copies of each skill,
  with the **unmanaged** one carrying the stale instructions agents still read
  (the pre-#1066 copies say `shipit lint --fix` where the current instruction is
  `pixi run lint --fix`). Two consumers had already hand-cleaned it; every repo
  crossing the pin was going to pay the same tax. A hand-EDITED copy matches no
  known shipped version and is kept with a warning, as always.
- install: the skill store moved from the repo root (`.shipit-skills/`) to
  **`src/shipit/data/skills/`** — the subtraction that makes the retirement safe.
  A retired-files match is on content hash and is GLOBAL, with no producing-repo
  exemption, so with the store at the repo root shipit's own self-install would
  have deleted its own source. Moving it under `src/` drops the wheel
  `force-include` hack (the store is ordinary package data now) and the dual-root
  fallback in `skills_root()`, and leaves the retired path live nowhere. A new
  guard fails any future manifest entry that names a path still live in shipit's
  own tree. ADR-0077 is amended explicitly, not silently contradicted.
- install: the managed `.markdownlintignore` no longer narrates the retired
  `.shipit-skills/` path as "the managed tree" — a consumer could not fix that
  themselves, since editing a managed file surfaces as an OVERRIDE.

## 1.6.1 - 2026-07-28

- release/bundle: the `vsix` composition now packs with
  `vsce package --no-dependencies` (#1058). shipit's VS Code consumers are
  esbuild-**bundled** extensions — their runtime dependencies already ride inside
  the bundle — so vsce's `npm ls` dependency walk contributes nothing but failure
  modes. With a workspace symlink dependency (`"@lex/shared": "file:./shared"`)
  that walk either yields an empty file set, composing a hollow `.vsix` that
  looks like a successful build, or kills the pack outright with
  `Extension entrypoint(s) missing …`. The latter is what left `lex-fmt/vscode`
  with no working release path at all: the argv is constructed inside the
  composition, so the consumer could not work around it.
- The flag is **unconditional** — no bundled-case detection, no config knob. One
  code path and one behaviour to reason about, and the dependency walk has no
  case in which it helps a bundled extension.

## 1.6.0 - 2026-07-27

- release/bundle: the `tarball` and `zed` compositions ship a **producer-declared
  payload** (ADR-0077, #1092). "Which files make up my package" is a
  producer-repo fact, so it is declared in the producing repo's own
  `.shipit.toml` — `bundle = { composition = "tarball", leg = "<toolchain>",
  payload = [{ path = "src", required = true }, { path = "queries" }] }` — and
  shipit tars exactly that under the named `[toolchains]` leg. `path` may be a
  file, a directory, or a nested path, and may name a build-produced file as
  readily as a committed one. An entry rides **when present** unless it declares
  `required = true`; a missing required entry is a loud bundle-stage failure.
- shipit no longer carries a built-in file list for either composition: the
  hardcoded `TREE_SITTER_PAYLOAD` / `ZED_PAYLOAD` tuples and the hardcoded
  `tree-sitter` / `rust` leg lookups are gone. A grammar that wants to ship one
  more file is now a config edit in its own repo, not a shipit release. The two
  compositions share one compose function and differ only in the publish
  endpoint they pair with (`zed`'s registry PR, ADR-0068).
- **No backwards compat (ADR-0077):** a `tarball`/`zed` artifact with no `leg` +
  `payload` is refused at config-parse with a migration-pointing message, never
  a silent fallback to the old shipit-side list. A `payload` in which no entry is
  `required = true` is refused too — an all-when-present payload could compose an
  empty archive, which is never a quiet outcome. `leg`/`payload` on any other
  composition is refused (those assemble their own contents). Two payload entries
  that name the same archive member — the same path, or one containing the other
  (`src` plus `src/parser.c`, which a directory operand already ships) — are
  refused: each member is declared exactly once.
- **The declared payload — AND the leg it rides under — are untrusted data.** The
  bundle stage refuses any symlink or junction on the whole path from the checkout
  root down: the `bundle.leg` [toolchains] directory itself, then each payload
  entry, walked one component at a time from the trusted checkout root (never from
  a base derived by resolving an untrusted path). So a committed `leg -> /etc` or a
  `leak -> /etc` payload component is refused, naming the offending component,
  before it can steer the archive at a host file — the bundle archives only real
  files and real directories, the same refuse-links, anchor-from-root model
  `shipit stage` uses, now via one shared primitive. Payload operands are also
  fenced from `tar`'s option list, so a path spelled like a flag is always read as
  a path. A payload entry of `.` (or `./`, `./.`) — which names the leg dir itself,
  not a member — is refused at config-parse.
- release/publish: the `zed` endpoint reads `extension.toml` from the leg the
  `zed` **bundle declared**, not from a hardcoded `rust` leg, and refuses a
  declaration that does not carry that manifest as a required payload entry — the
  archive and the rendered registry row always describe one extension. An
  artifact with the `zed` endpoint and no bundle still reads the crate's `rust`
  leg.
- docs: `docs/dependencies.md` is now the single, authoritative statement of the
  cross-repo dependency model (a plain conda package: channel, name,
  arch-dependent-or-universal, producer publishes, consumer pins). It declares the
  2026-07-20 reset and names the retired mechanisms explicitly, so a reader can tell
  current guidance from stale guidance without archaeology.
- install: an unparseable `.shipit.toml` now **refuses the run** instead of
  degrading to an empty unit set (#1101). `shipit install` caught the
  `ConfigError` from the `[artifacts]` parse and reconciled the repo as if it
  declared no signals and no endpoints — dropping exactly the managed blocks its
  artifacts gate — while exiting 0. Unit absence never *deletes* a block
  (removal rides the explicit retired list), so those blocks stayed on disk,
  fell out of the managed set, and went stale silently. A config the tool cannot
  parse is not a config declaring nothing: the parse error, whose message is
  already migration-pointing, now reaches the user as `error: …` + exit 1.
  Concretely, a `tarball` artifact predating the producer-declared payload
  (#1092) would have un-managed its repo's `tree-sitter-cli` toolchain block and
  its `rattler-build` conda-packager block on the next reconcile.
- The same posture now covers all three config reads in the install verb
  (declared signals, declared endpoints, `[artifact-deps]` projection), so
  install has ONE answer for a config it cannot parse. An **absent** config is
  unchanged — a missing map declares nothing and installs cleanly.
- lint: the public `pixi run lint` now runs in the **pinned lint environment**
  (#1066). The managed task moved out of the default `[tasks]` table into
  `[feature.lint.tasks]`, so it rides the one environment your manifest composes
  that feature into and a bare `pixi run lint` resolves there — the same task, same fleet-pinned
  toolchain, that the commit/push hooks (`pixi run -e lint lint`) and the CI
  lint lane run. `pixi run lint --fix` is the fixer on that same pin.
- Why it mattered: a task in `[tasks]` reaches every environment, and the
  default one carries whatever linter versions a repo happens to resolve — so
  the documented public command disagreed with the gate it claimed to
  reproduce. On 2026-07-19 that misclassified four already-clean consumer
  reconcile PRs as prettier debt (default-env prettier 3.9 vs the pinned 3.8),
  and running the fixer that way reformatted correct files into a state the
  hook then rejected.
- **No compatibility path (one lint entry point):** the `lint-full` twin is
  retired. `shipit repo new` no longer seeds it, and shipit's own lint lane runs
  `lint`; the `fmt` alias is gone in favour of `pixi run lint --fix`. A
  consumer's next `shipit install` reconcile migrates the task in one pass —
  the default-`[tasks]` line is removed and the `[feature.lint.tasks]` block
  added together, so there is never a window with both live. A consumer that
  still carries a hand-written `lint-full` task keeps it (it is consumer-owned
  scaffold, not managed); it is now a redundant alias and can be deleted.
- Role prompts, the managed `AGENTS.md` block, the install PR text, ADR-0004,
  and the lint docstrings all name the one contract: `pixi run lint` /
  `pixi run lint --fix`, never a bare `shipit lint`.
- install: `shipit install` now refuses a repo whose `pixi.toml` still calls the
  retired `shipit provision lexd` (ADR-0066), naming each offending task, its
  table, and the replacement (lexd rides the managed `[feature.shipit-lexd]`
  block, already on PATH in the lint env). These call sites are consumer-authored
  lane tasks (`lint-full`, `test-full`) that live outside every managed block, so
  no reconcile rewrites them: without the tripwire the pin bump lands green and
  the consumer's CI discovers the dead call later. The check judges only the task
  commands it can read exactly — plain, unquoted, unredirected word sequences,
  which is every one of the fleet's 14 call sites — and declines the rest rather
  than approximating a shell, so it never fails a valid manifest closed.
- cli: `shipit provision` is a tombstone. The verb is retired (ADR-0066) and
  restores nothing; it exists so a surviving call site fails with the remedy
  (lexd rides the Artifact channel; edit the pixi task) instead of click's
  `No such command 'provision'`. Hidden from `--help`, always exits 1.

## 1.5.0 - 2026-07-27

- stage: new `shipit stage` — the generic, manifest-driven stage-from-prefix step
  (conda-direct T3, #1079). After `shipit install`/`pixi install` resolves a
  conda dependency into the pixi env prefix, an app-type consumer declares a
  `[stage.<pkg>]` map of source-in-prefix → dest-under-`resources/` copies (a
  tool binary at `bin/<tool>`, a data artifact under `share/<pkg>/…`), and this
  step copies each file or directory into the shipped bundle — files keep their
  mode so a tool binary stays executable. It replaces the legacy cross-repo
  `fetch-deps`/`deps.json` fetch with only the source axis swapped (a gh-release
  download becomes a read of the already-resolved env prefix); the copy is
  durable and idempotent, and refuses loudly on an unresolved source or a
  destination that would escape the checkout. Distinct from the release-time,
  transient vsix `bundle.stage` map — the two share only the env-prefix resolver
  and the checkout-escape guard, never a parallel mechanism.
- install/artifact-deps: the consumer contract collapses to conda-direct
  (ADR-0077, #1092). **Location is derived:** `[artifact-deps.<pkg>] { repo }`
  is the sole input from which shipit projects the managed `channels` (+
  private-tier `[s3-options]`) block — the URL is never restated. **Version is
  consumer-owned:** the version is pinned as an ordinary pixi dependency in the
  SAME feature that carries the derived channel —
  `[feature.shipit-artifacts.dependencies].<pkg>` (or
  `[feature.shipit-artifacts-<feature>.dependencies].<pkg>` for a named
  `feature`) — so pixi resolves the pin against the channel, `pixi.lock` records
  it, and a generic bot (`pixi update` / Renovate) bumps it. `shipit install`
  fails loud if a declared artifact has no such pin, naming the exact table to
  add (never a silent resolve-nothing).
- **No backwards compat (ADR-0077):** the legacy `[artifact-deps.<pkg>]`
  `version` key is refused at parse with a migration-pointing message; the
  `version` field is removed from the typed `ArtifactDep`.
- **Cascade removed (supersedes ADR-0067):** the bespoke artifact-dep version
  bump — the consumer-side receive/bump workflow (`channel receive`) and the
  producer-side fan-out (`release cascade`) — is deleted. Cross-repo version
  bumps now happen via plain pixi / a generic dependency bot editing the
  consumer-owned pin. The source-rebuild `notify-downstreams` rail is unaffected.

## 1.4.3 - 2026-07-19

- changelog: new `shipit changelog check-fragment` — a required PR-time gate
  that fails a PR merging to `main` when `CHANGELOG/` holds no unreleased
  fragment (#1073). It is a fragment-PRESENCE check: it asks the cut's own "are
  there unreleased fragments?" question at PR time, over the same discovery
  machinery the cut uses, so a missing release note is caught per-PR — before
  merge — instead of at the next cut once every PR is already in. Self-gating and
  offline: the base ref is read from the CI runner env, the fragment presence
  from the current checkout's `CHANGELOG/` — no `gh` auth and no CI event
  trigger. Wired as a required, PR-only `changelog` lane (`local = false`), so it
  runs in the CI checks matrix but never on a laptop commit/push; it reads the
  working tree, so it needs no workflow YAML change.
- docs: record the decision to collapse the cross-repo artifact system to
  **conda-direct** (see ADR-0077, docs/adr/0077-collapse-to-conda-direct.md, plus
  the docs/spec/conda-direct.md spec); supersedes the accreted parts of
  ADR-0064/0067/0070/0071 (part of #1089).
- release/conda: the conda endpoint now packages a producer's build output
  **directly** into a `.conda` (conda-direct, ADR-0077, #1092) — the served
  subdirs, target triples and staged archive names are DERIVED from the
  artifact's own `[artifacts.<name>].platforms` declaration (the causal single
  source), never reverse-engineered from a staged filename. The gh-release→conda
  repackage coupling is gone: the build output is staged by the bundle stage and
  conda has NO gh-release dependency, so a conda-only publish plan is valid (the
  release-before-derived ordering constraint that required an unskipped
  gh-release for conda is removed; brew/notify-downstreams keep theirs). Both the
  per-platform and the noarch (`noarch: generic`) paths are covered, and the
  load-bearing `binary_relocation: false` no-relink recipe insight is preserved.
- test/conda: the conda **per-platform** producer path (`_publish_conda`) now has
  a REAL `rattler-build build` integration test — the seam that hid the four
  original conda-producer bugs (#1049 ×3 + #1052) was faked
  (`_CondaBuildRecorder` wrote an empty `.conda`), so shipit's argv/recipe were
  asserted but rattler-build never ran. `test_conda_per_platform_real_repackage_*`
  drives the real producer through an ACTUAL cross-target
  (`--target-platform` non-native) build, faking only the S3 publish, and asserts
  the `.conda` lands with the prebuilt binary at `bin/<binary>` (the
  archive-top-dir-strip class, #1049) and the no-relink guard on the recipe (the
  cross-platform relink class, #1052). Reverting fix #1049 makes the real build
  fail with `cp: <artifact>-<triple>/<binary>: No such file or directory`
  (#1053).
- test/conda: the ADR-0064 `file://` round trip (build → local conda channel →
  scratch `pixi` resolve → read the env-prefix staging path) is now automated —
  `test_conda_file_channel_roundtrip_*` plus a reusable, runnable harness
  (`tools/conda_channel_roundtrip.py`). It resolves via a PLAIN
  `[workspace]` channel+dep `pixi.toml` (not the `[artifact-deps]` projection,
  which hard-codes the GCS host), the loop ARF02 Steps 1/2 (#1078/#1079) run
  against. Both tests `skipif` cleanly when `rattler-build`/`pixi` are absent, so
  a bare host skips rather than fails.
- docs(repo-new): plan two new `shipit repo new` Creation profiles — a **Node**
  profile (TypeScript CLI + local library, one black-box `Hello, world!` test,
  `toolchain="npm"`, riding the existing `npm` dispatch leg and the managed
  `nodejs`/`pnpm` provisioning; chooses **pnpm** as its package manager with an
  exact `packageManager` pin (`pnpm@X.Y.Z` — the concrete version the managed
  pixi environment resolves, not a range), a tracked `pnpm-lock.yaml`, and a
  frozen `pnpm install --frozen-lockfile`) and a distinct **svelte-app** profile
  (SPA-only Vite + Svelte + Tailwind + TypeScript, one smoke test; a single-root
  Vite app whose Artifact declares no package and builds via a bare
  `pnpm run build`). This release accepts exactly **one effective profile** per
  `repo new` (one of `rust`, `node`, `svelte-app`); multi-profile composition
  stays future work. Amends `docs/spec/repo-new.md` (Rust → Rust + Node +
  svelte-app; one-effective-profile constraint restored and extended; Cargo naming
  scoped to the Rust profile; retires the Rust-only Non-Goals; carves svelte-app
  out of the minimal lib+CLI limit) and adds ADR-0077 (Node profile) and ADR-0078
  (frontend scaffolds are distinct profiles, not multi-stack composition or a
  base×flavour matrix). Planning only: the build/install toolchain-seam
  generalization the profiles depend on — registry polymorphism, profile-owned
  naming, pre-check dependency materialization, reconciling the `npm`-named
  dispatch leg to pnpm (including pnpm-aware `--filter` Artifact narrowing, not
  `--workspace`), and making `ArtifactDecl.package` optional — is separate work
  tracked in #1083.
- install: `shipit install` now makes the fundamental skills loadable by the
  agent runtimes (#1088). Skill content is a single real managed dir at
  `.agents/skills/<name>` (where agy/codex read it), and `.claude/skills` is a
  whole-directory symlink to it (`../.agents/skills`) that install ensures as a
  structural step — so Claude Code reads the identical set without a second
  physical copy. Previously install shipped the source-only `.shipit-skills/`
  store, which no runtime loads. The switch from an existing real `.claude/skills`
  dir is path-scoped and pristine-checked: a dir of shipit-pristine content is
  removed and symlinked; any consumer-modified file blocks and is left untouched
  (fail-safe), never via content-hash-global retirement (which would delete
  shipit's own byte-identical source). Whole-file managed writes also fail closed
  on a symlinked destination component in every mode, so an install never writes
  through a discovery-dir symlink onto a target outside the repo. See ADR-0077.

## 1.4.2 - 2026-07-19

- channel: `store_provision.verify()` now probes the served conda subdirs the
  repo ACTUALLY publishes — (its declared conda-endpoint platforms ∩ the served
  set) — instead of the fixed all-of-served set (#1076). It required a
  `repodata.json` under **every** `SERVED_SUBDIRS` entry incl. `win-64`, but a
  producer only publishes the subdirs it builds (`conda_assets`/`conda_subdir`
  drop unserved/undeclared platforms), so a correctly-provisioned channel for a
  repo that ships fewer platforms — e.g. lexd (linux x86_64/aarch64 + darwin-arm64,
  no windows) — reported **NOT ready** (false negative on the absent win-64). A
  new pure `release.publish.conda_served_subdirs(artifacts)` projects the repo's
  conda-endpoint artifacts' platforms onto the served subdirs (the same
  platform→triple→subdir derivation the publish stage uses); `verify` takes a
  `subdirs` probe set (an explicitly empty one is refused, never a vacuous
  all-pass), and the `verify` CLI derives it from the TARGET repo's `.shipit.toml`
  only when the operator OPTS IN with an explicit `--manifest` — `--repo` is an
  arbitrary `<owner>/<repo>`, so silently scoping from an ambient `.shipit.toml`
  could probe a narrower set than the target publishes and pass a channel missing
  a subdir (a false-ready). Absent `--manifest` (and for a conda-less manifest),
  the probe stays the conservative full served set. The same wrong-axis family as
  #1072, on the channel-readiness surface.
- install: the conda endpoint's packager (`rattler-build`) is now provisioned
  off a declared **`conda` endpoint** instead of the **rust** toolchain (#1071).
  It used to live only in the rust release-deps block, so a NON-rust conda
  producer — tree-sitter-lex's `tarball` grammar (`endpoints = "gh-release,
  conda"`) — declared conda but had no packager, and `shipit release publish`'s
  conda stage died `No such file or directory: 'rattler-build'` while
  `shipit install` reported "nothing to do". `rattler-build` now rides a new
  conda-endpoint-gated managed block (`pixi.toml#shipit-conda-packager`,
  `src/shipit/data/pixi-conda-packager-block.toml`), delivered iff any
  `[artifacts.*]` declares a `conda` endpoint (`_declared_endpoints`), and was
  **removed** from the rust release-deps block — so a rust+conda repo gets it
  exactly once (no duplicate `rattler-build` key / pixi conflict) and a
  rust-without-conda repo, which publishes no `.conda`, no longer carries it.
  The missing-`rattler-build` reconcile remedy now names the new block. Consumers
  that produce conda (rust or not) get the packager on their next
  `shipit install` reconcile.
- install: the managed `[feature.shipit-lexd]` block's `[target]` set is now
  GENERATED per-repo as **(the repo's declared `[workspace].platforms`) ∩ the
  channel's served set** instead of a fixed all-of-served set (#1072). The #1068
  fix emitted a `[target.win-64.dependencies]` table on every consumer, but no
  repo currently declares `win-64` (Windows paused, #895), so pixi warned
  (`target selector 'win-64' does not match any of the platforms supported by the
  workspace`) on **every** invocation — fleet-wide noise, hit by the owner on
  shipit's own `main`. A repo now carries a `win-64` target only when it declares
  `win-64` (keeping ADR-0071's fail-closed for a real Windows consumer), and none
  otherwise, so the dangling-selector warning is gone. The served-set
  intersection (osx-64 and other unserved platforms still carry no lexd dep, so
  their lint env solves) is unchanged from #1068. The lexd version is single-
  sourced through `units.LEXD_PIN`; shipit's own manifest drops its win-64 lexd
  target. Rolled out on each consumer's next `shipit install` reconcile.

## 1.4.1 - 2026-07-19

- cascade: the generated `shipit-artifact-cascade.yml` receive workflow now
  passes shipit's own strict yamllint and invokes the launcher through the `--`
  separator (#1057). The generator's foreign-dispatch guard echo was shortened
  under the 120-column cap (it exceeded it, forcing lex-fmt/vscode#162 to add a
  consumer `[lint].ignore` for the managed bytes), and the `pixi run --locked`
  step now reads `pixi run --locked -- ./bin/shipit channel receive …` so pixi
  never mistakes `./bin/shipit` for a task name. A regression test renders the
  workflow and runs the shipped yamllint config over it, so a >120-char line or
  a dropped separator can't silently return. Consumers no longer need a
  `[lint].ignore` for the generated file.
- lint: the managed `[feature.shipit-lexd]` block now **platform-scopes** the
  fleet-pinned `lexd` to the Artifact channel's closed served set
  (`SERVED_SUBDIRS`: osx-arm64/linux-64/linux-aarch64 and win-64) via pixi
  `[target]` tables, instead of a blanket `[feature.shipit-lexd.dependencies]`
  (#1068). The blanket dependency applied `lexd` to *every* platform a composing
  env declares, so a consumer declaring a platform **outside** the served set
  (e.g. `osx-64`, Intel Mac) hit an unsatisfiable lint-env solve (`No candidates
  were found for lexd`) and `shipit install` failed closed — no commit, no PR —
  blocking any such repo from reconciling. Platforms outside the served set now
  carry no `lexd` dep, so their lint env solves and install/reconcile no longer
  breaks. **win-64 stays in the served set** (served but owner-paused, #895):
  it keeps its target dep, so a win-64 solve still finds no channel candidate and
  **fails closed** — ADR-0071's Windows fail-closed at solve time is preserved,
  not softened to fail-open.

## 1.4.0 - 2026-07-19

- release: the `conda` derived endpoint gains a **noarch mode** so cross-repo
  DATA artifacts (the tree-sitter grammar, the wasm build) ride the Artifact
  channel as `noarch: generic` conda packages (ARF02-WS07, ADR-0076; #1064). An
  artifact whose composition produces a single platform-independent archive (the
  `tarball` composition — `<artifact>.tar.gz`, no triple) repackages that one
  archive into ONE `noarch: generic` `.conda` published to the channel's
  `noarch/` subdir, which every conda client reads alongside its platform subdir,
  so no consumer change is needed. The per-platform tool-artifact path
  (`CONDA_SUBDIRS`, triple→subdir fan-out) is untouched — the modes are additive.
  The recipe extracts into a `payload/` subdir and copies only that into
  `$PREFIX/share/<package>/`, so rattler-build's build scaffolding is never swept
  into the package, and it carries no `--target-platform` (rattler-build refuses
  it for noarch — the recipe's `noarch: generic` drives it). `noarch` is a
  distinct always-present subdir (`buckets.NOARCH_SUBDIR`), NOT a member of the
  per-platform served set: the store `verify --noarch` readiness probe is a
  single `noarch/repodata.json` resolve, never a per-platform sweep and never
  subject to the ADR-0071 `win-64` pause subtraction. Covered by a REAL
  end-to-end repackage test that drives an actual `rattler-build build` (the
  #1050/#1053 do-not-fake lesson).
- gh-setup: required-check auto-discovery no longer invents a phantom
  `<caller> / run` context that bricks rulesets (#1056). Static discovery (the
  no-runs onboarding path) now DROPS any job whose reported check name is
  statically unpredictable — a `strategy.matrix` job (it reports `id (values)`,
  never the bare id) or a `${{ … }}` display name — instead of guessing its job
  id, warning loudly (stderr + WARNING) on every drop. The guard is
  per-workflow: gh-setup writes the ruleset only when EVERY PR workflow still
  contributes at least one certain context; if any is left with zero, discovery
  REFUSES to write (rc 1, an actionable error demanding explicit `--checks` with
  a per-workflow certain/dropped breakdown) rather than silently write a weaker
  rule. On `lex-fmt/lex` this yields exactly `check`, `checks / plan`,
  `Documentation`, `WASM build` with zero human input, and never the phantom
  `checks / run`. `--checks` override and runs-based discovery are unchanged.
- lint: `shipit provision lexd` is retired — `lexd` now rides the public
  Artifact channel as an ordinary conda dependency, resolved through `pixi.lock`
  and integrity-checked by pixi's sha256 (ARF02-WS06, ADR-0066/0071; #1005). The
  bespoke fetcher (`src/shipit/provision/`, its trust-on-first-use SHAs, the
  `provision lexd` verb, and the `provision-lexd` pixi task) is deleted with no
  fallback. Fleet uniformity moves from a compiled binary constant to a
  shipit-managed, consumer-non-editable `[feature.shipit-lexd]` pixi block
  (channel + `lexd = "==0.19.10"`) that `shipit install` wires into every managed
  repo's lint env (ADR-0047), so a consumer cannot drift its `lexd` version. The
  orphaned `curl` lint dependency (only ever the fetcher's downloader) is dropped.
  Windows (`win-64`) is unserved under the build pause (#895) and now fails closed
  on a lint solve — deliberately, with no `provision` fallback.

## 1.3.2 - 2026-07-18

- conda: the repackage recipe disables rattler-build's default binary
  relocation (`build.dynamic_linking.binary_relocation: false`) — the 4th
  producer-path fix from the ARF02 seed (#1052, follows #1049). rattler-build
  relinks by default under conda-build's built-from-source assumption, but
  this endpoint repackages a PREBUILT, already-SIGNED release binary that
  links only system libraries: there are no conda-prefix paths to relocate,
  the relink needs a per-OS toolchain the single cross-platform runner lacks
  (the osx-arm64 build died on a Linux runner failing to find
  `install_name_tool`), and rewriting the Mach-O would invalidate the sign
  stage's signature. Validated locally (rattler-build 0.69.1) against all 3
  served subdirs of the real `lex-fmt/lex v0.19.9-rc.1` `lexd-lsp` archives:
  with the flag, all 3 build clean with no relink step.

## 1.3.1 - 2026-07-18

- conda: three fixes in the untested producer path, surfaced by the FIRST
  real run — the ARF02 channel seed (#1049, blocks #1002). (1) The rendered
  recipe's copy source is now the bare binary name: rattler-build STRIPS the
  archive's single top-level `<artifact>-<triple>/` dir on extraction, so the
  old prefixed source failed `cp: cannot stat`. (2) The S3 env seam feeds
  rattler-build the AWS SDK credential-chain names (`AWS_ENDPOINT_URL` /
  `AWS_REGION` / `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`) — the `S3_*`
  names were ignored and publish died "Could not determine region from AWS
  SDK configuration". (3) The managed rust-release deps block pins
  `rattler-build = "0.69.*"`: 0.68.* panicked during the S3 upload
  (opendal-core "concurrent tasks executed with no executor") even with
  correct creds. All three validated live against the `lex-fmt/lex`
  `v0.19.8-rc.1` `lexd-lsp` archive + a push to the public channel bucket.
- review: parse-failure diagnostics are evidence-based — only an explicit
  backend timeout recommends a faster model or a smaller diff (#1006, #1033
  as context). The old catch-all reported every unparseable reviewer response
  as "no parseable JSON … try a faster model or a smaller diff", which was
  actively wrong when `agy`'s model narrated prose instead of answering on a
  4-file docs diff (the pin itself was reverted by #1032). `parse_review_output`
  now distinguishes what the raw output actually shows: an explicit timeout
  marker (the one case where size/latency advice is honest), empty stdout (a
  silent non-delivery), complete JSON with the wrong `{summary, comments}`
  envelope (an output-contract fault, #826), and everything else — prose,
  narration, partial JSON — as a conservative "no review verdict" that points
  at the raw output instead of guessing a cause. Raw-output salvage (#76) and
  the structured `timed_out` flag are unchanged; no static model blacklist is
  introduced — AGY reviewer health is follow-up runtime-provenance work, and
  this change does not claim it fixed.

## 1.3.0 - 2026-07-18

- Every repo now has **one Claude Code session store, shared by all its Trees**
  (#1023). Claude Code keys session transcripts *and* auto-memory on
  `~/.claude/projects/<slug>/`, where the slug is the session's working
  directory — so a Tree per session (ADR-0027) handed every launch a brand-new
  empty namespace. Memory was never broken; it was re-partitioned every session
  and never read back, and resume could not find a transcript from any directory
  but the one that wrote it. The cost was measurable: 44 memory files stranded
  across 23 throwaway stores, and the real store frozen since the day session
  Trees took over.
  There is no configuration knob for that path — the derivation is hardcoded in
  the harness — but the store is a plain path and a **symlink is honoured**. So
  `tree create` now plants `~/.claude/projects/<slug>` as a symlink to the repo's
  store *before the session starts*, and `shipit install` links the canonical
  checkout the same way, so work in a Tree and work in the plain checkout share
  one store rather than splitting in two. One symlink fixes memory and resume
  together. The store is keyed on the **origin remote**, not the path —
  consistent with how Tree scanning already resolves repo identity, precisely
  because a path "is not a reliable identity" — and lives at
  `~/.claude/stores/<owner>/<repo>/`, outside `projects/` so shipit-owned state
  is never confused with the harness's own directories. The store is not in the
  Tree and is never swept with one: reclaiming a workspace no longer destroys
  what was learned in it.
  Planting is a defined, idempotent algorithm rather than "link it", because the
  canonical checkout's directory is the hard case and the common one: it already
  exists, with real memories in it. Clobbering would destroy them and skipping
  would leave the store split in two forever, so: an already-correct symlink is a
  no-op (re-running install is free), an absent one is created, a **real
  directory is adopted** — its contents merged into the store, then replaced by
  the link — and a symlink pointing somewhere else is **refused loudly, changing
  nothing**, since something outside shipit owns that path.
  Adoption is a recursive merge over relative paths, not a move of top-level
  entries: a slug directory holds `memory/` on both sides, so the first collision
  is directory-versus-directory, and moving the top-level entry would rename the
  whole tree into a layout Claude will not read. Every (source, target) type pair
  has a defined outcome — identical files are dropped as duplicates, **divergent
  files keep both** under a non-colliding name (never overwritten, never silently
  dropped, never machine-merged), directories merge, and a *type* conflict at any
  path is refused with both sides left untouched while the rest of the merge
  carries on. Symlinks are adopted, never followed. Nothing is deleted from a
  source until its content is verified present in the target, and a directory
  that could not be fully drained is never replaced by the link: memory is
  irreplaceable, and a store left split is recoverable where a deleted memory is
  not. Planting is **serialized per store**, so two checkouts of one repo
  migrating at the same time cannot both claim one destination and have the
  second's copy land on the first's memory — and two runs against the *same*
  checkout cannot race either: the second re-reads the directory once it has the
  lock and finds the first's link already there, rather than acting on what it
  saw before waiting. A refusal touches nothing on either side, including the
  store directory itself.
  `shipit install` plants the link on **every** run except `--dry-run`, including
  one where the managed set is already current: the link is not a managed file,
  so a clean plan says nothing about whether it exists — and an already-installed
  checkout is exactly the one with a store to migrate.
  Both seams are **fail-open**: an unresolvable repo, an unwritable `~/.claude`,
  or no `~/.claude` at all (a CI runner, a container) costs a Tree or an install
  exactly nothing, and logs at DEBUG rather than warning on every single run —
  the store is additive, and without it a session merely keeps its memory to
  itself, which is the behaviour every session had before this existed.
- spawn: the reviewer SPAWNED payload's `tree` now reports the reviewer's ACTUAL
  per-Run read-only Tree, not a speculative coordinate (#1039). ADR-0074 made
  review Trees per-Run with a minted UUID, so the flat-leaf naming
  `_launch_reviewer` reported and the UUID `review/producer.provision_review_tree`
  minted independently could no longer agree by computation — `payload["tree"]`
  named a plausible path the reviewer never ran in. The spawn boundary now mints
  the flat-leaf naming ONCE and threads it down through the review service
  (`run_detached_review` → `generate_review` → `run_fanout_review` →
  `provision_review_tree`) via a new optional `review_tree_naming` /
  `naming` parameter (default `None` = "mint your own", so the review adapters'
  own re-review path and every other caller are unchanged), so the producer clones
  the reviewer under that exact id. Two reviewers on the same head still mint
  distinct namings upstream, so their per-Run Trees — and payloads — still differ.
- `tree gc` now **reclaims a Tree on measured activity rather than proxies for
  it**, closing a bug that deleted a live session's worktree (#1018). One rule
  decides every Tree kind — review, ephemeral, and write alike:

  ```text
  KEEP  if  dirty  ||  unpushed  ||  idle < 48h
  ```

  The three ladders this replaces read fifteen inputs between them — a pidfile,
  a `ps` probe, the PR's state, the Tree's kind, and four separate time windows
  — to answer one question none of them measured: *is anyone working here?* The
  ephemeral ladder answered it from the clone root's mtime, which does not move
  when an agent edits under `src/` (measured lag: up to **10 hours**), and its
  last rung read age alone — so a single liveness false-negative deleted a clean,
  live Tree. `idle` is now measured directly, as the newest of any file's mtime
  under a pruned walk and `HEAD`'s commit stamp, so both an agent editing files
  and an agent committing deletions are seen.
  **Unknown is never idle.** A `git status` or `git rev-list` that fails, a walk
  that hits an unreadable directory or finds no eligible file, a `stat` that
  raises — each one KEEPS the Tree and is reported. A wrongly-kept Tree costs
  disk until the next sweep; a wrongly-deleted one costs work that no longer
  exists. That asymmetry is the whole design, and it matters more now that the
  sweep is on its way to running unattended (#1017).
  **48h is deliberately above the observed band, not inside it.** Across a live
  fleet, idle time separates with no overlap: every live Tree measured under 1h,
  every dead one over 41h. A Tree idle 41–48h simply waits for the next sweep,
  while the margin over the busiest live Tree stays 48×.
  The walk that measures this **prunes** `.git`, `.pixi`, `node_modules`,
  `target`, `.venv`, `dist`, `build`, and `__pycache__` — `.pixi` alone is ~97%
  of a Tree's file count, and unpruned the walk would cost more than everything
  it replaces. Measured across a live 155-Tree fleet: **6.8s end to end**, at
  ~7ms per Tree versus ~425ms unpruned.
  Acquiring a shared read-only review Tree now records activity, because a
  reviewer only ever *reads*: refreshing an already-current Tree rewrites no
  file, so an aged shared Tree handed to a reviewer could be reclaimed out from
  under the review that was using it.
  `--threshold` now sets the idle boundary. The `stale` bucket is gone: with one
  rule there is no ambiguous middle for a human to adjudicate.
- **`tree gc` now makes ZERO network calls, and the dead reclaim machinery is
  gone** (#1022). ADR-0072 replaced the liveness-and-PR-state reclaim ladder with
  one activity-based rule (`KEEP if dirty || unpushed || idle < 48h`); the earlier
  work left that rule reachable but the machinery it superseded still on disk. This
  removes it, with no change to the reclaim rule itself:
  - `session/liveness.py` (the pidfile, the `ps`/`jc` fork, the create-time
    tolerance, the argv host allow-list) and `tree/provision.py` (the pre-pin
    provisioning-commit record reader) retire, along with their tests. The
    `SessionStart` hook no longer writes a pidfile and the `WorktreeRemove`
    fast-path teardown no longer reads one or carves out provisioning commits — its
    never-lose-work floor is now exactly gc's own (dirty or unpushed).
  - **The entire `gh` network dependency leaves the Tree scan.** The per-repo
    `PrIndex` batch that fed a signal reclaim no longer reads is deleted, so
    `tree gc` (and `tree list`, which shares the scan) reads only the local
    filesystem and `git`. On the largest fleet ever observed this was the
    difference between a >10-minute sweep and a ~22-second one; the cost was the PR
    read, and it is gone. A test asserts the gather makes no `gh` call.
  - `tree list` drops its **PR** column with the `gh` read; `TreeRecord` no longer
    carries `pr`/`pr_state`. The stale bucket, the per-kind gc dispatch, and the
    unreachable `live_reviews` review-Tree rung are gone with the ladder.
  - Net change is a deletion of roughly 2,000 lines across source and tests.
- tree: Trees are now **flat and self-describing** — one directory per Tree named
  `<repo>-<agent>-<timestamp>-<id>` under the central root, replacing the five
  nested `<owner>/<repo>/<kind>/[<code>/]<leaf>` shapes at two depths (ADR-0074,
  #1025). Repo leads the name so `ls | grep <repo>` is the tooling-free narrowing
  the hierarchy promised; `<agent>` is the backend binary (`claude`/`codex`/`agy`),
  minted once from the backend registry rather than smuggled in as a session-id
  prefix; `<timestamp>` (`%Y%m%d-%H%M%S`) gives `tree list` its first real
  **created** column; and `<id>` is a full UUID — never a pid, never truncated. Its
  provenance follows the creator: a coordinator session Tree carries the harness
  session UUID from the `WorktreeCreate` payload (so the dir name IS the
  `claude --resume` handle), while every spawned-Run and native-helper Tree mints
  its own. The `<kind>` and `<owner>` segments and `tree_kind()` are gone (reclaim
  is one uniform activity-based rule since ADR-0072, and repo identity comes from
  the origin remote); `session/current.py` now resolves a Tree from cwd with no
  depth arithmetic; and `resume.py` reads the backend from a recorded field instead
  of reverse-engineering it from the id prefix.
- tree: **review Trees are per-Run**, not shared. ADR-0018's read-only *mode*
  stands — a reviewer still gets a chmod'd read-only clone — but the deterministic
  `(repo, branch)` sharing is dropped along with its reuse/refresh/acquisition-stamp
  machinery: each reviewer Run gets its own flat Tree, dated by its own files like
  every other Tree (#1025). Old nested Trees are not migrated — they are reclaimed
  by attrition and coexist with the flat shape (`registry.scan` walks for `.git`
  markers and never parsed depth). Branch names are unchanged.

## 1.2.4 - 2026-07-18

- The **`agy` local reviewer works again** (#1006). It has been pinned to Gemini
  3.5 Flash since #990, and Flash goes *agentic* in `agy`'s headless `--print`
  mode: instead of reviewing the diff it is handed, it narrates its hunt for one
  and never emits a verdict. Every `agy-local` run therefore settled `failed`.
  The reviewer was not slow or wrong — it was **absent**, and had been for days.
  What made it invisible is worth recording, because nothing here misbehaved: a
  required reviewer that fails is *degraded*, and the PR engine deliberately
  declines to let a degraded reviewer block Ready — otherwise one broken
  reviewer would wedge every PR in the repo. So PRs kept flowing, green, with
  codex and Copilot passing, while the roster promised three required reviewers
  and delivered two. Measured on this repo: `agy-local` failed on **every PR of
  the TREE03 epic**, roughly ten review rounds, without ever once blocking one.
  A check that fails loudly on every run reads, over time, as furniture.
  `agy` returns to `pro` (Gemini 3.1 Pro (High)). The ~20% review-speed win that
  #989's spike measured for Flash is given up **deliberately**: a reviewer that
  never returns a verdict is not faster than one that does, it is not a reviewer.
- `tree gc` now **reclaims a merged Tree without waiting out the age
  threshold** (#1009). The write ladder gated on age BEFORE it looked at the PR,
  so a Tree whose PR merged days ago — clean, nothing unpushed, the work safely
  on the remote — was kept purely because its directory mtime was under the
  two-week boundary. At a real merge rate that parks a fortnight of finished
  work: measured over a 503-Tree fleet, **421 Trees were kept by the age gate
  alone** while exactly one had a PR in flight, and the `kept: 500` the verb
  reported read as "500 Trees in use" when it only ever meant "500 Trees are
  recent". The gate was measuring throughput, not use.
  A merged PR is now decided FIRST, held only until the Tree has been **idle for
  12h**: the merge already proves the loss is safe, and the window covers the one
  thing age was really buying — a write Tree has no liveness signal (unlike an
  ephemeral session Tree, which has its pidfile), so an agent may still be
  working in a still-clean Tree whose PR has merged. That window's clock is time
  since the Tree's last ACTIVITY, NOT time since the merge: what it needs to know
  is whether anyone is still working in the Tree. Hours of idleness instead of
  weeks of age closes that hole without parking the fleet. This brings the write
  ladder in line with the ephemeral one (ADR-0027), which already checked the
  merge ahead of its liveness and age rungs.
  Idleness is measured from the **newest of the Tree's directory mtime and its
  `HEAD` commit timestamp**, and both are needed. A directory's mtime moves only
  when an entry is added or removed in that directory, so the ordinary shape of
  agent work — editing a file under `src/`, staging it, committing it — leaves
  the clone root's mtime untouched and is invisible to it; the commit timestamp
  is what observes an agent at work. Pushing does not change that stamp either,
  so the one interval this window exists to cover — between a push and the next
  edit, when a live agent's Tree momentarily reads clean and fully pushed — is
  genuinely covered rather than nominally. An unreadable commit timestamp reads
  as ACTIVE, never as ancient: a git hiccup must not license a delete.
  `--threshold` (14d by default) is unchanged and still governs the **unmerged**
  shapes — no PR, or a PR closed without merging — where age remains the only
  abandonment signal, and those still land in `stale` for a human rather than
  being deleted. Every never-lose-work guarantee is untouched: a dirty tree,
  unpushed commits, an unreadable commit list, an in-flight PR, or an unreadable
  PR state all still keep, whatever the Tree's age or merge state.
- **TREE03 planning docs land: the Tree gets rethought** (#1020, epic #1019).
  Running Trees
  for a while exposed three failures with one root cause — the system infers
  what it could measure, and encodes in paths what it then refuses to trust.
  `tree gc` deleted a **live** session's worktree (#1018); session memory has
  been silently discarded since Jul 6 (44 files stranded across 23 throwaway
  stores); and the directory hierarchy is written on create and ignored on read.
  Three ADRs record the decisions, and `docs/spec/tree-rethink.md` is the
  authoritative Spec:

  - **ADR-0072 — reclaim is activity-based.** One rule for every Tree kind:
    `keep if dirty || unpushed || idle < 48h`, where idle is measured
    newest-file mtime over a pruned walk. Supersedes ADR-0027's five-rung
    ladder and the pidfile liveness beneath it. Across the live fleet, idle time
    separates with no overlap — every live Tree under 1h, every dead Tree over
    41h — so the threshold sits in a chasm, and the apparatus that existed to
    manage an ambiguous middle (a `ps`/`jc` probe, a PR-state network read, four
    tunable windows) is deleted rather than fixed.
  - **ADR-0073 — the session store is per-repo, not per-Tree.** Transcripts and
    memory are keyed on the session's cwd, and a Tree per session means a new
    empty namespace every launch. One store per repo, linked into place at
    tree-create, fixes memory and resume together.
  - **ADR-0074 — Trees are flat.** `<root>/<repo>-<agent>-<timestamp>-<id>`,
    one uniform shape. No ADR ever chose nesting: it was inherited from the
    branch grammar, which is slashed for a git ref-collision reason that has no
    filesystem analogue.

  Docs reconciled with the new model: `docs/dev/naming.lex` gains a §4 for the
  flat Tree-directory grammar, `CONTEXT.md` gains **Reclaim** / **Idle** /
  **Session store** and drops read-only-Tree sharing, and both
  `docs/dev/epics.lex` §7 and the coordinator role stop telling every session
  that its memory is doomed — memory now persists, so learnings get promoted to
  the repo because the repo is how knowledge reaches reviewers, not because
  memory leaks.

## 1.2.3 - 2026-07-16

- The managed bootstrap scripts (`bin/setup-dev-env.sh`, `agent-start`) now
  resolve their **repo root symlink-safely** (#994). `cd -P` resolves every
  path component physically, so a symlinked intermediate `bin` sent `..` to the
  LINK TARGET's parent, and a symlinked script path (`~/bin/agent-start` → the
  checkout's copy) was never followed at all — both landed outside the
  checkout, provisioning the wrong repo or rooting a coordinator session in it.
  Each script now follows its own link chain first — joining relative link
  targets against the directory physically holding the link, as the kernel does
  — then resolves the final directory logically. Every resolution step is
  fail-open: a missing or erroring `readlink`, or a `cd` into a directory that
  is gone, warns and uses the path as-is instead of aborting the script or
  silently degrading to a bare `.` root.
- `install --pr` now **returns the operator to their branch** when the
  reconcile adds a new managed path (#993). The reconcile commit is built on an
  isolated scratch index (#992), so a newly written managed file — the
  `.shipit-skills/` skill store, a fresh agent definition — sits on disk while
  the checkout's real index has never heard of it. Git refuses to switch away
  from a branch whose HEAD carries an untracked working-tree file
  (`error: The following untracked working tree files would be removed by
  checkout: .shipit-skills/…`), so the best-effort branch restore only logged
  the failure and left the operator sitting on the `shipit/install` scratch
  branch — the exact strand the #777 restore exists to prevent. (The pushed PR
  and the exit code were always correct, so scripted fan-out was unaffected.)
  The restore now stages the newly ADDED managed paths — and only those — into
  the real index immediately before the switch, so they are tracked and the
  checkout is a plain branch change: the added path is dropped, the reconcile
  stays in the PR, and the operator lands back on their branch. Whatever the
  operator had STAGED survives the flow untouched: a managed path they already
  track is deliberately left alone, and so is one they had staged for deletion
  (`git rm --cached`) — neither is a path the reconcile added, and staging over
  either would destroy index-only work with no commit to recover it from.

## 1.2.2 - 2026-07-15

- Local **AGY reviews** are faster: the `agy` reviewer backend now runs through
  a native reviewer agent on Gemini 3.5 Flash, cutting local review latency
  (#989, #990).
- `install --pr` reconcile now reliably **publishes retired-file deletions**
  (#991, #992). The MODE_PR commit previously staged deletions into the index
  with `git rm --cached` but then committed with a pathspec `git commit --
  <paths>` — git's partial-commit mode, which builds the tree from the WORKING
  TREE of the named paths and disregards the index, silently negating the
  staged deletions. A retired file whose working-tree copy survived reappeared
  in the commit, so every reconciled consumer kept stale files alongside their
  replacements — most visibly the `skills/` → `.shipit-skills/` skill-store
  move, where all 11 retired `skills/*` files were left behind. The reconcile
  commit is now built on an **isolated scratch index** seeded from
  `origin/<base>` (`read-tree`) into which only the managed paths are staged —
  writes via `git add`, retired-path deletions via `git rm --cached` — and
  published with a whole-index commit. This honors the deletions, keeps the
  scoping that excludes unrelated dirty consumer files, and is correct
  regardless of the Tree's cut point (a stale `shipit/install` head can no
  longer squash unrelated commits into the reconcile), all while leaving the
  operator's real index untouched.

## 1.2.1 - 2026-07-15

- `install --pr` builds its reconcile commit from an authoritative
  apply-recorded touched-set instead of a hand-enumerated "commit universe"
  (#986, the design fix behind #852/#984). `apply()` now records the exact set
  of shipit-owned paths it is responsible for AS it mutates — managed unit
  destinations (NOOP units included), the re-stamped `.shipit.toml`, a
  re-rendered `CHANGELOG.md`, rewritten hook files, and every non-KEEP retired
  path — and the MODE_PR commit publishes exactly that set, scoped to the paths
  that actually carry a staged diff against `origin/<base>`. Retired-path
  deletions are staged from the index with a new `git rm --cached
  --ignore-unmatch` primitive, so a retired file's absence is published as a
  deletion without ever touching the working tree: a consumer file that
  reappeared at a retired path is preserved, a NOOP retired path is never
  unlinked, and an absent or untracked path can no longer crash PR generation.
  This removes the per-category carry/skip rules (`Plan.retire_carries` and the
  universe enumeration) whose asymmetries drove the seven-round #984 whack-a-mole,
  closing the consumer-edit leak surface by construction.

## 1.2.0 - 2026-07-15

- `shipit repo new --stack rust <name> [parent]` creates a new local Repo
  with a complete, verified, shipit-managed baseline (GEN01, #944): it
  scaffolds a two-crate Cargo workspace (a `<name>` CLI over a `lib<name>`
  library), applies the managed install baseline, resolves the pixi lockfile,
  and certifies the Repo by running its lint, test, and build Checks — staging
  the whole tree in a sibling and publishing it with one atomic rename only
  after every Check passes, so a single initial commit lands on `main` and any
  failure leaves the destination untouched. `--stack` is repeatable for future
  multi-toolchain Repos but v1 supports one profile, `rust`. Creation is local
  only — it creates no GitHub repository, remote, or release policy, keeping it
  distinct from `shipit install`, which adopts and reconciles an existing
  repository. See `docs/spec/repo-new.md` for the exhaustive contract.

## 1.1.1 - 2026-07-14

- The standing sign e2e (#899): `shipit wf verify-canary` dispatches
  shipit-canary's blessed release caller through the full sign proof matrix
  on live GitHub — the composed `stage=full` chain (sign+notarize on a real
  macOS runner, the #873/#889 class) and the staged
  `prepare`→`build`→`sign`→`publish` relay (the real cross-run artifact
  hand-off, the #898 class) — watches every run to its verdict, prints the
  proof-citation and teardown blocks, and exits green only when every run
  is. The workflows.lex §9 runbook makes citing both green chains mandatory
  for any PR touching the sign/relay/wf-yml surface, and names the exact
  canary-side surface (signed darwin-arm64 artifact, blessed caller, the
  owner-pushed Apple secret set) the proof rides on.
- Provision the `tree-sitter` CLI on release runners (#890, closing the
  TOL02-WS17 provisioning inventory's open hole 7): `shipit install` now
  delivers a managed `pixi.toml#shipit-tree-sitter-release-deps` block
  (conda-forge `tree-sitter-cli`, pinned `0.25.*` in parity with the grammar
  consumer's devDependency line) whenever a repo declares a tree-sitter
  `[toolchains]` leg — no manifest signals a grammar, so the declaration is
  the signal, the same union mechanics as the wasm-pack→node-deps delivery.
  A pixi-managed builder missing at `shipit build` now fails naming the
  install reconcile that provisions it, instead of a bare not-found note.

### Fixed

- Standalone `wf-build` dispatches are now a relay-complete source run for the
  sign/publish stages: a new standalone-only `notes` job re-derives the
  `release-notes` artifact at the tag via the new read-only
  `shipit release notes` verb, so a staged chain whose sign/publish names a
  build run as its source no longer fails `carry-notes` with
  `Artifact not found for name: release-notes` (#898).

## 1.1.0 - 2026-07-13

- lanes: declared-secrets seam — a per-lane `secrets` allowlist routes one
  scoped token into a wf-checks lane, gated routing-only in the block so a
  private-source test surface can move onto a managed lane (#778)
- install: self-cert now gates shipped skill content against the delivered
  markdownlint config, so the managed set can't ship content that reds a
  consumer's lint gate (#777)
- install --pr: flow-robustness — restore the caller's branch, pre-clean stale
  lefthook `.old` hook backups before activation, and a transactional
  fail-closed that rolls back a half-applied write on self-cert failure (#777)
- wf-checks: document lane self-provisioning as the sanctioned rule for
  submodule- and system-dep-dependent suites (provision in the lane's own
  `run` task, not via a block knob) (#759)
- managed-content: qualify the adoption.md pointer and align the spec
  placeholder surfaced by consumer reviewers (#781)
- release: electron bundle composition + dmg/AppImage integrity tiers
  (TOL02-WS14, #790)
- release: tauri-cli bundle composition — darwin .app/.dmg + linux
  .AppImage/.deb (TOL02-WS15, #827)
- release: vscode-marketplace + open-vsx endpoints + per-target .vsix
  (TOL02-WS13, #789)
- release: tree-sitter composition + notify-downstreams cascade
  (TOL02-WS16, #792)
- release: wasm/npm build composition (wasm-pack → npm tarball)
  (TOL02-WS12, #788)
- release: wasm-pack mirrors the tarball's platform_independent guard (#828)
- sign: electron per-code-role JIT entitlements + top-level .app hardening
  (#829, #830)
- sign: validate reseal payload link targets in the mac-app leg (#812)
- review: `shipit pr review validate` + REVIEW_SCHEMA self-check (#826)
- RPE01: Role Profiles and Work Environments epic (#825)

## 1.0.0 - 2026-07-12

- First release of shipit as its own published artifact. The tag is the
  payload: consumers ride the `@v1` workflow refs (ADR-0010) and the git pin
  (ADR-0033); `advance-major` takes the floating `v1` branch over from this
  release on, retiring the manual branch-advance workaround.
- release: make the deb composition CI-viable — cargo-deb self-provisions
  through the managed pixi surface, the native triple-dir contract, and a deb
  tier in assert-bundle (#785)
- release: archive-leg mac codesign + notarize — raw darwin CLI binaries ride
  the same sign stage as mac-app bundles (TOL02-WS08, #800)
- release: per-stage dispatch — the wf-* stage blocks are self-sufficient
  standalone (plan facts re-derived at the tag when omitted), and the
  routing-only `stage` choice caller is the blessed consumer dispatch surface
  (TOL02-WS09, ADR-0054, #804)
- release: declare shipit's own release surface — the no-build `gh-release`
  artifact (the tag is the payload) plus the blessed stage-choice dispatch
  caller `shipit-release.yml`, cutting shipit through its own pipeline (#774)
- release: close the release-tool provisioning holes — rust (cargo-edit,
  cargo-deb) and twine ride the shipit-managed pixi blocks, uv joins the
  managed surface, a provisioning inventory + drift guard pins the set, and
  an unprovisioned tool fails loudly naming the install reconcile instead of
  installing at run time (TOL02-WS17, #797, #799, #803)
