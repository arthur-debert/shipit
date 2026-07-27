- lint: the public `pixi run lint` now runs in the **pinned lint environment**
  (#1066). The managed task moved out of the default `[tasks]` table into
  `[feature.lint.tasks]`, so it exists in exactly one pixi environment and a
  bare `pixi run lint` resolves there — the same task, same fleet-pinned
  toolchain, that the commit/push hooks (`pixi run -e lint lint`) and the CI
  lint lane run. `pixi run lint --fix` is the fixer on that same pin.
- Why it mattered: a task in `[tasks]` reaches every environment, and the
  default one carries whatever linter versions a repo happens to resolve — so
  the documented public command disagreed with the gate it claimed to
  reproduce. On 2026-07-19 that misclassified four already-clean consumer
  reconcile PRs as prettier debt (default-env prettier 3.9 vs the pinned 3.8),
  and running the fixer that way reformatted correct files into a state the
  hook then rejected.
- **No compatibility path (one lint entry point):** the `lint-full` twin is
  retired. `shipit repo new` no longer seeds it, and shipit's own lint lane runs
  `lint`; the `fmt` alias is gone in favour of `pixi run lint --fix`. A
  consumer's next `shipit install` reconcile migrates the task in one pass —
  the default-`[tasks]` line is removed and the `[feature.lint.tasks]` block
  added together, so there is never a window with both live. A consumer that
  still carries a hand-written `lint-full` task keeps it (it is consumer-owned
  scaffold, not managed); it is now a redundant alias and can be deleted.
- Role prompts, the managed `AGENTS.md` block, the install PR text, ADR-0004,
  and the lint docstrings all name the one contract: `pixi run lint` /
  `pixi run lint --fix`, never a bare `shipit lint`.
