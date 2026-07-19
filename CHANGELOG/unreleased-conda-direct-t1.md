- release/conda: the conda endpoint now packages a producer's build output
  **directly** into a `.conda` (conda-direct, ADR-0077, #1092) — the served
  subdirs, target triples and staged archive names are DERIVED from the
  artifact's own `[artifacts.<name>].platforms` declaration (the causal single
  source), never reverse-engineered from a staged filename. The gh-release→conda
  repackage coupling is gone: the build output is staged by the bundle stage and
  conda has NO gh-release dependency, so a conda-only publish plan is valid (the
  release-before-derived ordering constraint that required an unskipped
  gh-release for conda is removed; brew/notify-downstreams keep theirs). Both the
  per-platform and the noarch (`noarch: generic`) paths are covered, and the
  load-bearing `binary_relocation: false` no-relink recipe insight is preserved.
