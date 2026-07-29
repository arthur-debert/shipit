- install/reconcile: the two pixi guards that judged the consumer's `pixi.toml`
  now judge the manifest the reconcile will **leave behind**, not the one on
  disk (#1127, #1081). Both ran inside `gather`, against the pre-update text,
  before the plan's decisions existed — the same defect in two places.
- The `provision lexd` tripwire was **blocking its own remedy**. 9 of the 13
  known fleet call sites are the `provision-lexd` task line *inside* the
  shipit-managed `[tasks]` span, whose desired content no longer defines it —
  so the reconcile would have deleted them itself. Instead `shipit install`
  refused, and the refusal named a line inside a block marked *"do not edit;
  regenerate via `shipit install`"*, instructing the operator to hand-edit
  managed content. The finding is now decided over the **projected** manifest,
  so those repos self-heal on the pin bump. A call the reconcile cannot reach —
  the consumer's own `feature.lint.tasks.lint-full` lane wiring, or a call
  inside a **declined** block — still fails closed, unchanged.
- The managed-key exemption behind the `rattler-build` migration (#1071) now
  exempts only keys that **depart** a managed block this plan actually rewrites.
  Before, any key in a present managed span was exempt — including one in a
  block the consumer **declined**, which `reconcile()` drops entirely. That let
  the conda-packager block splice a second `rattler-build` into
  `[dependencies]`, producing an unparseable `pixi.toml`. The declined donor now
  keeps its key a conflict and the receiving block is skipped.
- `ConsumerState` carries the raw `pixi.toml` text; the detections moved into
  `reconcile()`, following the `_plan_lefthook_conflicts` precedent. The prose in
  `reconcile.py`, `apply.py` and the `provision` tombstone that asserted every
  call site was consumer-authored and outside every managed block is corrected —
  it was false for 9 of 10 repos, and it is why the guard was built this way.
