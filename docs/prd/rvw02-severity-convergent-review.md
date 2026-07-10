# RVW02 — Severity-Standardized, Convergent Code Review

> Authoritative spec for the review-cycle redesign: findings arrive classified on
> one severity ladder, round 1 exhausts the high tiers, later rounds review only
> the fix range, and the loop converges by construction. Decision records:
> [ADR-0043](../adr/0043-head-strict-rerun-default.md),
> [ADR-0044](../adr/0044-findings-arrive-classified.md),
> [ADR-0045](../adr/0045-dimension-fanout-single-calibrator.md).
> Vocabulary: `CONTEXT.md` (**Finding**, **Severity**, **Severity override**,
> **Dimension pass**, **Calibrator**, **Review round**, **Breaker**, **rerun**,
> **Review-round record**, **Variant**, **Roster**, **Reviewer adapter**).

## Problem Statement

The review cycle works but converges slowly and expensively. A typical PR takes
~4 review rounds (~15 minutes of machinery each, before fix time), and an epic
roughly doubles that on the umbrella branch — hours of agent time per epic.

The root defect is that a single review pass is not exhaustive at any severity
tier: a reviewer surfaces two high-severity issues plus nitpicks in round 1,
then finds NEW high-severity issues in round 2 on lines it already reviewed.
Current evidence (external and ours) attributes this to attention anchoring and
long-context degradation, worst on large PRs. Because high-value findings leak
into later rounds, every round must re-review the whole PR, nothing converges
by construction, and the only guards are a blunt round cap and an
"all-nitpicks" stop.

Three structural gaps compound it: findings arrive with no standard
classification, so a shepherd re-classifies every comment by hand each round;
the review-once `rerun` default means fix commits are often never reviewed at
all — the exact place fresh mistakes land; and review content is never
persisted locally, so no prompt or pipeline change can be measured — only
felt.

## Solution

Reviews converge from high volume / high value to low volume / low value, with
predictable cost:

- Every **Finding** arrives pre-classified on one 4-tier **Severity** ladder
  (`critical | major | minor | nit`), with category and confidence riding
  along informationally. The engine reads severity directly; the shepherd's
  classification step disappears.
- Round 1 is engineered to exhaust the high tiers: each local-agent reviewer
  fans out into parallel **Dimension passes** (correctness, cross-file
  invariants, security/robustness, test quality) whose union a single fixed
  **Calibrator** dedups, adversarially verifies, severity-normalizes, and
  orders. Expensive once, by design.
- Rounds after the first are cheap and narrow: one incremental pass over the
  fix range only (`last-reviewed-head..new-head`) with mandatory
  dependency-neighborhood context, new nits suppressed. `rerun=true` becomes
  the default, so fix commits are actually reviewed.
- The **Breaker** stops the loop when a round has no major-or-worse finding;
  minor/nit findings still get addressed (thread resolution) but never mint
  another round.
- Every review is persisted as a **Review-round record** joined to the eval
  subsystem, so review quality becomes measurable per prompt **Variant**:
  recall, false-positive rate, cost, and latency — data, not intuition.

## User Stories

1. As a maintainer, I want every review finding to carry a standard severity,
   so that high-priority items surface first without anyone re-classifying
   comments.
2. As a maintainer, I want the major/minor boundary defined by the merge-block
   test ("would a competent reviewer hold the merge?"), so that severity is a
   judgment rule, not a vibe.
3. As a shepherd agent, I want findings pre-classified and severity-ordered,
   so that I address critical → nit in one logical sweep instead of triaging
   first.
4. As a shepherd agent, I want to stop classifying findings entirely, so that
   each round costs one less agent step.
5. As a maintainer, I want a dormant severity-override verb kept working but
   absent from role prompts and operator-facing guidance, so that a wrong
   reviewer-emitted severity can be corrected without reintroducing a
   classification stage.
6. As a maintainer, I want an unparseable finding to default to `major`, so
   that severity-parsing failures force an extra round rather than slip a
   real issue past the Breaker.
7. As a maintainer, I want round 1 to exhaust critical and major findings, so
   that later rounds can safely review only the new commits.
8. As a local-agent reviewer, I want my review split into parallel
   dimension-scoped passes, so that narrowed attention raises recall instead
   of one monolithic pass self-budgeting across everything.
9. As a maintainer, I want one fixed calibrator model judging every reviewer's
   findings, so that severities are calibrated on a common ruler across
   backends.
10. As a maintainer, I want every posted finding to have survived adversarial
    verification with quoted evidence and a tier-appropriate justification (a
    concrete failure scenario for major-or-worse, a clear rationale for
    minor/nit), so that false positives don't erode trust in the review.
11. As a maintainer, I want the calibrator forbidden from originating
    findings, so that the judge stage doesn't regress into another monolithic
    reviewer.
12. As a maintainer, I want rounds after the first to review only the fix
    range, so that the review loop converges and each round gets cheaper.
13. As a maintainer, I want incremental passes required to read the
    dependency neighborhood of changed lines, so that a local fix that breaks
    a distant invariant is still caught.
14. As a maintainer, I want a rebase or force-push to void the incremental
    premise and trigger a full re-review, so that history rewrites fail
    toward over-reviewing.
15. As a maintainer, I want `rerun=true` as the default for required
    reviewers, so that the commits addressing a review are themselves
    reviewed.
16. As a maintainer, I want the review loop to stop when a round has no
    major-or-worse finding, so that minor-only rounds end the machinery while
    their threads still get resolved before Ready.
17. As a maintainer, I want new nits suppressed after round 1, so that late
    rounds can't be recolonized by style churn.
18. As a maintainer, I want round-1 nits posted under a configurable cap and
    ordered last, so that I keep optionality on the low end without flooding.
19. As a human reading a PR, I want findings rendered as Conventional
    Comments with a blocking/non-blocking decoration, so that severity is
    legible at a glance in GitHub.
20. As the PR state engine, I want each finding's severity recoverable from a
    machine marker in the comment body, so that GitHub threads remain my only
    store and no prose parsing is needed.
21. As a maintainer, I want app-reviewer findings mapped to the shared ladder
    by their reviewer adapters, so that Copilot/Gemini/CodeRabbit findings
    obey the same Breaker as local reviewers.
22. As a maintainer, I want each reviewer's review summary to attest coverage
    (what was reviewed, what was skipped and why), so that silence means
    "clean," not "skipped."
23. As a harness operator, I want every review persisted as a review-round
    record with dispositions, invocation, and variant hashes, so that review
    content is replayable and measurable offline.
24. As a harness operator, I want review-round records joined to eval records
    by run id, so that one report answers "which prompt variant produced what
    recall at what cost."
25. As a harness operator, I want to replay a review against an arbitrary
    commit range without posting to any PR, so that A/B experiments on
    historical PRs need no GitHub machinery.
26. As a maintainer, I want new-pipeline round-1 output scored against the
    findings the old system surfaced across all its rounds, so that the
    redesign ships with a before/after number, not a feeling.
27. As a maintainer, I want the dimension set, nit cap, and calibrator to be
    configuration, so that cost/quality dials turn per repo without redesign.
28. As a maintainer, I want verified-but-out-of-scope findings routed out with
    a recorded disposition instead of erased, so that a future Opportunity
    harvest can read them without the review pipeline ever coupling to it.
29. As a coordinator, I want the fan-out invisible below the reviewer boundary,
    so that the state engine, funnel, and reconcile semantics stay untouched.
30. As a portfolio maintainer, I want the dev-cycle canon (release repo,
    CLAUDE.md, role docs) updated with the rerun flip in the same change, so
    that the canonical doc never contradicts the shipped default.

## Implementation Decisions

- **Finding domain module** (new, deep, pure): owns the **Severity** ladder,
  the **Finding** value object (severity, category, confidence, location,
  evidence, fix suggestion), the disposition vocabulary
  (`post | drop-unverified | nit-suppressed | out-of-scope`, where
  `out-of-scope` covers findings beyond the PR's diff — pre-existing issues
  being the archetype), severity ordering, and both wire renderings — the
  Conventional Comments human layer (`critical` →
  `issue (critical, blocking):`, `major` → `issue (blocking):`, `minor` →
  `suggestion (non-blocking):`, `nit` → `nitpick:`) and the machine marker
  (an HTML comment carrying the exact severity/category/confidence tuple).
  Review pipeline and PR state engine both consume this one module; no I/O.
- **Severity precedence** (ADR-0044): machine marker → reviewer-adapter
  mapping → `major` default; a write-once **Severity override** beats all
  three. Category and confidence are informational-only; severity is the
  engine's sole routing key.
- **Reviewer output schema**: the 4-tier enum replaces ERROR/WARNING/INFO
  (no compat); findings gain category and confidence; the review summary
  gains a coverage attestation (files/hunks reviewed, files skipped with
  reasons). Attestation is human-facing, not engine data.
- **Dimension fan-out** (ADR-0045): the detached review run executes the
  reviewer's configured **Dimension passes** in parallel against the shared
  read-only Tree (default set: correctness, cross-file invariants,
  security/robustness, test quality — a per-reviewer **Roster** option riding
  the same seam as `model`/`instructions`), unions the results, and hands
  them to the **Calibrator**. Passes may report pre-existing issues; routing
  them out is the calibrator's job, not prompt-mandated silence.
- **Calibrator** (ADR-0045): one fixed table-level agent/model (default:
  claude backend at high ReasoningLevel) shared by all reviewers — dedups,
  adversarially verifies with tier-appropriate evidence (quoted evidence
  always; a concrete failure scenario for major-or-worse, a clear rationale
  for minor/nit; a finding is dropped only when adversarial verification
  actively refutes it — never on mere uncertainty — and never downgraded, F2
  #665), normalizes severity, orders the result, assigns dispositions.
  It never originates findings. The reviewer's own bot posts the calibrated
  result; funnel/reconcile semantics are unchanged.
- **Incremental rounds** (ADR-0045): rounds after the first review
  `last-reviewed-head..new-head` (both SHAs already known to the engine —
  rounds are keyed by head SHA) as ONE pass at a cheaper ReasoningLevel, with
  prompt-mandated dependency-neighborhood context expansion; new nits are
  suppressed by the calibrator. If the last-reviewed head is not an ancestor
  of the new head, fall back to a full-PR round. Diff-scope machinery gains
  commit-range support; comment anchoring stays against the full PR diff.
- **Breaker**: stops on round cap, or on a round with NO major-or-worse
  finding. Minor/nit findings still require thread resolution before Ready
  but never mint rounds. A fired breaker still suppresses all re-requests.
- **CLASSIFY retirement** (ADR-0044): the state is structurally unreachable
  and removed; the classify verb survives as the severity-override writer,
  deliberately absent from role prompts and operator-facing guidance
  (decision records — ADR-0044 and this PRD — still describe it).
- **rerun flip** (ADR-0043): `rerun=true` (head-strict) becomes the code
  default; review-once is an explicit per-reviewer opt-out. The dev-cycle
  canon (release repo's dev-cycle doc, global CLAUDE.md, role docs) flips in
  the same change, canonical doc first.
- **Reviewer adapters**: each app reviewer's adapter owns mapping its native
  severity format to the ladder (Gemini's Critical/High/Medium/Low, etc.);
  anything unmappable defaults to `major`.
- **Review-round record** (new store): written verb-witnessed at generate
  time — raw dimension-pass outputs, the calibrator's full judged output
  including routed-out dispositions, the invocation config, per-run
  **Variant** hashes, run ids, tokens/duration — to the harness-owned,
  repo-keyed, append-only, never-committed JSONL convention, generalizing the
  eval store's helpers. Boundary: an eval record says how a run *behaved*; a
  review-round record says what the review *concluded*.
- **Eval report join**: the report gains a review axis joining round records
  to eval records by run id, grouped by variant — recall/FP/cost/latency per
  prompt variant. The variant label mechanism is the experiment-arm handle.
- **Replay**: a range + no-post mode of the review path reviews an arbitrary
  commit range of a repo, writes the record, touches no PR. This is the
  offline A/B harness; the round-1 range for a historical PR is
  `merge-base..first-round-head`.
- **Role prompts**: shepherd loses classification and gains
  address-in-severity-order guidance; reviewer role docs describe the
  dimension/calibrator contract. The `Agent: <name> [SEVERITY]` comment
  prefix is retired in favor of the two-layer rendering.
- **Config surface**: table-level — calibrator (backend/model/reasoning),
  nit cap (0 = floor at minor), existing `round_cap`. Per-reviewer —
  `dimensions`, `rerun`, existing `window` (bumped for local reviewers in the
  managed config to cover the parallel fan-out on large PRs).
- **Dev-cycle events**: new registered names for the new milestones
  (calibration completed, finding dispositioned, incremental-fallback fired),
  emitted verb-witnessed.

## Testing Decisions

- Good tests assert externally visible behavior — parse/render round-trips,
  state transitions, record contents, CLI output — never incidental
  internals. Prompt *content* quality is deliberately not unit-tested: that
  is what the offline A/B harness measures. The calibrator's judgment is not
  tested for wisdom, only its I/O contract (schema validation, disposition
  routing, never-originates enforcement where checkable).
- **Finding domain module**: table-driven — ladder ordering, marker
  render/parse round-trips, Conventional Comments rendering per tier,
  precedence chain (marker → adapter mapping → default → override), malformed
  marker handling.
- **PR state engine**: breaker semantics (no-major+ stop, round cap,
  re-request suppression), severity-override precedence, CLASSIFY removal.
  Prior art: the existing breakers/state test suites.
- **Reviewer adapters**: per-adapter native-format → ladder mapping tables,
  unmappable → `major`.
- **Diff range**: range resolution, ancestor check, rebase/force-push
  fallback to full review.
- **Review-round record store**: record build (pure), append/read, repo
  keying. Prior art: the eval store tests.
- **Orchestration**: fan-out/union/calibrate/post flow with fake backend and
  gh adapters — pass failures, empty unions, disposition routing to
  post/persist. Prior art: the review service and gh/git adapter fakes.
- **Replay + CLI verbs**: range + no-post behavior, user-facing errors
  (missing config, bad range), report join output shape.
- **Role prompt generation**: shepherd prompt carries no classification
  instruction; reviewer prompts carry the dimension slices. Prior art: the
  generated role-prompt tests.

## Out of Scope

- Wiring the Opportunity harvest: dispositions are recorded and persisted as
  the seam, but no capture call, no `shipit opportunities` dependency, no
  reader (a future feature consumes the records).
- Severity-scoped finder agents (a "highs-only" pass) — rejected on evidence
  (ADR-0045); do not reintroduce.
- A cross-backend ensemble/virtual reviewer — rejected until measurements
  justify the identity-model rework (ADR-0045).
- Static-analysis-assisted dependency-context expansion for incremental
  passes — prompt-directed expansion is v1; upgrade only if eval shows
  misses.
- Diff-scoping app reviewers (Copilot et al. keep reviewing however they
  review); changing the required-reviewer set or the sole-requester rule.
- Pre-committed acceptance thresholds for the A/B study — the first run is
  comparative; bars are set from data.
- Auto-merge, Ready-flip, or any change to the human merge gate.
- Category-based routing or policy (categories stay informational).
- Subjective agent-as-judge scoring inside eval records (still deferred to
  HAR04).

## Further Notes

- **Sequencing**: the schema/domain work lands first (everything speaks it),
  the persistence + replay harness second, so the fan-out and incremental
  rounds ship WITH baseline comparisons rather than after-the-fact ones.
  Baselines: two or three closed, complex PRs from phos-core / phos-app /
  lex-fmt with full review history; ground truth is the union of findings the
  old system surfaced across all rounds (recoverable from GitHub threads plus
  the verdict log), matched to new output by an agent-assisted pairing pass
  with human spot-checks. Known limit: this measures "does round 1 now catch
  what previously leaked into rounds 2–4," not issues neither system found.
- **Cost shape**: round 1 goes from 1 to ~5 model runs per local reviewer
  (dimensions + calibrator); rounds ≥2 drop from full-PR reviews to one cheap
  fix-range pass. Expected net: fewer rounds, earlier convergence, roughly
  cost-neutral per PR with much better placement of spend. The eval join
  exists to verify exactly this.
- The disposition vocabulary is the future Opportunity-harvest seam
  (`out-of-scope` findings are evidence-complete capture candidates); one
  reader feature away, zero coupling today.
- This is the first deliberate consumer of the eval subsystem's variant-label
  A/B mechanism.
- The research corpus behind ADR-0044/0045 (single-pass recall, anchoring,
  dimension-scoping evidence, incremental-review pitfalls, vendor practice)
  was gathered 2026-07; revisit before extending the architecture, not to
  relitigate it.
