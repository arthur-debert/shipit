# Redesign the CLI surface; leverage release's composable engine functions

A clean, consistent command-line and config experience is a primary goal of the shipit
reset — so shipit does NOT preserve release-core's CLI verbatim. Instead it splits the
reuse along a layer line:

- **Interface layer (reset / redesign):** the CLI commands, their args/options, output
  shapes, and config names are designed fresh for consistency across all of shipit. The
  PR-flow verbs (`pr status` / `pr next` / `pr ready` / `pr review …`) are **one
  shipit-native CLI convention** — the same inline `@root.command()` + `run(...) -> int`
  shape the setup verbs (`gh-setup`, `install`, `lint`) already use. shipit carries ONE
  verb convention, not two. ("Re-skin the entry points" = a new clean interface over the
  unchanged engine, NOT a passthrough wrapper that freezes release's interface.)
- **Implementation layer (copy / leverage):** the composable, tested engine functions are
  copied faithfully and called from the new CLI — `evaluate`, `gather`, `detect`,
  `evaluate_breakers`, `resolve_reviewers`, and the adapters. The genuinely tricky logic
  currently entangled in release's CLI modules (the `#614` request-attach verify poll, the
  guarded draft→ready re-check, the `TaskState`→next-action classification) is EXTRACTED
  into composable helpers we call — extracted, not rewritten.

This keeps the value (the state machine and its hard-won edge-case logic) while the surface
a user touches is clean and uniform. "Adapt the primitive, don't half-hack it" governs the
engine functions; it does not require keeping release's command surface.

## Consequences

- One CLI convention across shipit; no `wrap_verb` passthrough layer is imported.
- The PR-flow CLI verbs are thin: parse clean args → call copied engine helpers → render.
  Where release buried logic in a CLI module, that logic is lifted into a callable helper
  first, so the verb stays thin and the helper stays unit-testable (shipit's pure/boundary
  split).
- Config is part of the redesign: one policy file (`.shipit.toml`) with consistent table
  shapes (`[reviewers]` mirrors `[secrets]`), not release's `.release-sync.yaml`.
