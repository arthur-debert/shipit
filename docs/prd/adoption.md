# Adoption — fleet rollout of shipit (ADP00 / ADP01 / ADP02)

Status: planned. Supersedes INS01 (FUTURE_WORK) as the active rollout plan; its
remaining leftovers (reviewer-App liveness per org) fold into ADP00.

## Problem Statement

The core feature set for adoption is in place, but every consumer repo in the
portfolio still runs the legacy release tooling and none is onboarded. Rolling out
means dozens of touchpoints per repo (install, gh-setup, Trees, session bootstrap,
hooks, pixi tasks, lint, CI workflows, release), multiplied across the fleet —
hundreds of things that must work. Without a disciplined, phased approach this
becomes death by a thousand cuts: agents fighting half-working tooling on every
run, slow and expensive sessions that "reach the goal" while masking defects, and
issues that take forever to isolate through the three-layer indirection (shipit
code → root coordinator → consumer coordinator → its subagents).

Two further problems compound it:

- The two halves of adoption are asymmetric. The **local** half (install,
  gh-setup, Trees, session bootstrap, lint, log/eval) is built and dogfooded; the
  **CI** half (reusable workflows, act harness, pixi test/build/release
  encapsulation, release pipeline) is specced but unbuilt — consumer CI still
  calls the legacy release repo's reusable workflows.
- A set of consumer-facing breakages is *already known* (the managed lefthook
  assumes a lint environment consumers don't have; the linter binaries have no
  consumer delivery path; lexd has no delivery path; `shipit lint` has no rust
  leg; the lex-mirror hook deletes the managed AGENTS.md block). Discovering
  these one agent-run at a time would burn exactly the tokens and momentum the
  disciplined approach is meant to protect.

## Solution

Adopt in three strictly-ordered epics, local before CI, known-fixes before any
consumer:

- **ADP00 — shipit-side pre-work.** Fix every known blocker on main before
  touching a real consumer; the headline is a new governing principle: **the
  managed set owns the consumer environment** — dependencies, environments, and
  activation arrive as install-managed `pixi.toml` blocks, pinned to identical
  versions fleet-wide. Also: mint the tracking issue (five status tables), write
  the two survival prompts, and dry-run the full local checklist on the canary
  consumer.
- **ADP01 — local adoption, fleet-wide.** Per repo, a rigid nine-step checklist
  (install PR merged → gh-setup → `.treeinclude` → lint / test / build → Tree +
  session verified → one real agent task through the PR loop), each step with an
  explicit verified-by. The order is rigid because Tree creation and session
  launch fail closed on a non-onboarded repo. Sequencing (the canary is
  completed inside ADP00): lex → phos-core → phos-app → dodot → rest of
  lex-fmt → others.
- **ADP02 — CI adoption: build, then adopt.** First build the missing machinery
  in shipit, verified against the first real consumer (actionlint Lang, the act
  harness with an explicit known-unsupported surface, the thin checks caller,
  pixi test/build/release encapsulation, the release pipeline). Then per repo:
  re-point thin callers from the legacy release workflows one toolchain at a
  time with the required-check name held stable, act-test each workflow, verify
  remotely (an agent-driven PR through merge, then an rc cut through the full
  release pipeline), and only then remove the legacy release tooling from the
  repo and comb its agent memory.

Throughout, the working discipline is **stop-fix-restart**: at any tooling
friction in a verification run, stop the run, fix shipit or the managed set,
start fresh. Evidence — `shipit logs --flow` and the eval record — is read after
every run; a goal reached while fighting the tooling is a failed adoption run.

## User Stories

1. As the portfolio owner, I want adoption split into local-first then CI, so that risk is bounded and each repo delivers immediate local gains before any workflow surgery.
2. As the portfolio owner, I want all known consumer-facing breakages fixed in shipit before the first real consumer onboards, so that agent runs verify adoption instead of rediscovering documented defects.
3. As a consumer repo maintainer, I want `shipit install` to deliver the complete lint environment (tools, versions, environment definition) into my `pixi.toml`, so that `pixi run lint` works on a fresh clone with nothing pre-installed.
4. As the portfolio owner, I want every consumer pinned to the same tool versions from one packaged source, so that a version bump is one edit that rolls the fleet on the next install reconcile.
5. As a CI job, I want the consumer environment fully declared in the repo's manifest, so that `setup-pixi --locked` reproduces the laptop environment exactly and laptop/CI parity is structural.
6. As a consumer repo maintainer, I want the managed `lefthook.yml` to work on a stock consumer, so that my first commit after install doesn't fail on repo-specific scripts or missing environments.
7. As a consumer repo maintainer, I want lexd provisioned by a shipit subcommand at a fleet-pinned version, so that the lex Lang works in my repo without me tracking a binary that isn't on conda-forge.
8. As a rust consumer repo, I want `shipit lint` to carry rust Langs (clippy, rustfmt), so that commit/push checks cover my primary toolchain from day one of adoption.
9. As a lex-using consumer, I want the lex-mirror hook to preserve the shipit-managed AGENTS.md block, so that my first `.lex` edit doesn't silently strip the agent contract.
10. As a coordinator agent in a consumer repo, I want a documented, supported way to have shipit on PATH, so that the bootstrap launcher resolves without improvisation.
11. As the portfolio owner, I want the reviewer-App installs verified live for every org before that org's repos onboard, so that the review funnel works on the first PR.
12. As the root coordinator, I want a canary consumer to pass the entire local checklist before any real repo starts, so that pure mechanics are shaken out for near-free.
13. As the root coordinator, I want a per-repo local checklist with an explicit verified-by per step, so that "adopted" is a set of observed facts, not an impression.
14. As the root coordinator, I want the per-repo order enforced (install merged → gh-setup → Trees/sessions), so that fail-closed Tree provisioning never presents as a mystery breakage.
15. As a consumer repo maintainer, I want re-running install and gh-setup to be clean no-ops, so that update and onboarding are the same operation and drift is visible as reconcile outcomes.
16. As a consumer repo maintainer, I want my repo's gitignored-but-needed files declared in `.treeinclude`, so that fresh Trees are complete and sessions don't fail on missing secrets.
17. As the portfolio owner, I want each repo's lint debt cleared in one dedicated commit by a dedicated agent before lint goes blocking, so that adoption and feature agents never inherit unrelated lint noise.
18. As the root coordinator, I want `pixi run test` (with its per-repo variants) and `pixi run build` recorded and green per repo, so that the local bar covers the tasks agents actually run.
19. As a session agent in a consumer repo, I want `claude-start` to produce a working session Tree (correct branch, injected files, active pixi env, live hooks emitting events), so that consumer sessions match the shipit-repo experience.
20. As the root coordinator, I want one small real task driven through the full PR loop as the final local step, so that adoption is verified by the workflow it exists to serve.
21. As the root coordinator, I want to read `shipit logs --flow` and the eval record after every verification run, so that tooling fights are detected even when the goal was reached.
22. As the root coordinator, I want a stop-fix-restart rule with teeth, so that workarounds never substitute for fixes that would otherwise fire N more times across the fleet.
23. As the root coordinator in shipit, I want a survival prompt for myself and one for the in-consumer coordinator, so that both layers use the tooling correctly and friction bubbles up verbatim instead of being laundered into "done".
24. As the in-consumer coordinator, I want my role framed as an instrument (adoption is testing the tooling on me), so that I stop and report friction rather than improvising around managed files.
25. As the portfolio owner, I want five status tables in one tracking issue (pixi tasks × stack, local adoption × repo, CI workflows × stack, remote CI × repo, bird's-eye), so that fleet progress is legible at a glance and updated at every state change.
26. As the portfolio owner, I want the fleet manifest to be `.shipit.toml`'s `[project.portfolio]` table (ADR-0033), seeded by a one-time sweep of the three owners, so that "the fleet" is an enumerated, version-controlled list rather than memory — the tracking issue's bird's-eye table is a human status view derived from it, never the authority.
27. As a workflow author, I want actionlint as a lint Lang, so that workflow YAML errors are caught locally in milliseconds before any act run or push.
28. As a workflow author, I want an act harness that runs one workflow/job locally under containers with crafted event payloads, so that iterating on CI does not require pushing to find out.
29. As a workflow author, I want the act howto to state explicitly what act cannot verify (macOS/Windows runners, cross-workflow cascade, partial workflow_call, dispatch UX), so that local green is trusted only where valid.
30. As a consumer repo, I want my checks workflow to be a thin caller of shipit's reusable workflow running the same pixi tasks as my laptop, so that CI adds routing, never logic.
31. As the portfolio owner, I want consumer callers re-pointed from the legacy release workflows one toolchain at a time with the required-check name held stable, so that branch protection never breaks mid-migration.
32. As the portfolio owner, I want remote verification per repo to be an agent-driven PR through merge plus an rc cut through the full release pipeline, so that "CI adopted" means the real loop ran end to end.
33. As the portfolio owner, I want the legacy release tooling removed from a repo only after its remote verification passes, and the repo's agent memory combed afterwards, so that retirement never precedes proof and no stale guidance lingers.
34. As the portfolio owner, I want release-core retired only after shipit cuts one real release of one real consumer, so that the standing cutover gate stays in force.
35. As a planner, I want adoption work to target post-CLI02 main and rely only on its frozen external surface, so that in-flight CLI work never destabilizes adoption runs.

## Implementation Decisions

- **Three epics, one per phase**: ADP00 (pre-work), ADP01 (local, fleet-wide),
  ADP02 (CI build-then-adopt). Strictly ordered; ADP01 completes horizontally
  (full fleet) before ADP02 begins per-repo cutover. FUTURE_WORK is updated to
  point at them and mark INS01 superseded.
- **The managed set owns the consumer environment** (governing principle). The
  install verb's managed-unit registry grows sibling `pixi.toml` marker blocks
  alongside the existing tasks block: a lint feature/dependency block and its
  environment definition, carrying the fleet-pinned tool set (ruff, shellcheck,
  shfmt, yamllint, prettier, markdownlint, lefthook). This explicitly amends the
  lint PRD's "task line only, no dependency block" decision. Reconciliation
  semantics are unchanged: same pristine-hash outcomes (ADD / NOOP / UPDATE /
  OVERRIDE), consumer-edited blocks surfaced, never clobbered.
- **Canonical versions live in a packaged data block, with a drift check**:
  shipit's own manifest keeps its hand-written lint environment, and a test
  asserts the packaged block and shipit's own environment agree — shipit
  dogfoods exactly what the fleet receives; a version bump is one data edit.
- **lexd delivery is a shipit subcommand** (provision verb): pinned version and
  fetch logic live in the binary, consistent with lint-orchestration-in-binary
  (ADR-0004); the managed environment block invokes it as a task. No distributed
  script to reconcile. External fetch goes through the exec seam (ADR-0028).
- **The managed `lefthook.yml` becomes consumer-generic**: no references to
  shipit-repo-local scripts; every invoked task/environment is satisfied by the
  managed blocks. The lex-mirror leg is not part of the managed variant (repos
  that want it add it themselves); the AGENTS.md-clobbering defect in that hook
  is fixed as an ADP00 precondition for lex-using consumers.
- **Rust Langs (clippy, rustfmt) join the closed Lang registry** in ADP00 —
  adding an entry, nothing downstream changes (ADR-0004/0007 shape). go and
  tauri-specific legs are deferred to the repos that force them (dodot,
  phos-app) during ADP01/ADP02.
- **The pinned `bin/shipit` launcher is a documented story, not new machinery**:
  ADP00 documents the one supported install path for laptops and runners in the
  survival guide — the managed launcher resolving `.shipit.toml`'s
  `[shipit].version` pin via `uv tool run` (ADR-0033), NOT a pixi-dependency
  bootstrap. The launcher mechanism itself lands in the pin-core work, not here.
- **The local adoption bar is lint + test (+ build for compiled repos)**. No
  `run` task (not canon; per-repo optional), no local `release` task (arrives
  with ADP02's pixi encapsulation).
- **"Repo defines a `test` task" is an explicit checklist prerequisite of the
  test step (#444)**: the managed task block deliberately does not own `test`
  (repo-specific), and on a manifest without one `pixi run test` falls through
  to the POSIX `test` shell builtin — silent exit 1, zero output,
  indistinguishable from a red suite. The step's verified-by starts with the
  task existing (the session-start hook warns when it is missing); the managed
  set never ships a fallback/no-op `test` task.
- **Per-repo order is rigid**: install PR merged to main → gh-setup →
  `.treeinclude` → task verification → Tree/session verification → agent smoke.
  Motivated by fail-closed Tree provisioning on non-onboarded repos.
- **Agent smoke is the final local step**: one small real task through the full
  PR loop (draft → review → ready), judged by the durable record (`shipit logs
  --flow`) and the eval record, not by goal completion alone.
- **Lint-debt clearing is a sanctioned break-glass admin push**: one dedicated
  commit per repo by a dedicated agent, keeping implementing/coordinating
  contexts free of lint noise.
- **Sequencing**: canary (inside ADP00) → lex (rust, multi-crate, multi-artifact;
  also hosts ADP02's machinery build-out, so it is expected to take
  disproportionately long by design) → phos-core → phos-app (hardest
  composition; its sign leg is act-untestable and therefore remote-heavy) →
  dodot → rest of lex-fmt → others.
- **ADP02 is build-then-adopt**: the machinery (actionlint Lang, act harness
  with printed known-unsupported surface, thin checks caller, pixi
  test/build/release encapsulation, release pipeline with the three scar
  invariants) is built in shipit and verified against lex; per-repo adoption
  afterwards is thin. Cutover canon stays in force: shipit workflows run
  alongside the legacy ones; required-check name stable; release-core retires
  only after one real release of one real consumer.
- **Status lives in one GitHub tracking issue** (five tables: pixi tasks ×
  stack, local adoption × repo, CI workflows × stack, remote CI × repo,
  bird's-eye), updated at every state change, never checked in. The bird's-eye
  table is a human status VIEW, seeded from a one-time sweep of the three owners;
  the machine-readable fleet manifest is `.shipit.toml`'s `[project.portfolio]`
  (ADR-0033; CONTEXT.md's Portfolio term), which the sweep reconciles against.
- **Survival prompts are ADP00 artifacts**: a shipit-side coordinator prompt
  (indirection discipline, stop-fix-restart, evidence reading, table updates)
  and an in-consumer coordinator prompt (tooling contract, instrument framing,
  verbatim bubbling). Embedded in the epic issues and dry-run on the canary.
- **CLI02 interaction**: adoption targets post-CLI02 main; only the frozen
  external surface is relied on (plus the exit-code contract and `--json`
  additions where useful to tooling).

## Testing Decisions

- Good tests here assert external behavior: reconcile outcomes on a synthetic
  consumer repo, lint routing and hard-fail behavior, block content — never
  installer internals.
- **New managed units** (env/feature blocks, consumer-generic lefthook) are
  tested through the existing install-reconcile test pattern: fresh install
  ADDs, unchanged re-install NOOPs, consumer edit surfaces OVERRIDE.
- **Drift check** is a test asserting the packaged environment block agrees with
  shipit's own lint environment (the dogfood guarantee).
- **Rust Langs** follow the existing Lang test pattern (routing by
  extension/toolchain, hard-fail on findings, `--fix` opt-in where applicable).
- **The provision subcommand** keeps a pure decision core (version pin, target
  resolution, idempotence) tested in isolation; the fetch/exec boundary is
  injected per the one-exec-seam contract and faked in tests.
- **Adoption itself is evidence-verified, not unit-tested**: the per-repo
  checklist's verified-by column is the test plan; `shipit logs --flow` and eval
  records are the observations. A checklist step without observed evidence is
  not done.
- Prior art: the install reconcile suite, the lint Lang suite, and the exec-seam
  fakes already in the test tree.

## Out of Scope

- Building the pinned `bin/shipit` launcher mechanism itself (the pin-core work
  under ADR-0033, via `uv tool run` — not a pixi dependency); ADP00's docs pass
  only documents the supported install path, it does not build the launcher.
- Resolving self-install (`shipit install .`) — explicitly unresolved in
  ADR-0003 and unaffected here.
- go and tauri-specific lint legs (deferred to the adopting repos that force
  them).
- Content-addressed artifact store details (WF02) and the cascade — ADP02
  verifies releases through the pipeline as specced; build-once reuse and
  cross-repo cascade remain their own epics.
- Retiring release-core itself — gated behind the standing one-real-release
  rule; ADP02 removes legacy tooling per-repo only after that repo's remote
  verification.
- Cloud-deploy endpoints (e.g. Cloud Run) — out of scope as previously decided.

## Further Notes

- INS01's seed issues are closed but its scope (reviewer policy and App secrets
  into consumers, per-org App liveness) was never verified fleet-wide; the
  liveness check is an ADP00 item and the rest rides the normal install payload.
- The act howto's "what act cannot verify" section is load-bearing: phos-app's
  sign/notarize leg can never go green locally, so its remote verification
  budget is structurally larger.
- The machine-readable fleet manifest is `.shipit.toml`'s `[project.portfolio]`
  table (ADR-0033; CONTEXT.md's Portfolio term) — the version-controlled,
  stack-grouped list of repos a sweep iterates, reads pins from, and measures
  rollout against. The tracking issue's bird's-eye table is a human status VIEW
  derived from it (plus swept-but-not-onboarding repos), never the authority; a
  hand-edited GitHub issue cannot be the machine source of truth (WS07's
  "bird's-eye is the manifest" framing was corrected in ADP00-WS15).
- **Execution record (ADP00)**: the canary dry-run (#420) surfaced the
  tool/managed-set lag window live — a machine-global, auto-updating shipit left
  committed managed files stale against the running tool and leaked reconcile
  commits onto feature branches — and opened seven convergence workstreams
  (WS09–WS15: seeded `pixi.toml` #432, managed lint configs #436, the gh-setup
  ruleset 422s #438/#441, armed Trees #443, the ADR-0033 pin core #447,
  convergence #449) plus ADR-0033 itself, whose repo-pinned `bin/shipit`
  launcher removes the lag by construction. That lag window is the recorded
  learning.
- Death-by-a-thousand-cuts is the named failure mode; every process decision
  above (pre-fixed blockers, checklist with verified-by, stop-fix-restart,
  evidence reading, single tracking issue) exists to prevent it.
