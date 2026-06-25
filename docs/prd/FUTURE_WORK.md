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

## Active plan — observability first, then rollout

The PR-review path today is synchronous and blocking, with zero logging and no durable
record of the review funnel on the PR itself. When a review is broken, absent, empty, or
times out, none of those outcomes is distinguishable from "never requested" — so a PR can
silently park with no signal to anyone about why. The fix is an observability spine that
makes the PR itself the source of truth: piggyback GitHub as the state store (bot comments +
timestamps), with no daemon and no local state to keep in sync. Only once the funnel is
observable does gating-by-default become safe to turn on, which is why rollout (INS01)
sits at the end of the spine rather than the front.

| Epic | Delivers | Depends on |
| --- | --- | --- |
| OBS01 | Logging foundation — real `logging`, a predictable bounded file sink + level control + quiet CLI default + a CI sink. | — |
| OBS02 | Uniform funnel breadcrumbs on the PR — bot comments for requested / arrived / failed / empty, timestamped, isomorphic across app and local reviewers. | OBS01 |
| OBS03 | Async local execution — fire-and-forget detached run that posts result/failure back to the PR; the request returns immediately. No daemon, no local state. | OBS02 |
| OBS04 | State machine consumes the new info — reads breadcrumbs + timestamps, applies a wait window (per-backend, 20m global fallback); the Ready gate is "requested + outcome-recorded + threads-resolved", NOT "the review succeeded". Degraded reviewers are visible-but-non-blocking. Absorbs the deferred dispatcher finding (route on structured `TaskStatus` data, not on `next_action` prose). | OBS02, OBS03 |
| FLU01 | Small follow-ups from the PRF01 review (issue #24): graphql() doc-scope note; review diff stale-base hardening; pixi `review` extra (pyjwt) materialized in the env; configurable per-backend review timeout. | — (free-floating) |
| INS01 | Install integration (#25): carry the `[reviewers]` policy + codex/agy App `[secrets]` mappings + pr-loop AGENTS/skills into consumers via the managed set. Plus local-reviewer rollout (#26): per-consumer App-liveness verification + gating. Safe only after OBS04. | OBS04 |

Dependency spine: OBS01 → OBS02 → OBS03 → OBS04 → INS01; FLU01 is independent.

## Postponed

- Step 5 — pixi test/build/run + changelog/release → `docs/prd/pixi-test-build-release.md`
- Step 6 — reusable workflows + release-core cutover → `docs/prd/workflows-cutover.md`
