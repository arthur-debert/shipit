- install: the `.shipit-skills/*` files shipit delivered before the skills
  projection are now **retired** — `shipit install` deletes a pristine copy on
  the reconcile that crosses the pin (#1115). The projection (#1088) repointed
  the managed set at `.agents/skills/` but left the previously-delivered copies
  tracked in every consumer, owned by nobody: two divergent copies of each skill,
  with the **unmanaged** one carrying the stale instructions agents still read
  (the pre-#1066 copies say `shipit lint --fix` where the current instruction is
  `pixi run lint --fix`). Two consumers had already hand-cleaned it; every repo
  crossing the pin was going to pay the same tax. A hand-EDITED copy matches no
  known shipped version and is kept with a warning, as always.
- install: the skill store moved from the repo root (`.shipit-skills/`) to
  **`src/shipit/data/skills/`** — the subtraction that makes the retirement safe.
  A retired-files match is on content hash and is GLOBAL, with no producing-repo
  exemption, so with the store at the repo root shipit's own self-install would
  have deleted its own source. Moving it under `src/` drops the wheel
  `force-include` hack (the store is ordinary package data now) and the dual-root
  fallback in `skills_root()`, and leaves the retired path live nowhere. A new
  guard fails any future manifest entry that names a path still live in shipit's
  own tree. ADR-0077 is amended explicitly, not silently contradicted.
- install: the managed `.markdownlintignore` no longer narrates the retired
  `.shipit-skills/` path as "the managed tree" — a consumer could not fix that
  themselves, since editing a managed file surfaces as an OVERRIDE.
