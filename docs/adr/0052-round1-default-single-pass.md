# Round-1 review default: single pass; concern fan-out becomes opt-in

ADR-0045 shipped the concern-scoped dimension fan-out as the round-1
default on outside evidence (single-pass recall <50% at every tier,
multi-pass gains plateauing ~n=5), with the Review Lab (ADR-0048/0049)
built afterwards to replace exactly this kind of judgment call with
measurement. The Lab has now measured it, against the v37 Ground-truth
fixture (100 human-confirmed labels over three pinned portfolio PRs,
2 sweeps x 2 replicates per arm, equal-budget comparison):

- The 4-pass concern fan-out and a single monolithic pass found the SAME
  confirmed majors (5 each after adjudication); the fan-out paid ~4x the
  tokens (3.24 vs 0.83 Mtok) — 8.6 vs 26.7 recall-%/Mtok at equal budget.
- The fan-out's surplus emissions were adjudicated ~90% redundant
  (restatements of already-found findings), not additional recall.
- The fan-out missed a real CI-breaking major (an un-cfg-gated const,
  dead code on wasm32) in all four attempts, while the single pass and
  the severity-tier arm both found it: concern-scoped prompts each
  disclaim everything outside their bucket, and build/CI-class defects
  belong to no bucket. Scoping has a measured blind-spot cost, not just
  a token cost.
- The severity-tier fan-out (ADR-0051 arm) sat between the two on cost
  (2.50 Mtok) at the same recall.

Decision: the round-1 default pipeline shape for local-agent reviewers
(codex, agy) is a SINGLE monolithic pass. The concern fan-out remains
wired and selectable per reviewer via explicit Roster/config (and in Lab
cells via `shape = "fanout"`); the dimension registry, dedup, and dormant
calibrator are untouched. Nothing changes for rounds 2+ (head-strict
fix-range re-reviews) or for app reviewers (copilot).

The evidence is direction-consistent but single-experiment per config
(UNDERPOWERED flags stand); this default is cheap to reverse, and the
deciding follow-up is the `singlepass-xK-union` cell — K unioned cheap
passes at the fan-out's budget. If union-of-K beats one pass at equal
spend, that becomes the next default proposal; if the flip ever measures
a recall regression on a future fixture, revert is one config line.

Consequences: ~4x round-1 token cost reduction portfolio-wide at
measured-equal major recall; one fewer scoping blind spot; the fan-out
prompt machinery stays exercised by the Lab's committed cells, so the
opt-in path cannot rot silently.
