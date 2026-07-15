# Release-verb tool provisioning inventory (TOL02-WS17, #794)

Every external tool the release verbs shell out to, with its provisioning
story — produced by walking the ADR-0028 Exec-seam argv assembly points
across prepare → build → bundle → sign → publish, after two rc-killing
unprovisioned tools were found one at a time (#784 cargo-deb, #793
cargo-edit). The drift guard (`tests/test_tool_provisioning_guard.py`) keeps
this inventory honest: a NEW Exec argv tool cannot land without a row in the
guard's registry, and the registry's pins are cross-checked against the code
and data blocks they mirror, so this document and the code cannot silently
drift apart (every tool named here is asserted present, every pin verified).

## The provisioning source vocabulary

- **runner image** — preinstalled on the GitHub hosted runner the stage's
  block pins (`ubuntu-latest`, `macos-*`); guaranteed by the image contract,
  version floats with the image.
- **setup-pixi action** — installed by the block's `prefix-dev/setup-pixi`
  step, pinned via `pixi-version` (lockstep with Layer 0's `PIXI_PIN`,
  drift-tested).
- **pixi-managed** — a shipit-managed `pixi.toml` block (`shipit install`
  reconcile) resolved by the block's `pixi run --locked` under setup-pixi's
  lockfile-keyed cache — the #582 doctrine: release runs NEVER install at
  run time.
- **self-provisioned** — installed at the Exec seam by the verb itself, at a
  module-constant pin; the recorded EXCEPTION for tools conda-forge does not
  carry (#785 precedent).
- **consumer-owned** — provisioned only by the consumer's own manifest
  today: a HOLE this inventory tracks until a per-tool fix PR closes it.
- **repo-local / dev-only** — a committed script, or a tool only dev/CI
  harness flows touch (never a release runner).

## Inventory — tool × stage × source × pin × test

Release-stage tools (each stage runs `pixi run --locked ./bin/shipit ...` on
the runner its block pins; the DEFAULT pixi env is the PATH that run sees):

| Tool (argv) | Stage(s) | Source | Pin | Fails-when-absent test |
| --- | --- | --- | --- | --- |
| `git` | all (adapter `shipit/git.py`) | runner image | floats | — (actions/checkout itself requires it) |
| `gh` | prepare reads; publish gh-release (adapter `shipit/gh.py`) | runner image + ambient `GITHUB_TOKEN` | floats | — (image contract) |
| `pixi` | every stage (the blocks' setup-pixi step; adapter `pixienv/`) | setup-pixi action | `v0.71.0` = Layer 0 `PIXI_PIN` | `test_setup_dev_env_pixi_pin_agrees_with_ci`; wf-release family pinned by the guard |
| `uv` | every stage (`bin/shipit` launcher, ADR-0033); build/bundle for python (`uv build`) | pixi-managed (`pixi.toml#shipit-launcher-deps`, closes #758) | `0.11.*` = Layer 0 `UV_PIN` minor line | `test_launcher_deps_uv_pin_agrees_with_layer0_uv_pin`, `test_load_units_includes_the_launcher_deps_block` |
| `cargo` (the binary itself) | prepare (subcommand dispatch), build (`cargo build`), publish (`cargo publish`, `cargo metadata`) | pixi-managed (`pixi.toml#shipit-rust-release-toolchain`, #801 — the sysroot-only block, see closed hole 1) | `rust` `1.96.*` (lockstep with the rust lint block) | `test_missing_cargo_binary_gets_the_reconcile_remedy` |
| cargo-edit (`cargo set-version` / `cargo update`) | prepare (rust bump) | pixi-managed (`pixi.toml#shipit-rust-release-deps`, #793/#797) | `0.13.11.*` | `test_missing_cargo_set_version_gets_the_reconcile_remedy` |
| `cargo-deb` (`cargo deb`) | bundle (deb composition) | self-provisioned (`cargo install`, #784/#785 — not on conda-forge) | `CARGO_DEB_VERSION = 3.7.0` | `test_deb_self_provisions_cargo_deb_when_missing` |
| `wasm-pack` | bundle (wasm-pack composition, TOL02-WS12 #788) | pixi-managed (`pixi.toml#shipit-rust-release-deps`, rust signal; WS10 #798) | `0.15.*` (#846: conda-forge never carried a 0.13 build) | `test_pins_agree_with_their_one_authority` (pin lockstep) |
| `rust-std-wasm32-unknown-unknown` (no argv — the wasm32 target std in the managed rust sysroot) | bundle (`wasm-pack build` compiles against it) | pixi-managed (`pixi.toml#shipit-rust-release-toolchain`, #853 — conda-forge's `wasm-pack` does NOT pull it (the WS12 claim that it did was false); a sysroot component, so it rides beside `rust` and is skipped WITH it when a consumer owns its own `rust` pin, who then self-provisions the matching std, the #759 rule) | `1.96.*` (lockstep with the `rust` line, same block) | `test_pins_agree_with_their_one_authority` (pin + rust lockstep) |
| `npm` (`nodejs`) | prepare (`npm version`), bundle (`npm pack` of the wasm-pack tree, #788), build (`npm run build`), publish (`npm publish`) | pixi-managed (`pixi.toml#shipit-node-deps`) — delivered on the node manifest signal AND on a declared `wasm-pack` composition (#788: its `npm pack` needs npm, but wasm-pack rides the rust signal and the crate's npm `package.json` is generated, never tracked, so install unions the node signal off the declaration — `Composition.provisions_signal`) | `nodejs` `26.*`, `pnpm` `11.*` | `test_missing_npm_gets_the_reconcile_remedy` (#801, closed hole 3), `test_wasm_pack_composition_delivers_the_node_deps_block` (#788) |
| `go` | build (`go build`) | runner image (ubuntu images still carry Go) | floats | none (see holes) |
| `pytest` | test lane (not a release stage) | consumer env | consumer's | — |
| `busted` | test lane (the lua toolchain's test slot, TOL03-WS01 #972) | consumer env (a luarocks package NOT on conda-forge — like pytest it rides the consumer's own env, never a release stage; no managed block, no `provisions_signal`) | consumer's | — |
| `tree-sitter` (`tree-sitter-cli`) | test lane (corpus tests, `tree-sitter test`); build (`tree-sitter generate`); bundle (tarball composition reads the generate output) | pixi-managed (`pixi.toml#shipit-tree-sitter-release-deps`, #890 — delivered on the DECLARED tree-sitter `[toolchains]` leg, `Toolchain.provisions_signal`: no manifest signals a grammar, the wasm-pack→node-deps union mechanics) | `0.25.*` (parity with the grammar consumer's `tree-sitter-cli` devDependency line — the generated parser follows the CLI's minor line, bump both together) | `test_missing_tree_sitter_gets_the_reconcile_remedy` (build-stage remedy, the #890 death site), `test_tree_sitter_toolchain_delivers_the_cli_block` |
| `twine` | publish (pypi endpoint) | pixi-managed (`pixi.toml#shipit-python-release-deps`, #801 — the python toolchain signal, closed hole 2) | `6.2.*` | `test_missing_twine_gets_the_reconcile_remedy` |
| `ruby` | publish (brew formula `ruby -c` syntax check) | runner image (ubuntu) | floats | — |
| `rattler-build` | publish (conda endpoint — `rattler-build build` repackages a final release binary into a `.conda`, `rattler-build publish` pushes+reindexes the per-repo Artifact channel; ARF01-WS01 #950, ADR-0064) | pixi-managed (`pixi.toml#shipit-rust-release-deps`, rust signal — the walking-skeleton producer lex-fmt/lex is rust; a language-agnostic endpoint-gated home is future work) | `0.68.*` (spike-validated at 0.68.0) | `test_missing_rattler_build_gets_the_reconcile_remedy` |
| `vsce` | bundle (vsix composition `vsce package --target`), publish (vscode-marketplace `vsce publish`) | consumer-owned (the extension repo's `@vscode/vsce` devDependency; no fleet block) | consumer's | — (open hole 6) |
| `ovsx` | publish (open-vsx `ovsx publish`) | consumer-owned (the extension repo's `ovsx` devDependency) | consumer's | — (wired-but-off, open hole 6) |
| `tar` | bundle (archive composition), sign (reseal payload) | runner image (ubuntu + macos) | floats | — |
| `zip` | bundle (zip archive legs) | runner image (ubuntu + macos; ABSENT on windows runners) | floats | — (windows legs out of contract, see holes) |
| `codesign` / `security` / `xcrun` / `hdiutil` | sign (mac signer unit, wf-sign-mac on `macos-*`) | runner image (Apple toolchain; notarytool ⊂ Xcode) | Xcode image version | — (image contract) |

Block-side tools (invoked by workflow-block YAML, not through the Exec seam):

| Tool | Where | Source |
| --- | --- | --- |
| `jq` | wf-prepare's plan-output transport | runner image (ubuntu + macos) |
| `gh` | (none today — release side effects all go through the verbs) | — |

Dev-side Exec tools, inventoried because the drift guard covers the whole
ADR-0028 whitelist (they never run on a release runner):

| Tool | Where | Source | Pin |
| --- | --- | --- | --- |
| `ps` | session liveness probe | OS | — |
| `gcloud` | Artifact channel store provisioner (`shipit.channel.store_provision`, ARF01-WS03) | dev-host (operator's own gcloud, opt-in infra harness — never a release runner) | — |
| `curl` | `shipit provision lexd` fetch | pixi (shipit's own default env) | `*` |
| `act` / `docker` | `shipit wf test` harness | pixi test feature (`act = "0.2.*"`) / host daemon | act pinned |
| `bin/check-e2e` | e2e harness default | repo-local committed script | — |

## Open holes (each closes via its own per-tool fix PR, #785/#797 precedent)

Holes 1–3 of the original WS17 sweep are CLOSED by #801 — kept here (struck
to one line each) so the guard notes' numbering stays stable:

1. **CLOSED (#801): `cargo`/`rust` on release runners.** Promoted into the
   default-env `pixi.toml#shipit-rust-release-toolchain` block — deliberately
   the sysroot-only block, separate from `cargo-edit`'s, so a consumer
   already pinning `rust` consumer-side (padz/lex, the pre-#801 workaround)
   trips the `PixiKeyConflict` first-splice guard on exactly that block and
   keeps both its pin AND its cargo-edit delivery (#853 adds the wasm32
   target std to the same block: a sysroot component, delivered and skipped
   with the sysroot it must match). A missing `cargo` at prepare/publish
   fails loudly naming the reconcile
   (`shipit.release.provisioning.missing_tool_remedy`).
2. **CLOSED (#801): `twine` on wf-publish.** The python toolchain signal
   (`pyproject.toml` joins `TOOLCHAIN_MANIFESTS`) delivers the
   `pixi.toml#shipit-python-release-deps` block (twine from conda-forge, the
   #797 template), and the publish dispatch loop's loud-fail translation
   names the reconcile remedy when twine is absent.
3. **CLOSED (#801): `npm` absent-provisioning probe.** The bump AND publish
   failure translations now name the reconcile remedy for a missing `npm`
   (the same `missing_tool_remedy` map; the cargo-edit
   `explain_command_failure` precedent, generalized to the missing-binary
   Exec cause).
4. **`go` release consumers.** Build-stage go rides the runner image
   unpinned; no fleet go consumer releases through the pipeline yet. When
   one onboards, promote go into a default-env managed block (the rust
   direction, same conflict caveat).
5. **`zip` on windows runners.** Windows bundle legs would compose on a
   windows runner, which ships no `zip`. Windows legs are out of contract
   fleet-wide today (#785: cross-compile lanes out of contract); the drift
   guard row records it so a windows onboarding cannot miss it.
6. **`vsce`/`ovsx` on the vscode marketplace legs (TOL02-WS13, #789).** The
   VS Code marketplace tools are node CLIs the extension repo carries as
   `@vscode/vsce` / `ovsx` devDependencies (`npm ci` → `node_modules/.bin`),
   not conda-forge packages — so they are consumer-owned today, with no
   fleet-managed block. The vsix composition's `win32-x64` leg additionally
   depends on the cross-target build (TOL02-WS11 #787) for the windows binary
   it packages. The hole closes when a real vscode consumer (lex-fmt/vscode)
   cuts an rc through the pipeline as ADP02 resumes — either onboarding a
   managed node block or ratifying consumer-owned as the deliberate posture;
   `ovsx` stays wired-but-off until that consumer's `OVSX_PAT` verifies.
7. **CLOSED (#890): `tree-sitter` CLI on release runners.** The premise was
   stale — conda-forge DOES carry `tree-sitter-cli` (all five fleet
   platforms), found when the first consumer rc (lex-fmt/tree-sitter-lex,
   WS16's live fire) died missing-binary at `tree-sitter generate`. The
   `pixi.toml#shipit-tree-sitter-release-deps` block delivers it in the
   default env, unioned off the declared tree-sitter `[toolchains]` leg
   (`Toolchain.provisions_signal` — no manifest signals a grammar, the
   #788 wasm-pack mechanics), pinned `0.25.*` in parity with the consumer's
   own devDependency line; a missing CLI at build fails loudly naming the
   reconcile.

With holes 1–3 closed, a stock consumer needs ZERO consumer-side
provisioning to traverse prepare → publish: every release-stage tool is
runner-image, setup-pixi, pixi-managed, or the recorded cargo-deb
self-provision exception. The proof is the #801 canary rc — a shipit-canary
`-release-rc` cut on stock managed blocks only — run after the canary repo's
install reconcile picks these blocks up. The lone exception is a VS Code
extension consumer, whose `vsce`/`ovsx` ride its own node manifest (open
hole 6) — a marketplace-shaped consumer, not the stock rust/python one.

Future composition tools (WS14–WS15: `electron-builder`, `tauri`; notary
tooling beyond `xcrun notarytool`) are not Exec tools yet — WS12's
`wasm-pack`, WS13's `vsce`/`ovsx` (open hole 6), and WS16's `tree-sitter`
(hole 7, closed by #890) have all landed, their rows in the inventory above.
When the
remaining workstreams land argv for their tools, the drift guard fails until
the tool gets a provisioning row — that is the guard doing its job; do not
allowlist around it.

## The drift guard

`tests/test_tool_provisioning_guard.py` holds the machine-readable registry
this table mirrors and enforces, in four directions:

1. every ADR-0028 argv-sweep whitelist head (`_ADAPTER_HOMES`,
   `tests/test_tool_argv_sweep.py`) has a provisioning entry, and vice versa;
2. a release-surface head-discovery sweep (AST, over the release verbs' and
   tool registries' modules) fails on any argv-shaped literal head that is
   neither inventoried nor explicitly declared a non-argv literal — the
   "new tool without a provisioning story" tripwire;
3. pinned sources carry pins, and each pin is cross-checked against its one
   authority (`CARGO_DEB_VERSION`, the managed block data files, the wf
   blocks' `pixi-version`, Layer 0's `UV_PIN`);
4. every inventoried tool name appears in this document.
