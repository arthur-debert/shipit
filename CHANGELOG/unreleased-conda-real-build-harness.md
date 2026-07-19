- test/conda: the conda **per-platform** producer path (`_publish_conda`) now has
  a REAL `rattler-build build` integration test — the seam that hid the four
  original conda-producer bugs (#1049 ×3 + #1052) was faked
  (`_CondaBuildRecorder` wrote an empty `.conda`), so shipit's argv/recipe were
  asserted but rattler-build never ran. `test_conda_per_platform_real_repackage_*`
  drives the real producer through an ACTUAL cross-target
  (`--target-platform` non-native) build, faking only the S3 publish, and asserts
  the `.conda` lands with the prebuilt binary at `bin/<binary>` (the
  archive-top-dir-strip class, #1049) and the no-relink guard on the recipe (the
  cross-platform relink class, #1052). Reverting fix #1049 makes the real build
  fail with `cp: <artifact>-<triple>/<binary>: No such file or directory`
  (#1053).
- test/conda: the ADR-0064 `file://` round trip (build → local conda channel →
  scratch `pixi` resolve → read the env-prefix staging path) is now automated —
  `test_conda_file_channel_roundtrip_*` plus a reusable, runnable harness
  (`tools/conda_channel_roundtrip.py`). It resolves via a PLAIN
  `[workspace]` channel+dep `pixi.toml` (not the `[artifact-deps]` projection,
  which hard-codes the GCS host), the loop ARF02 Steps 1/2 (#1078/#1079) run
  against. Both tests `skipif` cleanly when `rattler-build`/`pixi` are absent, so
  a bare host skips rather than fails.
