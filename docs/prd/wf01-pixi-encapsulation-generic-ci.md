# WF01 — Pixi encapsulation + generic CI

> Epic: **WF01** (theme WF — Workflows). Status: **planned**.
> Spec source of truth. Glossary: `CONTEXT.md` (build/release section). Decisions:
> `docs/adr/0007-repo-as-path-toolchain-map.md`, `docs/adr/0010-reusable-workflows-and-producing-logic-home.md`;
> design rationale `docs/dev/{architecture,workflows}.lex`. Map: `docs/prd/FUTURE_WORK.md`.
> First epic of the Workflows family; unblocks WF02..WF06.

## Problem Statement

A portfolio repo's CI today is a thin caller into one of N per-toolchain reusable
workflows in `arthur-debert/release` (`rust-ci`, `tauri-ci`, `electron-ci`,
`nvim-plugin-ci`, …). Each re-derives the same `checkout → provision → run → collect
→ post` skeleton in a slightly different flavor, and the provisioning lives in
`apt-get` / `setup-node` / `dtolnay` — so the only way to find out whether a CI
change works is to push and watch a runner (the "monte carlo" loop, ~20 min per
iteration). A maintainer cannot run the commit/push checks the way CI runs them, cannot test a
workflow edit locally, and cannot add a new project shape without touching the
central repo.

## Solution

shipit models a repo as a `.shipit.toml` **path→toolchain map** and drives all of CI
through a single generic reusable workflow that is essentially `setup-pixi` +
`pixi run <task>` over a declared **lane** matrix. Provisioning becomes pixi
environments; build/test/lint become uniform-named pixi tasks the consumer supplies
behind a stable interface; per-**toolchain** difference hides behind those names so
the workflow stays generic. The same `pixi run` invocations run on a laptop, in
local Docker, and in CI — and an `actionlint` check plus an `act` harness let a
maintainer validate workflow edits locally before pushing. A consumer's CI shrinks
to a thin caller of `arthur-debert/shipit@vN`, upgraded by bumping one version.

## User Stories

1. As a portfolio maintainer, I want to declare each build-bearing path's toolchain
   in `.shipit.toml`, so that shipit knows how to provision and run that part
   without me hand-writing CI.
2. As a maintainer, I want one repo to carry several toolchains at once (a Tauri app
   = rust + npm + mkdocs), so that multi-part repos are first-class rather than
   forced into a single project type.
3. As a maintainer, I want `pixi run test` / `pixi run build` / `pixi run lint` to
   mean the same thing in every repo, so that the generic CI workflow and lefthook
   stay dumb and never drift per-repo.
4. As a maintainer, I want CI to run the exact same `pixi run` invocations as my
   pre-commit hook, so that "CI is the source of truth" is one definition, not two
   transcriptions that drift.
5. As a maintainer, I want to run a CI **lane** locally and get the same result, so
   that I stop pushing release candidates just to see if a step works.
6. As a maintainer, I want provisioning (rust, node, webkit2gtk, mold/lld,
   wasm-bindgen) to come from pixi, so that a clean machine and a clean runner
   provision identically and reproducibly.
7. As a maintainer, I want to declare CI lanes (`{ run, required, local, trigger,
   runner, scope }`), so that the generic workflow fans them into jobs without me
   writing YAML per lane.
8. As a maintainer, I want the **commit/push checks** (required∩local lanes —
   `lint` + `test`) to run in pre-commit and CI identically, so that a missing tool
   hard-fails the same way everywhere.
9. As a maintainer, I want `actionlint` in the lint check, so that a bad expression,
   broken `needs:`, or malformed matrix is caught in milliseconds locally, not after
   a 20-minute push.
10. As a maintainer, I want an `act`-based local runner for a single workflow/job
    with crafted event payloads and inputs, so that I can validate the thin routing
    YAML without a runner — within act's known limits.
11. As a maintainer, I want to be told exactly what `act` cannot reproduce
    (macOS/Windows runners, GPU, cross-workflow cascade, partial `workflow_call`),
    so that I trust local green only where it is meaningful.
12. As a maintainer, I want my consumer CI workflow to be a thin caller of
    `arthur-debert/shipit@vN`, so that upgrades are a one-line version bump with no
    vendored workflow copy to drift.
13. As a maintainer adding a new toolchain (e.g. zig), I want to add a registry entry
    rather than fork the workflow, so that nothing downstream changes.
14. As a release engineer, I want the required-check name to stay stable when a repo
    moves from the release-repo workflow to the shipit workflow, so that branch
    protection does not break mid-cutover.
15. As a maintainer, I want `shipit` to validate my `.shipit.toml` toolchain/lane
    declarations (unknown toolchain, missing path, duplicate lane), so that a typo is
    a clear local error, not a confusing CI failure.
16. As an agent driving a PR, I want CI to be fast and locally reproducible, so that
    I spend my time iterating rather than waiting on runners.
17. As a maintainer of a docs-only repo (comms), I want the same model to cover an
    mkdocs toolchain with a Pages lane, so that docs sites are not a special case.
18. As a maintainer, I want lanes to declare their **scope** (thin/full) so that an
    expensive lane runs thin on an unrelated PR and full on nightly/dispatch.

## Implementation Decisions

- **toolchain-map config (deep module).** Extend the `.shipit.toml` parser to read
  the **path→toolchain map**, the **lane** declarations, and (stubs consumed later)
  the artifact/endpoint declarations. Pure parse + validate (unknown toolchain,
  missing path, duplicate lane name, bad scope) returning a typed model; no I/O.
  Mirrors the existing `config.py` / `prstate/reviewers_config.py` split.
- **toolchain registry.** A closed registry of toolchains (rust, npm, mkdocs, go,
  wasm, …), each carrying its provisioning expectation and the uniform task names it
  expects (`build`/`test`/`lint`). Same shape as `verbs/lint.py`'s `LANGS` and
  `prstate/reviewers.py`'s adapter registry — adding one is adding an entry
  (ADR-0007).
- **lane planner (deep module).** Pure function `(declared lanes + event +
  path-diff) → ordered job matrix`, each job tagged with its lane, **scope**
  (thin/full), runner, and required/local flags. Mirrors `prstate/state.py`
  `evaluate()`: snapshot in, plan out, no network. The generic workflow consumes the
  emitted matrix.
- **commit/push checks = required∩local lanes.** `shipit lint` and the
  consumer-supplied `test` task are the two canonical check lanes. Lefthook calls the
  commit/push checks; CI runs all lanes. Refines, does not redefine, the existing
  **commit/push checks** (architecture.lex §7).
- **Generic reusable workflow (thin routing).** Published from
  `arthur-debert/shipit@vN` (ADR-0010): `setup-pixi` + emit the lane matrix +
  `pixi run <lane task>` per job + collect/post. Routing only — matrix, artifact
  up/download stubs, secret injection. No shell logic in YAML.
- **actionlint into the lint check.** Add a workflow/actionlint **Lang** to
  `verbs/lint.py`'s registry so `shipit lint` (hence pre-commit and CI) lints
  workflow YAML. Hard-fail check: missing `actionlint` exits non-zero (architecture.lex §7).
- **act harness (`shipit wf test` / a pixi task).** A wrapper that runs one
  workflow/job under `act` in catthehacker containers with an input-file and crafted
  event payload, and prints act's known-unsupported surface so a local pass is
  trusted only where valid. Decision logic (which job, which event, payload
  assembly) is a testable pure core; the `act` invocation is the injected boundary.
- **Provisioning carried by pixi features** per Spike 0: webkit2gtk4.1 (+ glibc
  system-requirement), mold/lld, wasm-bindgen via pinned SHA-verified download
  (the `tools/provision-lexd.sh` pattern), zlib/expat tail for native-GUI consumers.
- **Required-check-name stability.** The generic workflow's required job name is held
  stable across the cutover so the branch ruleset keeps matching (the same move the
  current `ci.yml` migration already documents inline).

## Testing Decisions

- A good test asserts external behavior, not implementation: feed a recorded
  `.shipit.toml` + event + diff, assert the emitted lane matrix / parsed model /
  validation error — never reach into private structure or run a real runner.
- **Unit-tested (pure cores):** toolchain-map config (parse + every validation
  error), the toolchain registry resolution, the lane planner (event × scope ×
  required/local → matrix), and the act-harness decision logic (job/event/payload
  assembly). Prior art: `tests/prstate_fixtures/` JSON snapshots + the
  `evaluate()`/`dispatch()` pure-decision tests, and `test_prstate_reviewers_config.py`.
- **Fixture corpus:** a small set of `.shipit.toml` declarations spanning a
  single-toolchain rust-cli and a multi-toolchain tauri repo, each with an event +
  diff, asserting the planned matrix.
- **Not unit-tested (validated by actionlint + act):** the generated reusable
  workflow YAML itself; covered by the actionlint check and a smoke `act` run in CI.
- The `act` invocation, `setup-pixi`, and real provisioning are integration concerns,
  exercised by running the checks on ≥2 real toolchains, not mocked in unit tests.

## Out of Scope

- The **content-key** / artifact resolve-or-build store — WF02 (this epic's lanes
  may build naively; cross-revision reuse is WF02).
- The deep test/quality dimension — GPU runners, native-WebDriver e2e, required-vs-
  non-required nuance beyond the lane flag — WF03.
- Changelog/release/sign/distribution — WF04/WF05.
- The cross-repo cascade — WF06.
- The `supage` repo's server **deploy** (its Go services shipped to Google Cloud
  Run) — a *deploy*, not a **Release**, so it falls outside the artifact→endpoint
  model. Its `supage` CLI artifact is in scope under the normal model; the Cloud Run
  deploy stays bespoke in-repo.

## Further Notes

- This epic is the foundation: WF02 keys reuse on the artifacts WF01's lanes produce;
  WF03 enriches the lane model; WF04/05 add the composable release/publish jobs
  alongside this generic CI workflow.
- The work runs alongside the existing release workflows; nothing is retired here
  (the standing rule: release-core retires only after WF06's real-release proof).
- Sequencing: the Workflows family follows the observability spine (OBS01–04 → INS01)
  in `FUTURE_WORK.md`; WF01 is authored now but executes after that spine.
