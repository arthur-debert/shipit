- lint: the managed `[feature.shipit-lexd]` block now **platform-scopes** the
  fleet-pinned `lexd` to the Artifact channel's served subdirs
  (osx-arm64/linux-64/linux-aarch64) via pixi `[target]` tables, instead of a
  blanket `[feature.shipit-lexd.dependencies]` (#1068). The blanket dependency
  applied `lexd` to *every* platform a composing env declares, so a consumer
  declaring an **unserved** platform (e.g. `osx-64`, Intel Mac) hit an
  unsatisfiable lint-env solve (`No candidates were found for lexd`) and
  `shipit install` failed closed — no commit, no PR — blocking any such repo from
  reconciling. Scoping keeps ADR-0071's "unserved platforms fail closed" (lexd is
  simply absent on osx-64/win-64, so lint cannot run there — already true under
  the retired `provision lexd`) **while the env still solves**, so install and
  fleet reconcile no longer break on those platforms.
