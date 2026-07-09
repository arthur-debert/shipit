"""The Tool model — uniform verbs over the path→toolchain map (ADR-0039).

A **Tool** is a shipit verb (``shipit test``, ``build``, ``e2e``, …) that
dispatches declared work to its producing command: the tree-input tools walk
the repo's path→toolchain map (``.shipit.toml [toolchains]``, ADR-0007) per
**Leg**; the artifact-input ``e2e`` walks the ``[artifacts]`` map instead —
a registry default per entry, a declared override in the map. The verb is
the single implementation everywhere (laptop, lefthook hook, CI job); the
pixi task of the same name is a thin one-line caller.

The pure, fixture-testable cores live here (the PRD's pure-cores/effectful-
shells decision, docs/prd/tol01-ci-tools.md):

- :mod:`shipit.tools.registry` — the CLOSED toolchain registry (rust / go /
  python / npm), each entry carrying its default producing command per tool
  slot (``test``, ``build``). Mirrors the lint ``Lang`` registry: adding a
  toolchain is adding an entry, nothing downstream changes.
- :mod:`shipit.tools.legs` — the leg planner: (map entries, tool, optional
  leg selector, passthrough args) → ordered leg invocations, with the
  ADR-0039 selector/passthrough rules (fan-out in map order; passthrough on
  several legs without a selector is a hard error, never a broadcast).
- :mod:`shipit.tools.build` — the build-step planner: the leg × artifact-map
  join (target narrowing, go's env, the ADR-0041 version injection).
- :mod:`shipit.tools.e2e` — the e2e planner, on the artifact axis: the
  harness registry with its bats default, the ``<NAME>_BIN`` derivation,
  and the declared binary's expected location.

One deliberately NON-pure module sits beside them:
:mod:`shipit.tools.artifact_source` — the artifact-source seam (the WF02
boundary) whose local-build source runs the planned build steps through an
injected exec runner.

The effectful shells — the verbs that execute the planned work through the
one exec seam (ADR-0028) and render the report — are
:mod:`shipit.verbs.test`, :mod:`shipit.verbs.build`, and
:mod:`shipit.verbs.e2e`, reusing these cores.
"""
