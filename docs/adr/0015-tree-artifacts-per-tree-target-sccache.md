# Tree build artifacts: per-Tree `target/`, sccache as the cross-Tree cache

> **See ADR-0017** (Trees v2): with Trees provisioned per spawn, **sccache is now
> load-bearing** — it is what keeps a per-spawn cold Tree's first build cheap — rather
> than the nicety this ADR framed it as. The deferred warm-template below stays deferred
> for the same reason (provisioning is already cheap enough).

Each **Tree** owns its build artifacts (`target/`, `node_modules/`); we do **not**
share a `CARGO_TARGET_DIR` across Trees. Cross-Tree reuse happens one layer down,
through **sccache** (content-addressed compiler output, already running portfolio-wide),
configured with two non-obvious settings so it actually pays off across Trees:
`SCCACHE_BASEDIRS` (sccache's cache key includes the absolute CWD, so without it every
distinct Tree path misses) and `CARGO_INCREMENTAL=0` (sccache disables incremental
anyway, and incremental bakes absolute paths that break under any copy). A cold Tree's
first build is therefore sccache-warm, not from scratch.

## Considered options

- **Shared `CARGO_TARGET_DIR` across Trees.** Rejected: Cargo takes a global lock on
  `target/`, so a shared dir serializes concurrent builds and thrashes incremental
  state — the opposite of what a parallel fleet needs.
- **No cross-Tree cache at all.** Rejected: every Tree would rebuild from scratch.
  sccache already exists cross-repo; reusing it costs only the two config lines.

## Consequences

- ~1 `target/` per active Tree on disk — bounded by the cleanup policy, not by the
  number of Trees ever created.
- The two sccache settings are load-bearing: omit `SCCACHE_BASEDIRS` and cross-Tree
  hit-rate collapses to zero; omit `CARGO_INCREMENTAL=0` and incremental's absolute
  paths break under reflink/copy.
- Because each Tree has its own object store (ADR-0014), there is no shared-`gc`
  concern and no need for `gc.auto=0`.
- **Future optimization (not built in v1):** a warm "template" Tree — a clone on
  `main`, kept pulled-and-built, reflink-copied (`cp -c` on APFS, ~0 time and disk,
  verified at 200 MB in 0.00 s) to spawn a Tree with `target/` already warm. Deferred
  because sccache makes cold starts cheap enough, and the template adds a freshness
  daemon, a same-APFS-volume constraint, and is a rust-only win (python/node Trees
  provision in seconds). Reach for it only if measured cold-start build time hurts.
