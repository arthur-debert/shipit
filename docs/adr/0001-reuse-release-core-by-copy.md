# Reuse release-core by copy, not dependency

> **Superseded in part by [ADR-0028](0028-one-exec-seam-tool-adapters.md)
> (PROC02-WS01, 2026-07):** the "two deliberate `gh` boundaries" consequence
> below no longer holds — the engine's copied boundary
> (`shipit/prstate/ghapi.py`) was merged into the ONE `gh` Tool adapter,
> `src/shipit/gh.py`, which now carries the REST/GraphQL helpers, the
> pagination-merging helper, and the PR-flow acts. The copy-not-dependency
> decision itself, and the Divergences ledger below, still stand; this ADR is
> kept as the historical record of why the engine arrived with its own
> boundary.

shipit reuses release-core's proven code (the `gh` boundary, the verbs, and —
in Step 4 / epic PRF01 — the whole `prstate` PR-state engine) by **copying the
source into shipit's own slim package and re-skinning its entry points**, never
by depending on the published release-core wheel. The global no-backwards-compat
/ clean-fork principle wins over the wheel dependency an earlier roadmap draft's "`pixi add`
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

## Divergences

Because the copy is no longer kept byte-for-byte in lockstep with upstream, any
deliberate extension of the copied engine beyond release-core's shape is recorded
here, so a future re-copy knows what NOT to clobber:

- **OBS04 (`prstate/state.py` `TaskStatus`)** — `TaskStatus` is extended with
  `reviewer_funnel`: structured per-reviewer funnel data (the native
  `ReviewLifecycle` paired with the OBS02/ADR-0005 funnel check-run breadcrumb,
  plus the WS02 normalized `FunnelState`), and with `degraded`: the set of required
  reviewers settled at a non-success terminal outcome (failed / empty / timed-out),
  surfaced loud but non-blocking. The snapshot (`PullContext`) likewise gains
  `review_funnel` + an injected `now`. This lets the OBS04 readiness engine redefine
  readiness over *settled* (outcome-recorded, not review-succeeded) and lets
  `pr next` route on structured state rather than `next_action` prose (issue #24.1).
  Upstream release-core has none of these fields. See ADR-0006 and the module note
  in `prstate/state.py`.
- **PROC02-WS01 (`prstate/ghapi.py` deleted)** — the engine's own `gh` boundary
  is gone: the engine now calls the single `gh` Tool adapter (`shipit.gh`,
  ADR-0028), which absorbed `ghapi`'s REST/GraphQL/pagination helpers and the
  PR-act calls. A future re-copy must NOT restore `ghapi.py`; port any upstream
  `ghapi` fix into `shipit/gh.py` instead. The stdlib-only guarantee that once
  justified the separate boundary holds across the merge (`shipit.gh` and the
  `execrun`/`redact` runner under it are stdlib-only).
