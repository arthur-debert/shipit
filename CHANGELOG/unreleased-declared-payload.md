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
  composition is refused (those assemble their own contents). Two payload entries
  that name the same archive member — the same path, or one containing the other
  (`src` plus `src/parser.c`, which a directory operand already ships) — are
  refused: each member is declared exactly once.
- **The declared payload — AND the leg it rides under — are untrusted data.** The
  bundle stage refuses any symlink or junction on the whole path from the checkout
  root down: the `bundle.leg` [toolchains] directory itself, then each payload
  entry, walked one component at a time from the trusted checkout root (never from
  a base derived by resolving an untrusted path). So a committed `leg -> /etc` or a
  `leak -> /etc` payload component is refused, naming the offending component,
  before it can steer the archive at a host file — the bundle archives only real
  files and real directories, the same refuse-links, anchor-from-root model
  `shipit stage` uses, now via one shared primitive. Payload operands are also
  fenced from `tar`'s option list, so a path spelled like a flag is always read as
  a path. A payload entry of `.` (or `./`, `./.`) — which names the leg dir itself,
  not a member — is refused at config-parse.
- release/publish: the `zed` endpoint reads `extension.toml` from the leg the
  `zed` **bundle declared**, not from a hardcoded `rust` leg, and refuses a
  declaration that does not carry that manifest as a required payload entry — the
  archive and the rendered registry row always describe one extension. An
  artifact with the `zed` endpoint and no bundle still reads the crate's `rust`
  leg.
