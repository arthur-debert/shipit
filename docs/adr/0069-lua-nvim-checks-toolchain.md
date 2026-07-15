# The lua/nvim checks toolchain: busted + stylua + selene, with a leg-relative M.version bump

shipit's checks and release registries were first-class for rust / go / python
/ npm / tree-sitter, but not for **lua / Neovim plugins**. A nvim-plugin repo
had release (notes-only) but no lua checks: no lint `Lang`, no test adapter, no
version-bump projection. The in-flight lex-fmt/nvim cutover (#104) bolted an
inline `luacheck` job onto its workflow and FROZE `M.version` (shipit had no
adapter to bump it). TOL03-WS01 (#972) closes that profile gap so a nvim-plugin
repo plans a normal wf-checks matrix and bumps its version like every other
toolchain — no bespoke jobs.

## Decision

Add a `lua` entry to each of the three closed registries a toolchain profile
spans, choosing the tools that (a) a nvim plugin actually uses and (b) provision
CLEANLY through shipit's managed pixi surface — i.e. exist on **conda-forge**,
so a consumer's managed env solves them without a luarocks side-channel.

- **Test — `busted`** (`src/shipit/tools/registry.py`). The luarocks-standard
  spec runner a nvim plugin's `spec/` assertions run under. `busted` is a
  luarocks package and is **NOT on conda-forge**, so — exactly like `pytest` —
  it rides the test lane in the **consumer's own env**, never a release stage.
  No managed pixi block, no `provisions_signal`; the tool-provisioning inventory
  records it `consumer-env` (not a hole — a test-lane tool by design).

- **Lint — `stylua` (format) + `selene` (lint)** (`src/shipit/lint.py`). BOTH
  are on conda-forge (verified: `conda-forge/stylua`, `conda-forge/selene`), so
  a consumer's managed lint env solves them cleanly. This is why **selene**, not
  the classic **luacheck**: luacheck is a luarocks package NOT on conda-forge,
  so it would never provision through the managed surface. The choice is a
  *provisioning* decision (the WS brief's "pick the one that provisions cleanly
  and note why"), not a quality judgement. stylua is the one lua `--fix` leg
  (`--check` verifies, the bare form formats in place); selene is check-only (no
  autofix) and reads the consumer's own `selene.toml` (rule set + the neovim
  `std`), which a nvim plugin already ships.

- **Version bump — a pure `M.version` rewrite** (`src/shipit/release/bump.py`).
  A nvim plugin freezes its version as a string on its module table
  (`M.version = "x.y.z"` in the plugin's entry `init.lua`). The lua adapter is a
  pure edit like python's `pyproject.toml` bump — no luarocks/build tool
  invoked — writing the semver the tag names **verbatim** (a Lua string is
  arbitrary text: no PEP 440 constraint to normalize toward, the npm shape, not
  python's).

## The entry-file path: leg-relative `init.lua`

python's edit adapter names a FIXED leg-relative manifest (`pyproject.toml`); a
nvim plugin's version file is `lua/<plugin>/init.lua`, where `<plugin>` varies
per repo. Rather than add new config surface (a declared version-file key), the
lua adapter reuses the existing leg-cwd resolution: its `edit_path` is the
leg-relative `init.lua`, resolved against the lua `[toolchains]` leg's map path.

**The documented layout:** a lua repo maps its `[toolchains]` lua leg to the
plugin's Lua package directory — `lua/<plugin>` — so `init.lua` (holding
`M.version`) resolves against the leg cwd exactly as `pyproject.toml` does for
python (ADR-0007: the leg path is the consumer's `.shipit.toml` decision). A
plugin whose spec layout needs busted run from a different cwd uses the existing
per-path `test` override — no registry change.

## Build is n/a: the first buildless toolchain

A Neovim plugin is interpreted source with no compile/bundle step, so `lua`
declares an **empty `build` slot**. `shipit.tools.build.plan_build` skips a leg
whose build argv is empty rather than exec an empty command — the build analogue
of the go/tree-sitter **zero-file bump adapters**. lua is the first buildless
toolchain; the skip is per-leg, so a repo mixing a lua leg with a real build leg
still builds the real one.

## Consequences

- lex-fmt/nvim (#104) can express its checks through managed lanes (drop the
  inline `luacheck` job) and its version bumps again — the WS's consumer-side
  payoff, verified when ADP02 resumes (epic TOL03 WS06).
- The lua LINT tools (stylua/selene) are conda-forge-provisionable but are NOT
  yet wired into a managed pixi lint block (the per-language `rust-lint` /
  `go-lint` blocks' analogue, delivered on manifest detection). shipit's own
  tree has no `.lua`, so its gate is unaffected; a `pixi-lua-lint-deps-block`
  delivered on a lua signal is the natural **follow-up** for fleet provisioning
  — noted here so the gap is stated, not silent.
- The registries stay CLOSED and mirrored: `tools.registry` (6 toolchains),
  `lint.LANGS` (9 langs), and `release.bump.ADAPTERS` (one per toolchain, the
  `test_registry_mirrors_the_toolchain_set` invariant) all gain their lua entry
  together.
