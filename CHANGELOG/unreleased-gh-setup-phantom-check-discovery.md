- gh-setup: required-check auto-discovery no longer invents a phantom
  `<caller> / run` context that bricks rulesets (#1056). Static discovery (the
  no-runs onboarding path) now DROPS any job whose reported check name is
  statically unpredictable — a `strategy.matrix` job (it reports `id (values)`,
  never the bare id) or a `${{ … }}` display name — instead of guessing its job
  id, warning loudly (stderr + WARNING) on every drop. The guard is
  per-workflow: gh-setup writes the ruleset only when EVERY PR workflow still
  contributes at least one certain context; if any is left with zero, discovery
  REFUSES to write (rc 1, an actionable error demanding explicit `--checks` with
  a per-workflow certain/dropped breakdown) rather than silently write a weaker
  rule. On `lex-fmt/lex` this yields exactly `check`, `checks / plan`,
  `Documentation`, `WASM build` with zero human input, and never the phantom
  `checks / run`. `--checks` override and runs-based discovery are unchanged.
