- spawn: a failed child's exit reason now reaches the operator (#1153). The
  refusal read `stderr` only, and a headless `claude -p` reports its own errors
  on **stdout** — which the failure path discarded entirely. Five of six
  implementer Runs launched for one epic exited `rc=1` rendering as a bare
  `error: claude child exited 1` with nothing else, so the coordinator learned
  nothing about any of them; two had done correct, complete work that was
  recovered only by hand-inspecting the Trees. A nonzero child now reports a
  bounded tail of **both** streams, each labelled, stdout first because that is
  where the common failure lands. A child that wrote to neither says so
  explicitly and names the Tree to open and how long it ran before dying — 204s
  of silence and 2s of silence are different failures, and the exit code alone
  distinguishes neither. The `uncommitted=N` salvage note, which is what made
  the two recoveries possible, rides alongside it unchanged; the durable record
  gains `stdout_bytes` / `stderr_bytes` so "the child said nothing" is
  answerable from the log without re-running anything.
- spawn, review: measuring a child stream's size for the durable record can no
  longer raise on undecodable output. A lone surrogate in the stream raised
  `UnicodeEncodeError` from inside the failure handler itself, masking the child
  failure being reported with a traceback; the same shape was live in the review
  producer's timeout/transport handler.
- spawn: the parent-project env scrub moved INTO `launch()`, so no launch site
  can omit it. The reviewer producer and the calibrator both built their child
  env straight from the backend adapter and never scrubbed, handing a child
  rooted in its own read-only Tree a `PIXI_PROJECT_MANIFEST` naming a manifest
  that belongs to a **different** Tree — the cross-Tree leak rooting exists to
  prevent, visible as pixi's `WARN Using local manifest … rather than … from
  environment variable` on every hook invocation inside the Tree. It is cosmetic
  today only because pixi recovers by preferring the local manifest. Every
  `launch()` caller launches a Tree-rooted child, so rooting the scrub in the
  seam rather than in each caller is what stops the next call site
  reintroducing it.
