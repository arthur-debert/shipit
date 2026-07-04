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

A NEW verb `src/shipit/verbs/lint.py`, attached in `cli.py` exactly like
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
  opt-in formatter pass.
- **whole-tree vs staged** — staged-only was deliberately NOT implemented; both
  hooks call `pixi run lint`, which lints the whole tracked tree via
  `git ls-files` (a conscious simplification, not a gap).
- **path → toolchain map** — built-in by extension (the `LANGS` registry); the
  optional `[lint]` override was not implemented, so routing is fully zero-config.
- **consumer pixi.toml integration** — a marker-delimited managed BLOCK carrying
  only the `lint = "shipit lint"` task line (no linter-dependency block).

### Verified by

On shipit itself, `pixi run lint` and the lefthook pre-commit hook run the
IDENTICAL lint check and agree; CI runs the same `pixi run lint` under `--locked`; a
file with a deliberate lint error fails the check non-zero (proving the check is
hard, not advisory) and a clean tree passes; and a missing linter fails loudly
rather than skipping. The consumer-install leg (the lefthook caller + task lines +
feature deps added to install's managed set) is verified when Step 2's install
carries them into a test consumer.
