- release/bundle: the `tarball` and `zed` compositions ship a **producer-declared
  payload** (ADR-0077, #1092). "Which files make up my package" is a
  producer-repo fact, so it is declared in the producing repo's own
  `.shipit.toml` — `bundle = { composition = "tarball", leg = "<toolchain>",
  payload = [{ path = "src", required = true }, { path = "queries" }] }` — and
  shipit tars exactly that under the named `[toolchains]` leg. `path` may be a
  file, a directory, or a nested path, and may name a build-produced file as
  readily as a committed one. An entry rides **when present** unless it declares
  `required = true`; a missing required entry is a loud bundle-stage failure.
- shipit no longer carries a built-in file list for either composition: the
  hardcoded `TREE_SITTER_PAYLOAD` / `ZED_PAYLOAD` tuples and the hardcoded
  `tree-sitter` / `rust` leg lookups are gone. A grammar that wants to ship one
  more file is now a config edit in its own repo, not a shipit release. The two
  compositions share one compose function and differ only in the publish
  endpoint they pair with (`zed`'s registry PR, ADR-0068).
- **No backwards compat (ADR-0077):** a `tarball`/`zed` artifact with no `leg` +
  `payload` is refused at config-parse with a migration-pointing message, never
  a silent fallback to the old shipit-side list. A `payload` in which no entry is
  `required = true` is refused too — an all-when-present payload could compose an
  empty archive, which is never a quiet outcome. `leg`/`payload` on any other
  composition is refused (those assemble their own contents).
