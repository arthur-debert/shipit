# shipit — Future Work

The roadmap is retired; per-capability PRDs under `docs/prd/` are the source of truth,
and this file is the high-level map across them. Two standing rules carried over from
the roadmap:

- shipit must stay a useful, shippable tool after each shipped step; nothing later breaks
  what an earlier step delivered.
- Do NOT retire release-core until shipit has cut one real release of one real consumer.

## Shipped

- Spike 0 — pixi runs the rust+tauri toolchain. Verified 2026-06-25 (macOS+Linux CI). No PRD (a spike); rationale in `docs/dev/lessons-learned.lex §8`.
- Step 1 — gh-setup → `docs/prd/gh-setup.md`
- Step 2 — install + reconciliation → `docs/prd/install-reconciliation.md`
- Step 3 — lint / fmt checks → `docs/prd/lint-checks.md`
- Step 4 — PR flow (PRF01) → `docs/prd/prf01-pr-flow.md`
- OBS01 — logging foundation → `docs/prd/obs01-logging.md`
- OBS02 — review funnel → `docs/prd/obs02-review-funnel.md`
- OBS03 — async local-review execution → `docs/prd/obs03-async-review.md`
- OBS04 — readiness engine consumes the funnel → `docs/prd/obs04-readiness-engine.md`
- FLU01 — PRF01 review follow-ups → `docs/prd/flu01-prf01-followups.md`

## Active plan — adoption

The rollout plan is `docs/prd/adoption.md`: fleet adoption in three strictly-ordered
epics, local before CI, known-fixes before any consumer. It supersedes INS01 (its seed
issues #25/#26 are closed; the unverified leftover — per-org reviewer-App liveness —
folds into ADP00, and the rest of its payload rides the normal install set).

| Epic | Delivers | Depends on |
| --- | --- | --- |
| ADP00 | shipit-side pre-work: the managed set owns the consumer environment (install-managed pixi env/dep blocks, fleet-pinned versions); consumer-generic lefthook; lexd provision subcommand; rust lint Langs; lex-mirror AGENTS.md fix (#363); documented shipit-on-PATH story; App-liveness check; tracking issue + survival prompts; canary passes the full local checklist. | CLI02 (`docs/prd/cli-api-separation.md`) |
| ADP01 | Local adoption fleet-wide: per-repo nine-step checklist (install PR → gh-setup → `.treeinclude` → lint/test/build → Tree + session → agent smoke through the PR loop), evidence-verified via `shipit logs --flow` + eval. Sequencing (canary completes inside ADP00): lex → phos-core → phos-app → dodot → rest. | ADP00 |
| ADP02 | CI adoption, build-then-adopt: actionlint Lang, act harness + howto, thin checks caller, pixi test/build/release encapsulation, release pipeline (absorbs Steps 5–6 / WF01 scope, verified against lex); then per-repo cutover — re-point callers one toolchain at a time, act-test, remote-verify (agent PR + rc cut), remove legacy release tooling, comb memory. | ADP01 |

Dependency spine: OBS01 → … → OBS04 (shipped) + CLI02 (`docs/prd/cli-api-separation.md`) → ADP00 → ADP01 → ADP02.

## Absorbed into the active plan

Formerly postponed steps whose execution now rides ADP02; their PRDs remain the spec of record.

- Step 5 — pixi test/build/run + changelog/release → `docs/prd/pixi-test-build-release.md` (execution slot: ADP02's build half)
- Step 6 — reusable workflows + release-core cutover → `docs/prd/workflows-cutover.md` (execution slot: ADP02; the one-real-release retirement gate stands)
