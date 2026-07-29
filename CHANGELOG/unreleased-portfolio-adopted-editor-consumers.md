- portfolio: `lex-fmt/nvim` and `lex-fmt/zed-lex` join the `[project.portfolio]`
  fleet manifest. Both were excluded as "editor-config / grammar-packaging repos"
  while both in fact carry a `.shipit.toml` with a `[shipit] version` pin and
  managed blocks in their `pixi.toml` — they are adopted consumers, so every
  fleet-wide operation driven off the manifest (the sweep, a reconcile roll)
  silently skipped them. `zed-lex` is the worst case: 14 releases behind, no
  `[lanes]`, no `[artifacts]`, and inside the `provision lexd` refusal set, so it
  is the repo least likely to be noticed drifting. Membership in the manifest is
  ADOPTION — a `[shipit] version` pin — not repo genre; the exclusion note now
  names only `lex-fmt/comms`, which carries neither manifest on `main`.
