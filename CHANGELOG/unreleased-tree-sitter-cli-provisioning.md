- Provision the `tree-sitter` CLI on release runners (#890, closing the
  TOL02-WS17 provisioning inventory's open hole 7): `shipit install` now
  delivers a managed `pixi.toml#shipit-tree-sitter-release-deps` block
  (conda-forge `tree-sitter-cli`, pinned `0.25.*` in parity with the grammar
  consumer's devDependency line) whenever a repo declares a tree-sitter
  `[toolchains]` leg — no manifest signals a grammar, so the declaration is
  the signal, the same union mechanics as the wasm-pack→node-deps delivery.
  A pixi-managed builder missing at `shipit build` now fails naming the
  install reconcile that provisions it, instead of a bare not-found note.
