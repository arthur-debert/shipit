# Reuse release-core by copy, not dependency

shipit reuses release-core's proven code (the `gh` boundary, the verbs, and —
in Step 4 / epic PRF01 — the whole `prstate` PR-state engine) by **copying the
source into shipit's own slim package and re-skinning its entry points**, never
by depending on the published release-core wheel. The global no-backwards-compat
/ clean-fork principle wins over the wheel dependency the ROADMAP's "`pixi add`
the existing package" wording superficially implies: shipit is the *slimmed,
renamed successor* to that engine, not a consumer of it, and the slow/fast split
(architecture.lex §2) ships the PR-loop code through the `shipit` package itself.

## Consequences

- "Do not rewrite the state machine — re-skin its entry points only" is taken
  literally: `prstate/` is copied verbatim, including its own stdlib-only
  `ghapi` boundary. shipit therefore keeps **two deliberate `gh` boundaries** —
  `shipit/gh.py` (verb-layer: gh-setup/install repo mutations) and
  `shipit/prstate/ghapi.py` (the engine's PR-introspection boundary). The small
  `rest()`/pagination overlap is intentional duplication, the price of not
  rewriting the engine's I/O.
- No release-core wheel ever appears in shipit's dependency graph; bug fixes are
  ported by re-copying, not by version bumps.
