- release/bundle: the `tarball` composition now BUILDS the grammar's
  `tree-sitter-<parser>.wasm` (`tree-sitter build --wasm`) and ships it at the
  archive root, alongside `tree-sitter.json` and the editor-bundle manifest
  `shared/embedded-grammars.json` (both added as when-present payload entries)
  — restoring the v0.11.2 tree-sitter tarball union the shipit cutover dropped
  (#1078). The shipit-managed `tarball` had shipped the generated C `src/` +
  queries only, so the wasm consumers (lex-fmt/vscode, lex-fmt/lexed) could not
  migrate off `fetch-deps`. The wasm build needs a wasm backend (emscripten on
  PATH or Docker) on the bundle leg, exactly as the legacy `tree-sitter.yml@v3`
  build job required; a run that produces no wasm is a hard bundle-stage
  failure, never a silent source-only archive.
