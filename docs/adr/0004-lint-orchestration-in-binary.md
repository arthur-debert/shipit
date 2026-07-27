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
  opt-in formatter pass, exposed as `pixi run lint --fix` (pixi forwards the task's
  trailing args); only tools with a safe in-place fix participate, the rest still
  run as checks (`src/shipit/lint.py`, `Tool.fix`). The fixer deliberately has NO
  separate task name — a second entry point is a second thing to drift, and a
  fixer that can run outside the pinned environment reformats correct files into
  a state the hook then rejects (#1066).
- **Whole-tree, NOT staged-only.** Staged-only was deliberately NOT implemented.
  Both the pre-commit and pre-push hooks call `pixi run -e lint lint` — the same
  task, in the same environment, a bare `pixi run lint` resolves to — which lints the
  whole tracked tree via `git ls-files` (`lefthook.yml` and
  `src/shipit/data/lefthook.yml`; `lint.py:210-211`). Rationale (stated inline in
  `lefthook.yml`): a green hook then never lies about an unstaged edit. This is a
  conscious simplification of release's `stage_fixed` staged-only dance, not a gap.
- **Path → toolchain map is built-in by extension.** Routing is the hardcoded
  `LANGS` registry (`lint.py:120`, `lang_for` at `:132`); extensionless scripts
  route by shebang. The optional `[lint]` `.shipit.toml` override was NOT
  implemented — routing is fully zero-config.
- **Consumer pixi.toml integration is a managed BLOCK, anchored in the lint
  FEATURE.** install splices a marker-delimited block (TOML-comment markers
  `PIXI_LINT_TASK_OPEN`/`_CLOSE`) carrying only the stable
  `lint = "./bin/shipit lint"` line, block-hashed and reconciled by the install
  algorithm (ADR-0003). Its anchor is `[feature.lint.tasks]`
  (`src/shipit/data/pixi-lint-task-block.toml`), **not** the default `[tasks]`
  table where it originally landed. That placement is load-bearing, not
  cosmetic: a pixi task is reachable from every environment whose features
  declare it, and the default feature is in all of them — so a `lint` line in
  `[tasks]` ran in the DEFAULT environment while the fleet-pinned toolchain the
  binary shells out to materializes only in the `lint` environment. The public,
  documented `pixi run lint` therefore executed against whatever linter versions
  the default env happened to resolve, disagreeing with the commit hook and the
  CI lane; on 2026-07-19 that misclassified four already-clean consumer PRs as
  prettier debt (#1066). In the lint feature the task is declared once, and the
  managed `[environments]` unit composes that feature into exactly ONE
  environment, so pixi resolves a bare `pixi run <task>` to the single
  environment defining it — and `pixi run lint`, `pixi run lint --fix`, the
  hook's explicitly pinned `pixi run -e lint lint`, and the CI lane are one task,
  one environment, one gate. The one-environment half of that is an invariant of
  the manifests shipit installs, not a property the anchor can enforce alone: a
  manifest that composed the lint feature into a second environment, or declared
  its own `lint` task in another enabled feature, would make the bare form
  ambiguous again — refusing that at reconcile is #1107, and the same latent hole
  exists for the `test` task (ADR-0039). The linter-dependency block is its
  sibling unit (`[feature.lint.dependencies]`, ADP00) — that amendment predates
  this one.

## Consequences

- lefthook and `pixi.toml` stay dumb thin callers; neither carries per-language logic, so
  neither drifts.
- The orchestration is plain testable code in the package, kept out of the subprocess
  boundary so it is unit-testable (shipit's pure/boundary split).
- An unprovisioned linter fails the lint checks loudly rather than quietly skipping, so the
  checks cannot silently weaken.
- The lint-check definition cannot fork between local and CI: there is one binary, one config,
  and — since #1066 — one task in one pixi environment, so the public command cannot resolve
  a different toolchain than the gate it claims to reproduce.
