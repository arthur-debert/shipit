# Partial-release prevention: build/sign barrier, then resumable publish

`workflows.lex §3.3` states partial-release prevention for a *single* bundle
("publish only when build+package succeeded and sign succeeded-or-skipped; never
ship a half-built set"). A real **Release** publishes *many* **artifacts** to *many*
**distribution endpoints** at once — `lex` cuts 13 artifacts to crates.io + npm +
GH release in one go. True transactional all-or-nothing is impossible there: a
crates.io publish is sequential, non-atomic, and cannot be unpublished, so once
external publishing has begun it cannot be rolled back.

## Decision

A **Release** prevents partial publication in two phases:

- **Phase 1 — build/sign barrier (all-or-nothing):** resolve-or-build (by
  **content-key**), **bundle**, and sign the *entire declared artifact set* first.
  If *any* artifact fails to build or sign, publish *nothing*. This is the only
  point where a clean abort is possible, so the whole set must clear it together.
- **Phase 2 — publish (ordered, idempotent-resumable):** only after the barrier,
  begin publishing in dependency order. Publishing is fail-fast but **resumable** —
  a re-run skips already-published artifacts and continues — because a mid-publish
  endpoint failure (an npm hiccup after three crates landed) cannot be undone, only
  retried forward. This matches the topological-crate-publish-with-backoff the
  release repo already runs.

### Alternatives rejected

- **Attempt transactional all-or-nothing across endpoints** — impossible: external
  registries have no rollback; pretending otherwise produces stuck half-releases
  with no clean recovery.
- **Per-artifact independence (each releases on its own)** — drops the barrier, so a
  Tauri app could ship while its companion CLI failed to sign; the set is meant to
  move as a version, so the barrier is across the set, not per artifact.

## Consequences

- `shipit release` separates a build/sign stage (the abort point) from a publish
  stage (resumable), and publish is safe to re-invoke after a transient endpoint
  failure without double-publishing.
- The barrier composes with ADR-0008: artifacts that hit their content-key clear the
  barrier instantly (no recompile), so the all-or-nothing build phase is cheap on a
  re-run.
