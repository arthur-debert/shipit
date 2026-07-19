- stage: new `shipit stage` — the generic, manifest-driven stage-from-prefix step
  (conda-direct T3, #1079). After `shipit install`/`pixi install` resolves a
  conda dependency into the pixi env prefix, an app-type consumer declares a
  `[stage.<pkg>]` map of source-in-prefix → dest-under-`resources/` copies (a
  tool binary at `bin/<tool>`, a data artifact under `share/<pkg>/…`), and this
  step copies each file or directory into the shipped bundle — files keep their
  mode so a tool binary stays executable. It replaces the legacy cross-repo
  `fetch-deps`/`deps.json` fetch with only the source axis swapped (a gh-release
  download becomes a read of the already-resolved env prefix); the copy is
  durable and idempotent, and refuses loudly on an unresolved source or a
  destination that would escape the checkout. Distinct from the release-time,
  transient vsix `bundle.stage` map — the two share only the env-prefix resolver
  and the checkout-escape guard, never a parallel mechanism.
