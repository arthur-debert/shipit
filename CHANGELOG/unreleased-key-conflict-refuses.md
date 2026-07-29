- install: a consumer key shadowing a managed pixi block now **refuses** instead
  of warning and exiting 0 (#1116). A block that could not be **delivered** was
  being treated as a block that did not need delivering — the same shape as #1101
  one layer up — so the repo silently under-delivered its managed set (a pin, a
  task: the guard is not pin-specific) while the reconcile reported success.
  16 of 20 portfolio repos were in exactly that state, all over pins; three
  found it only by hand-inspecting a v1.6.0 bump.
- The supported override is **declaring** it: `[managed.decline].keep` in
  `.shipit.toml` already exists for precisely this, and a declined block is no
  longer a conflict at all (it will never be spliced), so the intent lands in
  version control where it is reviewable instead of in a warning line nobody acts
  on. The refusal names the key, the block, and both remedies — deletion first,
  because that is the right answer for most of them: 13 of the 16 colliding repos
  carried a stale hand-pin whose own comment named the shipit gap the managed
  block has since closed.
- Scoped to KEY conflicts. `PixiTaskConflict` stays warn-only — shipit's own repo
  carries a deliberate, documented `test`-task conflict, so refusing there would
  make shipit refuse to install itself (a dogfood test now pins that) — and
  `PixiTableConflict` stays warn-only with a fleet-wide count of zero rather than
  shipping an untested refusal. Both are stated in the code, not left implicit.
- Every applying mode fails closed, the working-tree refresh included: like the
  `provision lexd` tripwire, the finding is about the state of the consumer's
  manifest rather than about publishing. The guard runs before the no-op
  shortcut, because the common shape is a repo whose managed set is otherwise
  current — the plan carries no work and would otherwise exit 0.
