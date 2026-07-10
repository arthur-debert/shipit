# Dimension-scoped fan-out with a single calibrator; severity-scoped finders rejected

> **Amended by RVW02-WS08 (decouple, #669).** The calibrator is now an
> OPTIONAL, DORMANT stage — OFF by default. The default round-1 posts the
> **mechanically-deduped union** of the dimension passes, using each pass's own
> severity: findings sharing a `(file, line, claim)` merge into one canonical
> (the most-severe member) that posts, duplicates ride the record — a
> deterministic dedup, no LLM judge. Rationale: the WS05/F2 A/B baseline (#638,
> #665) measured the LLM calibrator NET-NEGATIVE on round-1 major recall — it
> refuted a true major the passes had found, dragging fan-out recall (0/3)
> *below* the single-pass baseline (1/3). The fan-out is the bet with outside
> evidence; the calibrator was an unproven precision layer bolted on, and on the
> baseline it destroyed signal. So it is KEPT — concept, config, hooks, and the
> F2 reproduction-based floor all wired but dormant (the ADR-0044 `classify`
> pattern: kept warm, not run) — and a reviewer opts it back on by configuring a
> `calibrator` in its Roster entry. Tuning/re-verifying it (the app-G1
> wrong-refutation, #665) is a later, independent effort, NOT a blocker for
> shipping the fan-out gain. The rest of this ADR (the fan-out, the passes, the
> single-ruler and never-originates constraints) stands; wherever it says the
> union "feeds"/"is judged by" the calibrator, read that as the on path.
>
> **Amended by RVW02-WS08 (F2, #665).** The calibrator's verification floor
> (when it IS on) is REPRODUCTION-based: it drops a finding ONLY when adversarial
> verification actively REFUTES it (misquoted or fabricated evidence, code that
> does not behave as the finding claims, a failure that cannot occur) — never on
> mere uncertainty or imperfect phrasing — and a finding that reproduces is
> kept, never downgraded. The "or the finding is dropped" wording in the
> original decision below predates this and is superseded: quoted evidence and
> the tier-appropriate rationale (concrete failure scenario for major-or-worse,
> a rationale for minor/nit) are still required, but a candidate that clears
> those and reproduces is not dropped for want of a perfect justification. The
> A/B evidence (WS05, #638) found the pre-amendment floor over-pruned
> reproducible true positives.

A local-agent reviewer's first review of a PR is no longer one monolithic
"find everything" pass: the detached review run fans out into parallel
**dimension passes** (correctness, cross-file invariants, security/robustness,
test quality — a per-reviewer Roster option) whose union feeds a single
**Calibrator** that dedups, adversarially verifies with tier-appropriate
evidence (quoted evidence always; a concrete failure scenario for
major-or-worse findings, a clear rationale for minor/nit — or the finding is
dropped), normalizes Severity on one ruler, and emits the severity-ordered
result the reviewer's bot posts. The
evidence (2025-26: single-pass recall <50% at every tier, anchoring/run
variance, multi-pass recall gains plateauing ~n=5) backs dimension-scoping and
pass aggregation; it does NOT back severity-scoped finders (a "highs-only
agent") — severity is assigned at calibration, dimensions scope the search. Do
not "fix" this by adding a severity-scoped pass.

Two deliberate constraints: the Calibrator is one fixed table-level
agent/model shared by every reviewer (the common severity ruler is the point —
per-reviewer calibrators would fork it), and it NEVER originates findings (a
judge that also finds is a monolithic reviewer again, with the anchoring bias
the fan-out exists to remove). Every judged finding gets a disposition (post,
drop-unverified, nit-suppressed, out-of-scope — pre-existing issues being the
archetypal out-of-scope routing); routed-out findings are persisted in the
review-round record, not erased — the reserved seam for future Opportunity
harvest.

Rounds after the first are cheap by design: one incremental pass over
`last-reviewed-head..new-head` with prompt-mandated dependency-neighborhood
context (read callers/definitions beyond the diff — raw-hunk incremental
review is the documented cross-file-regression failure mode), new nits
suppressed, falling back to a full-PR round when the last-reviewed head is no
longer an ancestor (rebase/force-push voids the incremental premise; fail
toward over-reviewing). The fan-out is invisible below the reviewer boundary —
prstate sees one review per reviewer per head, and the Roster/funnel/reconcile
machinery is untouched; a cross-backend "ensemble reviewer" that would have
halved round-1 cost was rejected because it breaks reviewer identity (funnel
check runs, rerun semantics, the sole-requester rule) before a single
measurement exists.
