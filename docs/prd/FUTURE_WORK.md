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
- Step 3 — lint / fmt gate → `docs/prd/lint-gate.md`
- Step 4 — PR flow (PRF01) → `docs/prd/prf01-pr-flow.md`
- OBS01 — logging foundation → `docs/prd/obs01-logging.md`
- OBS02 — review funnel → `docs/prd/obs02-review-funnel.md`
- OBS03 — async local-review execution → `docs/prd/obs03-async-review.md`
- OBS04 — readiness engine consumes the funnel → `docs/prd/obs04-readiness-engine.md`
- FLU01 — PRF01 review follow-ups → `docs/prd/flu01-prf01-followups.md`

## Active plan — rollout

The observability spine (OBS01 → OBS04) is shipped: logging, uniform funnel breadcrumbs,
async local execution, and a readiness engine that reads the breadcrumbs + timestamps and
gates on "requested + outcome-recorded + threads-resolved", NOT "the review succeeded"
(degraded reviewers are visible-but-non-blocking; the dispatcher routes on structured
`TaskStatus` data, not `next_action` prose). With the funnel observable, gating-by-default
is now safe to roll out — which is why rollout (INS01) sits at the end of the spine rather
than the front.

| Epic | Delivers | Depends on |
| --- | --- | --- |
| INS01 | Install integration (#25): carry the `[reviewers]` policy + codex/agy App `[secrets]` mappings + pr-loop AGENTS/skills into consumers via the managed set. Plus local-reviewer rollout (#26): per-consumer App-liveness verification + gating. Safe only after OBS04. | OBS04 |

Dependency spine: OBS01 → OBS02 → OBS03 → OBS04 → INS01 (OBS01–OBS04 shipped); FLU01 (shipped) was independent.

## Postponed

- Step 5 — pixi test/build/run + changelog/release → `docs/prd/pixi-test-build-release.md`
- Step 6 — reusable workflows + release-core cutover → `docs/prd/workflows-cutover.md`
