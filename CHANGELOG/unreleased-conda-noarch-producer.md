- release: the `conda` derived endpoint gains a **noarch mode** so cross-repo
  DATA artifacts (the tree-sitter grammar, the wasm build) ride the Artifact
  channel as `noarch: generic` conda packages (ARF02-WS07, ADR-0076; #1064). An
  artifact whose composition produces a single platform-independent archive (the
  `tarball` composition — `<artifact>.tar.gz`, no triple) repackages that one
  archive into ONE `noarch: generic` `.conda` published to the channel's
  `noarch/` subdir, which every conda client reads alongside its platform subdir,
  so no consumer change is needed. The per-platform tool-artifact path
  (`CONDA_SUBDIRS`, triple→subdir fan-out) is untouched — the modes are additive.
  The recipe extracts into a `payload/` subdir and copies only that into
  `$PREFIX/share/<package>/`, so rattler-build's build scaffolding is never swept
  into the package, and it carries no `--target-platform` (rattler-build refuses
  it for noarch — the recipe's `noarch: generic` drives it). `noarch` is a
  distinct always-present subdir (`buckets.NOARCH_SUBDIR`), NOT a member of the
  per-platform served set: the store `verify --noarch` readiness probe is a
  single `noarch/repodata.json` resolve, never a per-platform sweep and never
  subject to the ADR-0071 `win-64` pause subtraction. Covered by a REAL
  end-to-end repackage test that drives an actual `rattler-build build` (the
  #1050/#1053 do-not-fake lesson).
