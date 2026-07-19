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
