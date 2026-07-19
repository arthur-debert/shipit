- lint: the managed `[feature.shipit-lexd]` block now **platform-scopes** the
  fleet-pinned `lexd` to the Artifact channel's closed served set
  (`SERVED_SUBDIRS`: osx-arm64/linux-64/linux-aarch64 and win-64) via pixi
  `[target]` tables, instead of a blanket `[feature.shipit-lexd.dependencies]`
  (#1068). The blanket dependency applied `lexd` to *every* platform a composing
  env declares, so a consumer declaring a platform **outside** the served set
  (e.g. `osx-64`, Intel Mac) hit an unsatisfiable lint-env solve (`No candidates
  were found for lexd`) and `shipit install` failed closed — no commit, no PR —
  blocking any such repo from reconciling. Platforms outside the served set now
  carry no `lexd` dep, so their lint env solves and install/reconcile no longer
  breaks. **win-64 stays in the served set** (served but owner-paused, #895):
  it keeps its target dep, so a win-64 solve still finds no channel candidate and
  **fails closed** — ADR-0071's Windows fail-closed at solve time is preserved,
  not softened to fail-open.
