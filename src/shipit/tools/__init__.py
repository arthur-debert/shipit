"""The Tool model — uniform verbs over the path→toolchain map (ADR-0039).

A **Tool** is a shipit verb (``shipit test``, later ``build``, …) that walks
the repo's declared path→toolchain map (``.shipit.toml [toolchains]``,
ADR-0007) and dispatches each entry — a **Leg** — to its producing command: a
registry default per toolchain, a per-path override in the map entry. The verb
is the single implementation everywhere (laptop, lefthook hook, CI job); the
pixi task of the same name is a thin one-line caller.

Two pure, fixture-testable cores live here (the PRD's pure-cores/effectful-
shells decision, docs/prd/tol01-ci-tools.md):

- :mod:`shipit.tools.registry` — the CLOSED toolchain registry (rust / go /
  python / npm), each entry carrying its default producing command per tool
  slot. Mirrors the lint ``Lang`` registry: adding a toolchain is adding an
  entry, nothing downstream changes.
- :mod:`shipit.tools.legs` — the leg planner: (map entries, tool, optional
  leg selector, passthrough args) → ordered leg invocations, with the
  ADR-0039 selector/passthrough rules (fan-out in map order; passthrough on
  several legs without a selector is a hard error, never a broadcast).

The effectful shell — the verb that executes the planned legs through the one
exec seam (ADR-0028) and renders the report — is :mod:`shipit.verbs.test`
(WS02 adds ``build`` beside it, reusing these cores).
"""
