# LNT01 — Lint hermeticity: one config, injected, ambient-blind

> Status: **planned** (2026-07-06). Feature #495.
> Decisions: `docs/adr/0037-the-gate-owns-the-config.md` (the gate injects one
> canonical config + scrubs ambient env; divergence is normalized, not
> accommodated). Builds on `docs/adr/0036-the-gate-owns-style.md`,
> `docs/adr/0004-lint-orchestration-in-binary.md`, `docs/adr/0028-one-exec-seam-tool-adapters.md`.
> Beachhead merged: #493 / PR #494 (`c2552230`). Precedes ADP02 (#422).

## Problem Statement

The lint verdict must not depend on whose machine it runs on, which project it
runs in, or what other tooling wrote into the working tree. Today it does:
every linter, when it runs, hunts for config — the current directory, ancestor
directories, `$HOME`/XDG, tool-specific environment variables — so a stray
`~/.markdownlint.json`, a `.prettierrc` two folders up, or a `SHELLCHECK_OPTS`
export silently changes the answer. We have shut that door for exactly **2 of
~10 registered tools** (shfmt + prettier's `.editorconfig`, via #493), and even
that was done per-repo (honor the repo's own tracked config, neutralize an
ambient one) rather than uniformly.

The deeper problem is recurrence. Reproducible linting has been attempted ~20
times and always regressed. Every attempt was reactive and per-tool, with **no
artifact that stayed green** — so the next tool, a version bump, or a new
ambient source silently reopened a door, and the leak surfaced in a consumer
smoke six repos downstream instead of in shipit. A fleet survey of all 19
managed repos (2026-07-06) confirmed the shape of the debt: Rust is greenfield
(no repo commits rustfmt/clippy config), ruff config exists in exactly one
place (embedded in shipit's `pyproject.toml`), the only genuine *rule* conflict
is prettier (four TypeScript repos disagree on `semi` / `trailingComma`), and
the layout variation is real but bounded (a Tauri split, a TS monorepo, three
non-standard Rust crate layouts).

## Solution

**The gate owns the config (ADR-0037).** shipit ships one canonical config per
tool; the gate injects it into every invocation and scrubs the ambient
environment at the one `_run_tool` exec seam, so nothing outside the repo is
ever consulted. A property test parametrized over the tool registry proves the
verdict cannot move under a hostile ambient config, and gates shipit CI — the
artifact that ends the 20-times cycle.

**Standardize maximally by normalizing the repos, not the gate.** The default
for every divergence is *eliminate it*: unify prettier's rules and fix the
resulting violations; move Rust members to the standard `crates/*` layout — as
long as the repo stays buildable and testable. Only structurally irreducible
differences (Tauri's `src-tauri/`, the TS monorepo's packages, generated
`parser.c`) are accommodated, each concretely (gate subtree-targeting, or the
`[lint].ignore` escape hatch). The whole standard is inspectable and workable
**locally** — `shipit lint` on a laptop is byte-for-byte the CI check.

Two phases, mirrored in the work streams:

1. **Standardize maximally** — define the canonical config set, build the
   gate mechanism + the invariance test, then normalize the fleet to the
   standard (prettier unify, Rust relayout, config adoption + debt clear).
2. **Handle the irreducible residue** — the structural exceptions the gate
   accommodates, and the legacy `release`-CI path reconciliation the layout
   moves require.

## User Stories

- As a **maintainer**, when I run `shipit lint` locally I get the exact verdict
  CI gets, regardless of what config files exist in my `$HOME` or above the
  repo.
- As a **contributor to any fleet repo**, the lint rules for my language are the
  same as every other repo's — I never discover a per-repo `.prettierrc`
  disagreement in review.
- As a **shipit developer**, when I register a new linter, the invariance test
  fails until I have injected its config and scrubbed its env — I cannot ship a
  leaky tool.
- As a **fleet operator**, adopting shipit in a new repo means adopting the
  canonical configs; there is no per-repo lint-rule negotiation, only the
  `[lint].ignore` file-scope list.

## Implementation Decisions

### The gate injects config (mechanism 1)

Generalize the `editorconfig_pin` field on the `Tool` dataclass
(`src/shipit/verbs/lint.py`) into an always-applied config-injection: each tool
carries the flag(s) that pin it to shipit's canonical config (`--config <path>`
for ruff/prettier/markdownlint/yamllint; explicit flags for shellcheck/shfmt;
`--config-path` for rustfmt; clippy lints on the command line). Unlike
`editorconfig_pin` (gated on whether the repo tracks its own `.editorconfig`),
injection is unconditional — the canonical config is the only config. Canonical
config bodies ship as shipit data and are materialized/pointed-at at lint time.

### One env scrub (mechanism 2)

`_run_tool` is the single choke point through which every linter subprocess
runs (`lint.py:638`, sole caller of `execrun.run` in the gate). It gains a
scrubbed environment — built once, passed via `execrun.run(env=..., replace_env=True)`
(the mechanism the Tree provisioner already uses) — dropping `$HOME`, `XDG_*`,
`*_CONFIG*`, `SHELLCHECK_OPTS`, `YAMLLINT_CONFIG_FILE`. No new plumbing in
`execrun` (it already forwards `env`/`replace_env`).

### The invariance property test (mechanism 3 — the acceptance gate)

In `tests/test_lint.py`, generalize the existing
`test_shfmt_verdict_is_hermetic_across_ambient_editorconfig` template:
parametrized over the `LANGS` registry, for each tool plant a hostile config in
an ancestor dir + `$HOME` + the tool's env var and assert the verdict is
identical to the clean run. Green + gating in CI is the acceptance criterion for
the whole epic.

### Canonical config set

- **ruff** — carve `[tool.ruff.lint]` out of shipit's `pyproject.toml` into a
  standalone `ruff.toml` (the de-facto fleet baseline: `B`/`UP`/`I`); seed the
  same file fleet-wide; add it to `mkdocs-lex` (which has none).
- **Rust** — greenfield: define one canonical `rustfmt.toml` + clippy lint set,
  inject everywhere (no repo commits one today).
- **Go** — promote `supage`'s `.golangci.yml` (its lone holder) to canonical.
- **prettier** — one canonical `.prettierrc`: `singleQuote: true`,
  `printWidth: 100`, `tabWidth: 2`, `semi: false`, `trailingComma: none`, with
  the svelte + tailwindcss plugins (a *capability* to parse `.svelte`, verified
  inert where there is no `.svelte`), applied to all four TS repos.
- **universals** (markdown/json/yaml/gh-actions/shell/lexd) — confirm the
  already-managed configs are the canonical set; add markdownlint + yamllint to
  `lex-fmt/lex` (the only repo missing them).

### Normalize, don't accommodate

- **prettier rules** — no per-repo divergence; reformat + fix violations in
  `lexed`, `vscode`, `simple-gal-ui`, `phos-editor/app`.
- **Rust layout** — `rustloc` (members at root dir names) and `clapfig` (root is
  also a crate) move to `crates/*`; confirm `simple-gal` (lone single-crate) is
  covered by the root invocation.
- **phos.photo** — correct its misclassification (no Python; static site).

### Irreducible residue (accommodate concretely)

- **phos-editor/app (Tauri)** — Rust under `src-tauri/` (structural); the gate
  targets that subtree for Rust and root `src/` for TS/Svelte.
- **lex-fmt/lexed (TS monorepo)** — tsc/eslint fan out over the packages'
  manifests, not a single root tsconfig.
- **lex-fmt/tree-sitter-lex** — generated `parser.c` goes to `[lint].ignore`;
  decide C scope (add clang-format, or universal-linters-only).

### Legacy `release`-driven CI

Many fleet repos still run CI through the legacy `~/h/release` tooling. Layout
normalization (Rust relayout) and config injection may move paths that
`release` assumes; reconcile those so local `shipit lint` and CI stay identical.
(Turning off the `release` project's canary requirements is noted as a separate,
out-of-scope follow-up.)

## Work Streams

Phase-1 foundation (shipit code + data), then fleet normalization, then residue.

- **WS01 — Gate injects config + scrubs env.** `Tool` config-injection field +
  the `_run_tool` env scrub. The tool-agnostic mechanism. (shipit)
- **WS02 — Invariance property test.** Registry-parametrized hermeticity test,
  gating CI. The acceptance alarm. (shipit) *Depends on WS01.*
- **WS03 — Canonical config set.** Carve shipit's ruff into `ruff.toml`; define
  Rust rustfmt+clippy; promote Go golangci; the unified `.prettierrc`; confirm
  universals. (shipit data) *Feeds WS04–WS06.*
- **WS04 — Prettier unification.** Adopt the canonical `.prettierrc` + reformat
  - fix violations across the 4 TS repos; each a green PR. (fleet) *Depends on WS03.*
- **WS05 — Rust layout normalization.** `rustloc` + `clapfig` → `crates/*`;
  confirm `simple-gal`; each a green PR. (fleet)
- **WS06 — Fleet config adoption + debt clear.** Seed the canonical configs
  across all 19 repos (add lex's md/yaml, mkdocs-lex's ruff, fix phos.photo's
  classification); clear surfaced debt; each repo green. (fleet) *Depends on WS03.*
- **WS07 — Irreducible residue.** Gate subtree-targeting for the Tauri split +
  TS monorepo; generated-code `[lint].ignore`; the C-scope decision. (shipit + config)
- **WS08 — Legacy `release` CI path reconciliation.** Update `release` CI logic
  where WS05 layout moves or WS01 injection change paths; keep local == CI.
  (release repo) *Depends on WS05.*

## Testing Decisions

- The **invariance property test** (WS02) is the epic's acceptance gate: green +
  gating in shipit CI, iterating over every registered tool.
- Each **fleet** WS (WS04–WS06) keeps the touched repo's own test + lint suite
  green through its PR — no normalization lands red.
- WS07's residue handling is covered by extending the invariance test to the
  subtree-targeted invocations (the verdict for `phos-app`'s `src-tauri/` Rust
  and root TS is each hermetic).

## Out of Scope

- **ADP02 (#422)** — making the gate a required CI check fleet-wide (this epic
  makes it uniform + proven; ADP02 rolls it out as the gate).
- Turning off the `release` project's canary requirements (separate follow-up).
- New linters/languages beyond the current registry (the mechanism covers
  whatever is registered; adding tools is later work).
- Raising the lint *floor* (new rule families) — that is ADR-0036's rule-adoption
  seam, independent of hermeticity.

## Depends on

- #493 / PR #494 (`c2552230`) — the `editorconfig_pin` beachhead this generalizes.
- ADR-0036 (the gate owns style), ADR-0004 (lint in the binary), ADR-0028 (one
  Exec seam).
- The fleet layout inventory (2026-07-06 survey) recorded in this PRD's problem
  statement.
