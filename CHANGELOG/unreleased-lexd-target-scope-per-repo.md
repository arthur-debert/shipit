- install: the managed `[feature.shipit-lexd]` block's `[target]` set is now
  GENERATED per-repo as **(the repo's declared `[workspace].platforms`) ∩ the
  channel's served set** instead of a fixed all-of-served set (#1072). The #1068
  fix emitted a `[target.win-64.dependencies]` table on every consumer, but no
  repo currently declares `win-64` (Windows paused, #895), so pixi warned
  (`target selector 'win-64' does not match any of the platforms supported by the
  workspace`) on **every** invocation — fleet-wide noise, hit by the owner on
  shipit's own `main`. A repo now carries a `win-64` target only when it declares
  `win-64` (keeping ADR-0071's fail-closed for a real Windows consumer), and none
  otherwise, so the dangling-selector warning is gone. The served-set
  intersection (osx-64 and other unserved platforms still carry no lexd dep, so
  their lint env solves) is unchanged from #1068. The lexd version is single-
  sourced through `units.LEXD_PIN`; shipit's own manifest drops its win-64 lexd
  target. Rolled out on each consumer's next `shipit install` reconcile.
