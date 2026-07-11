# lint checks — one definition, orchestrated in the binary

> Status: **implemented** (shipped before the per-feature PRD convention)
> Origin: this capability was built from the retired roadmap §3, reproduced below so the
> design + verification rationale survives the roadmap's retirement.
> Decisions: `docs/adr/0004-lint-orchestration-in-binary.md` (orchestration
> moves out of lefthook and into `shipit lint`) · `architecture.lex §5` (why a
> binary, not templated tasks), `architecture.lex §7` (the lint check: one
> definition, hard).

## Original design (reproduced)

The standardized multi-language lint check: a `[feature.lint]` pixi environment that
provisions the linters, a `shipit lint` subcommand that runs them over the tree,
exposed as `pixi run lint`, and a thin lefthook caller that fires it on
pre-commit and pre-push. This is shipit's FIRST pixi integration — the point
where the substrate proven in Spike 0 stops being a spike and becomes a real
dependency of shipit's own repo. The lint check is dogfooded on shipit from this step
forward (`lessons-learned.lex §1d`).

### The one inversion to internalize BEFORE reading release-core's gate

release-core's gate (`release_core/verbs/gate.py`) runs `lefthook run pre-commit
--all-files` — lefthook IS the orchestrator, carrying a per-language glob map and
shelling each tool, while the `gate` verb only wraps it and parses its `GATE: OK`
verdict. shipit INVERTS this. Because pixi has NO cross-manifest task inheritance
(`architecture.lex §5`), the rich logic cannot live in a pixi task templated into
each consumer (that is drift on `pixi.toml`); it lives in the binary. So in
shipit the orchestration moves OUT of lefthook and INTO `shipit lint`: lefthook is
thin (it calls `pixi run lint`), pixi is thin (it runs `shipit lint`), and
`shipit lint` does the per-language discovery, routing and aggregation. Do NOT
reproduce release's lefthook-as-orchestrator shape, its `toolset.py`
npm/pip/binary provisioning, or its verdict parsing — those three are exactly
what pixi plus the binary model replace.

### What to reuse from release-core (the slim, valuable part)

Take the per-language TOOL INVOCATIONS and version pins as the starting
reference — they are battle-tested command lines, not orchestration:

- python — `ruff check` + `ruff format --check`
- rust — `cargo fmt --all -- --check` + `cargo clippy --all --all-targets --all-features -- -D warnings`
- shell — `shellcheck --severity=info` (+ `shfmt -d` for formatting)
- yaml — `yamllint`
- json — `prettier --check`
- markdown — `markdownlint`
- go — `gofmt -l` + `go vet ./...` (+ `golangci-lint run` where present)
- lex — `lexd check` (shipit-native; see the provisioning gap below)

release-core's pins live in `toolset.py` (ruff 0.15.x, shellcheck 0.11, yamllint
1.38, prettier 3.x, markdownlint-cli 0.48, lefthook 2.1.9, golangci-lint 1.64);
reuse them as a baseline but RE-PIN through conda-forge, not npm/pip — see the
pixi integration below. Skip every release-specific lefthook command
(`workflow-action-major`, `consumer-contract-*`, `captured-fixtures-lint`,
`lint-skills`): those encode release's sync model, the thing shipit deleted.

### The shipit lint verb (the orchestrator)

A NEW lint service `src/shipit/lint.py`, exposed through the thin
`src/shipit/verbs/lint.py` CLI wrapper and attached in `cli.py` exactly like
`gh-setup` (a thin click command forwarding to a `run(...) -> int`). The
per-language orchestration is pure logic — keep it OUT of the subprocess boundary
so it is unit-testable, the same split `checks.py` uses against its gh calls. The
verb:

- DISCOVERS files (whole tree via `git ls-files` — tracked files only, which keeps
  generated and ignored paths out of scope) and ROUTES each to a toolchain by
  extension and — for extensionless scripts — shebang (release routes shell this
  way; mirror it).
- RUNS each language's tool(s), aggregates the results, and emits one verdict. It
  is a HARD-fail check (`architecture.lex §7`): a missing tool exits non-zero, never
  skips. A clean run is `0`; any failure is `1`.
- is the SAME definition everywhere. CI runs `pixi run lint` (= this verb) and the
  lefthook pre-commit hook runs `pixi run lint`; "both agree" because it is ONE
  binary with ONE config, not two transcriptions of the rules drifting apart.

### The pixi integration (shipit's first pixi.toml)

Step 3 adds a `pixi.toml` to shipit with a `[feature.lint]` environment carrying
the linter dependencies and a `lint = "shipit lint"` task. The linters are
required-check-path tools, so they are PINNED in `pixi.lock` and CI runs
`--locked` (`architecture.lex §2`) — bumps arrive as auto-PRs, never silently.
shipit's own CI flips here from the self-contained python job to `setup-pixi` +
`pixi run lint`; the required check name (`check`) stays stable across the move —
the `ci.yml` header comment already anticipates exactly this.

The conda-forge provisioning reality: most linters are clean conda-forge packages
(ruff, shellcheck, shfmt, yamllint, go, lefthook, actionlint, golangci-lint), but
THREE are not — the same gap class Spike 0 hit with wasm-bindgen
(`lessons-learned.lex §8`):

- prettier, markdownlint-cli — npm tools. Provision nodejs from conda-forge and
  `npm install -g` the pinned versions, or pick conda-native substitutes.
- lexd — a cargo/rust binary (it lives at `~/.cargo/bin`), NOT on conda-forge.
  `cargo install lexd` at a pinned version (the wasm-bindgen pattern from Spike
  0), or vendor a prebuilt binary.
- cargo fmt / clippy — components of the rust toolchain; confirm the conda-forge
  `rust` package carries them or add the rustup components.

Resolving these is the central provisioning fork (below), not a detail: the
hard-fail rule means an unprovisioned linter FAILS the check, it does not quietly
skip.

### The lefthook caller (the unit Step 2 deferred)

A thin `lefthook.yml` with two hooks, each a one-line caller: pre-commit →
`pixi run lint`, pre-push → `pixi run lint` (and `pixi run test` once Step 5
supplies test). lefthook itself comes from conda-forge pinned in `pixi.lock`, so
release's "one runner on PATH" dance (`toolset.py` resolving the right lefthook
past `node_modules` copies) dissolves — pixi provides exactly the pinned binary.
`shipit lint` installs the git hooks (the release `--install-hook` equivalent) so
a fresh clone is one command from a working lint check.

This lefthook caller is the managed unit Step 2 explicitly DEFERRED to Step 3
(install §2, "the lefthook caller — DEFER to Step 3"). Step 3 adds it — plus the
`lint`/`test` task lines and the `[feature.lint]` deps — to install's managed set,
so a `shipit install` provisions a consumer's lint check the same way it provisions the
skills and the AGENTS.md block.

### The consumer-side question — how the lint check lands in a consumer's pixi.toml

install must get the `[feature.lint]` deps and the thin task lines into the
CONSUMER's `pixi.toml` without making `pixi.toml` a managed-but-edited drift
file — the precise hazard `architecture.lex §5` names (templating a task into the
consumer's `pixi.toml` makes the manifest a managed-but-edited file, drift on the
most important config file). The thin `lint = "shipit lint"` line is stable and
safe; the dependency pin list is the open part. Settle whether this is a
marker-delimited shipit BLOCK in `pixi.toml` (block-hashed like the AGENTS.md
block and reconciled by Step 2's algorithm) or another mechanism. This couples
Step 3 to Step 2's reconciliation and should be settled with it.

### Dogfood scope — what shipit's own lint check actually exercises

shipit's repo is python + lex + yaml + json + shell + markdown, so its own
`pixi run lint` exercises only those legs; the rust, go and tauri toolchains it
standardizes are NOT present here (the dogfood blind spot,
`lessons-learned.lex §6`). That is acceptable for Step 3 — the lint check's SHAPE and
the python/lex/shell/yaml/json/markdown legs are dogfooded for real; the
compiled-language legs are first exercised against a real consumer when install
carries the lint check outward, and fully at Step 6's reference cut.

## Decisions

The questions this PRD originally left open are now resolved and captured in
`docs/adr/0004-lint-orchestration-in-binary.md`:

- **lexd + the conda-forge gap** — prettier and markdownlint-cli are conda-forge
  deps (no npm path needed); lexd is fetched at a pin by `tools/provision-lexd.sh`,
  which the `lint`/`fmt` tasks depend on.
- **check vs fix** — check-only by default; `--fix` (= `pixi run fmt`) is the
  opt-in formatter pass. `--fix` NEVER rewrites files under a test-data
  directory (the built-in mutation guard below, #500) — a fixer corrupting a
  deliberately-malformed / byte-exact fixture is silent test breakage.
- **whole-tree vs staged** — staged-only was deliberately NOT implemented; both
  hooks call `pixi run lint`, which lints the whole tracked tree via
  `git ls-files` (a conscious simplification, not a gap).
- **path → toolchain map** — built-in by extension (the `LANGS` registry);
  routing is zero-config. The `[lint]` table now carries ONE thing — the
  consumer ignore seam below — not a routing override.
- **consumer pixi.toml integration** — a marker-delimited managed BLOCK carrying
  only the `lint = "shipit lint"` task line (no linter-dependency block).

### The consumer-owned ignore seam (#484)

A shipit-onboarded consumer OWNS some non-prose paths a managed prose linter has
no business touching: byte-exact test fixtures consumed verbatim by tests
(markdownlint `--fix` would corrupt them), generated aggregates like a built
`CHANGELOG.md` (an inline `<!-- markdownlint-disable -->` is clobbered on the
next regeneration), and vendored / upstream-synced files. markdownlint's only
path-exclusion seam, `.markdownlintignore`, is a WHOLE-FILE managed unit — a
consumer path added there is drift the next `shipit install` reconcile reverts.

The sanctioned, reconcile-safe seam is a consumer-owned `[lint].ignore` glob list
in `.shipit.toml` (the consumer-policy home, alongside `[secrets]` /
`[reviewers]`):

```toml
[lint]
ignore = ["crates/lex-babel/tests/fixtures/**", "CHANGELOG/"]
```

`shipit lint` reads it and drops matching paths from the discovered file list
BEFORE routing, so one glob excludes a path from EVERY leg (markdownlint, shfmt,
ruff, …) — Lang-agnostic, not per-linter `--ignore` plumbing. Patterns are
gitignore-style — the SAME syntax as the `.markdownlintignore` this seam
replaces, matched by shipit's own `.treeinclude` engine (`tree/include.py`), NOT
a full-path glob: `*` does not cross `/`, `**` matches any run of segments, a
trailing-slash pattern drops a directory's whole subtree (`CHANGELOG/` → every
built `CHANGELOG/*.md`, which lex needs and full-path matching could not express),
an unanchored name floats to any depth, and a leading `/` anchors to the repo
root. The seam is reconcile-safe because `.shipit.toml`
is consumer policy: `write_manifest` strips only `[shipit]`/`[managed]` and the
seed-if-absent policy pass only appends its own tables, so a `shipit install`
NEVER clobbers `[lint]`. The MANAGED `.markdownlintignore` still covers the
shipit-managed paths (`skills/`, `AGENTS.md`) plus the test-data conventions of
the built-in guard below (#500); this consumer seam ADDS a layer beside it, it
does not edit it. This is distinct from the zero-config lex-projection rule
(`lex_projections`, #436), which routes a generated `X.md` out automatically when
its `X.lex` source is tracked — the ignore seam is the explicit escape hatch for
everything that rule can't infer.

### The built-in test-data mutation guard (#500)

The consumer ignore seam above is OPT-IN — a repo that never sets `[lint].ignore`
gets no protection, and when `shipit install` writes the managed
`.markdownlintignore` it replaces any legacy per-repo ignore that used to protect
fixtures. So `shipit lint --fix` — run in a hook, in CI, or by hand — was free to
hand a deliberately-malformed or byte-exact test fixture to an in-place fixer
(`markdownlint --fix`, `prettier --write`, `shfmt -w`, `ruff --fix`, `cargo fmt`),
silently corrupting the very tests the fixture backs.

The durable fix is a built-in, always-on guard over any path under a
conventional test-data directory — `fixtures/`, `__fixtures__/`, `testdata/`,
`golden/`, `goldens/`, `snapshots/`, `__snapshots__/` (`lint.PROTECTED_TESTDATA_GLOBS`,
gitignore-style, matched by the same `.treeinclude` engine as the consumer seam).
It is enforced two ways, because the fixers come in two shapes:

- A **batch fixer** (`markdownlint --fix`, `prettier --write`, `shfmt -w`,
  `ruff --fix`) takes a file list, so the protected paths are simply dropped
  from its batch — the fixer never sees them (`drop_protected_testdata`).
- The **per-manifest Rust formatter** (`cargo fmt`) takes NO file list: it
  rewrites a whole crate, reaching a protected `.rs` via a `mod` decl (or a
  fixture that is itself a crate), and `rustfmt`'s own `ignore` config is
  nightly-only (#502). So the verb snapshots the protected `.rs` bytes before
  the fix-form run and restores any the formatter rewrote (`protected_testdata`
  + `_snapshot`/`_restore`) — the net effect is the same: the fixture is
  byte-identical after `--fix`, while real crate files stay formatted.

Two deliberate scoping choices:

- **Mutation-only.** The guard touches ONLY a mutating fix-form run, so CHECK
  mode — the CI gate — still lints these files and a genuinely-broken fixture is
  still reported; only the destructive auto-rewrite is refused. A tool with no
  fixer (shellcheck, yamllint, lexd, `cargo clippy`) still covers a fixture even
  during a `--fix` run.
- **Verb-level, tool-agnostic.** The guard lives in the verb, not per-linter, so
  it holds for the fixers that have NO ignore-file of their own (shfmt, ruff,
  cargo fmt) as well as the ones that do — a per-tool ignore would be a partial
  guarantee.

The managed `.markdownlintignore` carries the SAME conventions (both the packaged
data file and shipit's own root copy, kept byte-identical for the dogfood
reconcile-to-noop check), so deliberately-malformed MARKDOWN fixtures are spared
in check mode too — malformed markdown is a common fixture genre and flagging it
is noise about a test, not signal about a document. A consumer needing to protect
MORE paths (or a non-conventional layout) still uses the `[lint].ignore` seam.

### Editorconfig hermeticity (#493)

Pinned tool binaries make the gate reproducible only if the CONFIG context is
pinned too. `shfmt` and `prettier` both honor an `.editorconfig` — and both walk
up past the git root, so a dev's `~/.editorconfig`, an ancestor directory, or an
untracked file another tool symlinked into the working tree can flip the verdict:
the SAME commit that passes in a bare clone can demand a 200-line reformat in a
co-resident checkout (the phos-core step-9 smoke, #472). A hermetic gate must
give the same verdict regardless of checkout path or co-resident tooling.

So `shipit lint` pins the editorconfig-aware tools to the repo's TRACKED config
only. If the repo tracks NO root `.editorconfig` of its own, it runs `shfmt` with
an explicit `-i 0` (any formatting flag makes shfmt ignore `.editorconfig`
entirely; `0` is its tab default) and `prettier` with `--no-editorconfig` — so an
ambient/injected/ancestor `.editorconfig` is never consumed. If the repo DOES
track a ROOT `.editorconfig`, it OWNS its formatting config — the file travels
with every checkout, so the verdict is already commit-determined — and the tools
are left to honor it (shipit's own 4-space, indented-case shell house style
depends on shfmt reading the tracked `.editorconfig`).

The pin keys on the ROOT `.editorconfig` ONLY, never a nested one. The pin is a
single tree-wide flag (shfmt/prettier run once at the root), so honoring a nested
tracked config would require splitting their batches by editorconfig scope —
deliberately out of scope. Keying on ANY nested config instead would open a
hermeticity HOLE (codex, #493 review): a repo tracking only a nested
`.editorconfig` would disable the pin repo-wide, yet files OUTSIDE that nested
scope would still walk up and consume an untracked root/ancestor config — the very
checkout-dependence the pin exists to kill. Root-only keeps the guarantee absolute
(identical verdict everywhere, no exceptions), at the cost of the rare
nested-only-tracked-config nicety.

The pin decision is a repo-wide git FACT, read from the repo's canonical
TOP-LEVEL tracked list (resolved via `git rev-parse --show-toplevel`) — NOT from
the routed file list. It is therefore independent of both `[lint].ignore` (an
ignored path must not flip hermeticity) and the target path (`shipit lint src/`
still honors a root-tracked config), closing the ordering and subdirectory holes
the #493 review surfaced.

### Verified by

On shipit itself, `pixi run lint` and the lefthook pre-commit hook run the
IDENTICAL lint check and agree; CI runs the same `pixi run lint` under `--locked`; a
file with a deliberate lint error fails the check non-zero (proving the check is
hard, not advisory) and a clean tree passes; and a missing linter fails loudly
rather than skipping. The consumer-install leg (the lefthook caller + task lines +
feature deps added to install's managed set) is verified when Step 2's install
carries them into a test consumer.
