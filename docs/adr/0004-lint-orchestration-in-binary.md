# Lint orchestration lives in the binary

The lint command's per-language discovery, routing, and aggregation live in the
`shipit lint` **binary** — not in lefthook, and not templated into the consumer's
`pixi.toml`. This inverts release-core's shape, where lefthook is the orchestrator
carrying the per-language glob map. In shipit, lefthook and pixi stay thin one-line
callers (`pixi run lint` → `shipit lint`), and all the rich logic sits in the versioned
package.

The reason is pixi's missing seam: pixi has **no cross-manifest task inheritance**, so a
consumer cannot inherit or override a task shipit defines elsewhere. The only way to put a
rich task into a consumer is to template it into that consumer's `pixi.toml` — which makes
the manifest a managed-but-edited file, i.e. drift on the most important config file. Put
the logic in a binary instead and the consumer's `pixi.toml` carries only a stable,
never-drifting one-line task.

It is a **hard-fail check**: a missing tool fails non-zero, never skips. There is exactly one
lint definition, so CI's `pixi run lint` and the local pre-commit hook run the identical
binary with the identical config — "both agree" because there is one transcription of the
rules, not two. Full rationale is in `docs/dev/architecture.lex §5` (why a binary, not
templated tasks) and `§7` (lint checks: one definition, hard).

## Resolved details (carried from docs/legacy-prd/lint-checks.md)

- **Provisioning / the conda-forge gap.** prettier and markdownlint-cli ARE on
  conda-forge, so the anticipated npm path is unnecessary — every linter is a
  pinned conda-forge dep in `[feature.lint.dependencies]` (`pixi.toml`: ruff,
  shellcheck, go-shfmt, yamllint, prettier 3.8.\*, markdownlint-cli 0.49.\*,
  lefthook). The one exception **was** **lexd** (not on conda-forge), fetched at
  a pin by `tools/provision-lexd.sh` (v0.18.4 from the `lex-fmt/lex` GitHub
  release, with checksum pinning); the `lint`/`fmt` tasks
  `depends-on = ["provision-lexd"]` so CI and the hook provisioned it identically.
  **Superseded by ADR-0066 (ARF02-WS06):** `lexd` now rides the Artifact channel
  as an ordinary conda dependency in a managed `[feature.shipit-lexd]` pixi block
  (locked and sha256-verified via `pixi.lock`, like every other linter); the
  `provision` module/verb, the `provision-lexd` task, and `tools/provision-lexd.sh`
  are gone. The rest of this ADR's decision (lint logic in the binary) stands.
- **Check vs fix.** Lint is CHECK-ONLY by default (release's scar: a
  formatter under `--all-files` silently rewrites untouched files). `--fix` is the
  opt-in formatter pass, exposed as `pixi run fmt`; only tools with a safe
  in-place fix participate, the rest still run as checks
  (`src/shipit/lint.py`, `Tool.fix`, `pixi.toml` `fmt` task).
- **Whole-tree, NOT staged-only.** Staged-only was deliberately NOT implemented.
  Both the pre-commit and pre-push hooks call `pixi run lint`, which lints the
  whole tracked tree via `git ls-files` (`lefthook.yml` and
  `src/shipit/data/lefthook.yml`; `lint.py:210-211`). Rationale (stated inline in
  `lefthook.yml`): a green hook then never lies about an unstaged edit. This is a
  conscious simplification of release's `stage_fixed` staged-only dance, not a gap.
- **Path → toolchain map is built-in by extension.** Routing is the hardcoded
  `LANGS` registry (`lint.py:120`, `lang_for` at `:132`); extensionless scripts
  route by shebang. The optional `[lint]` `.shipit.toml` override was NOT
  implemented — routing is fully zero-config.
- **Consumer pixi.toml integration is a managed BLOCK.** install splices a
  marker-delimited block (TOML-comment markers `PIXI_OPEN`/`PIXI_CLOSE`, anchored
  under `[tasks]`) carrying only the stable `lint = "shipit lint"` line — never a
  linter-dependency block, since the linters ride in as the shipit package's own
  deps (`install.py:56-63`, `:190-200`; `src/shipit/data/pixi-tasks-block.toml`).
  Block-hashed and reconciled by the install algorithm (ADR-0003).

## Consequences

- lefthook and `pixi.toml` stay dumb thin callers; neither carries per-language logic, so
  neither drifts.
- The orchestration is plain testable code in the package, kept out of the subprocess
  boundary so it is unit-testable (shipit's pure/boundary split).
- An unprovisioned linter fails the lint checks loudly rather than quietly skipping, so the
  checks cannot silently weaken.
- The lint-check definition cannot fork between local and CI: there is one binary, one config.
