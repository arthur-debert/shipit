# Review Lab — Measured Review Experimentation

> Authoritative spec for RVW03 Phase 2: the methodology layer that turns review
> pipeline changes from one-shot hill climbing into small, fair, measured
> experiments. Decision records:
> [ADR-0048](../adr/0048-ground-truth-fixture-deterministic-scorer.md),
> [ADR-0049](../adr/0049-convergence-curve-objective-one-axis-cells.md),
> [ADR-0050](../adr/0050-review-scope-is-the-diff-context-is-the-checkout.md);
> foundation: [ADR-0045](../adr/0045-dimension-fanout-single-calibrator.md) as
> amended. Vocabulary: `CONTEXT.md` (**Ground-truth fixture**, **Ground-truth
> label**, **Adjudication**, **Cell**, **Sweep**, **Variant**, **Invocation**,
> **Review-round record**, **Dimension pass**, **Calibrator**). Execution
> tracker: epic #687 (RVW03), which also tracks the Phase-1 harness
> prerequisites (#688–#691).

## Problem Statement

RVW02 shipped the dimension fan-out on deliberately thin evidence, and the
post-epic audit showed why the evidence was thin: the two arms were never
comparable (different scope instructions, post-processing applied to one arm
only, contradictory context rules), the measuring was human eval-report
reading against 3 ground-truth majors (one finding = a 33-point recall swing),
`reasoning=high` never actually reached a backend, token cost was never
captured, and every experiment ran through an uncommitted monkey-patch driver.
Meanwhile the real open questions — 2 of 3 ground-truth majors missed by every
arm, semantic near-duplicates, whether the calibrator can ever pay its way —
cannot be answered by more one-shot 15-minute, ~2M-token runs. There are too
many degrees of freedom (prompts, dimension sets, models, reasoning, context
strategy, sweeps) to hill-climb blind, and the coordinator-context cost of
babysitting big runs is itself a failure mode.

## Solution

The Review Lab stands on the Phase-1 harness (sanctioned
`shipit pr review replay --fanout`,
per-run artifact bundles, robustness fixes, real token/reasoning measurement
— #688–#691) and adds four pieces:

- **A parity baseline.** Every arm and pass carries one canonical rule —
  *report only on the diff; read anything; run nothing* (ADR-0050). Scope
  stops being a confound; post-processing stages (dedup, nit-cap, calibrator)
  are toggleable axes measured separately, never silently bundled into one arm;
  Invocations are pinned per cell and stamped from actual argv.
- **A versioned in-repo Ground-truth fixture** (ADR-0048): 8–12 pinned
  portfolio PR ranges, ≥25 major-or-worse labels at v1, every label with
  provenance. The fixture grows by banked Adjudication as cells run; scored
  results name the fixture version. Building v1 is deliberate scouting work:
  parallel scout agents mine fix-commit history per repo, a normalizer agent
  compresses candidates to label format, the human confirms — the coordinator
  never ingests the raw bulk.
- **A deterministic scorer** (ADR-0048): file + line-range + claim-token
  overlap, aliases banked from adjudicated near-misses. No LLM in the
  instrument. Scoring banked records is free, repeatable, CI-runnable. The
  matching primitive is shared with semantic dedup of same-round findings
  (#673). One defect with several valid anchors (a cross-file emission
  family, which aliases cannot bridge) is declared explicitly in the fixture
  as labels sharing a `defect` equivalence-family id and counts once for
  recall (#751) — identity is banked data, never inferred cross-file
  similarity.
- **Cells and convergence curves** (ADR-0049): declarative one-axis experiment
  files with mandatory baseline/axis fields, idempotent banked results,
  replicates for variance, and K-sweep convergence curves reporting cumulative
  recall, precision, cost, and latency per sweep point. Comparisons happen at
  equal budget; round-1 exhaustiveness is the product north star but a curve
  point, never a cell gate.

The experiment program then runs as small foreground, subscription-billed
sessions — one cell each. Candidate axes, roughly in order: dimension-set
composition and detection depth (#666), sweeps blind vs informed, semantic
dedup (#673), reasoning effort (post-#685), per-dimension and cheaper models,
context strategy (checkout-walk vs commit-priming vs ascetic), and the
calibrator re-enable cell with its entry bar (#665: refuting one ground-truth
positive fails the cell). Fix-range/breaker dynamics are observational-only,
answered from live review-round telemetry.

`shipit pr review` remains the one-shot product surface throughout; the lab is
how its configuration earns changes.

## Out of Scope

- Multi-round fix-range replay (simulated shepherd fixes) — revisit only if
  sweep cells plateau.
- Synthetic bug injection in the fixture core.
- Product Roster knobs (per-dimension models, sweep counts) ahead of cell
  evidence; capabilities live in the lab runner until promoted.
- Cross-backend ensemble reviewers (rejected in ADR-0045).

## User Stories

1. As a maintainer, I want every review-pipeline claim backed by a scored cell
   against a versioned fixture, so that decisions stop resting on 3-sample
   coin flips.
2. As a maintainer, I want cells declared in reviewed files with an explicit
   baseline and single axis, so that an unfair comparison is caught at PR
   review, not after the tokens are spent.
3. As a maintainer, I want banked results reused by key and curves extended
   incrementally, so that experimentation fits subscription budgets and no
   result is ever paid for twice.
4. As a maintainer, I want recall, precision, cost, and latency reported per
   sweep point at equal budget, so that a design converging in two cheap
   sweeps beats one expensive sweep on the merits.
5. As a maintainer, I want the scorer deterministic and LLM-free, so that the
   ruler cannot repeat the calibrator's misjudgment one level up.
6. As a maintainer, I want unmatched emissions and near-misses adjudicated
   once and banked, so that the fixture grows as a side effect of running
   experiments and false positives become measurable.
7. As a maintainer, I want every arm under one scope-and-context rule, so that
   arms answer the same question and their denominators compare.
8. As a coordinating agent, I want fixture scouting fanned out to scout and
   normalizer agents, so that planning context stays in the smart-cache range.
9. As a maintainer, I want the calibrator readmitted only by a cell that
   reduces false positives without refuting a single ground-truth positive, so
   that the precision layer never again destroys recall silently.
10. As a maintainer, I want underpowered tiers marked in every report, so that
    a 0/3-style number can never masquerade as signal again.

## Dependencies & Sequencing

Phase-1 workstreams #688 (replay --fanout), #689 (artifact bundles), #690
(robustness), #691 (tokens/reasoning) land before any cell runs. Within Phase
2: fixture v1 and the scorer land together (labels are only as useful as the
thing that reads them); the cell runner follows; the experiment program is
ongoing operation, not a deliverable that "finishes".
