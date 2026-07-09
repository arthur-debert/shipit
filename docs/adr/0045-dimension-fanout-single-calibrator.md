# Dimension-scoped fan-out with a single calibrator; severity-scoped finders rejected

A local-agent reviewer's first review of a PR is no longer one monolithic
"find everything" pass: the detached review run fans out into parallel
**dimension passes** (correctness, cross-file invariants, security/robustness,
test quality — a per-reviewer Roster option) whose union feeds a single
**Calibrator** that dedups, adversarially verifies (quoted evidence + a
concrete failure scenario, or the finding is dropped), normalizes Severity on
one ruler, and emits the severity-ordered result the reviewer's bot posts. The
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
drop-unverified, nit-suppressed, out-of-scope/pre-existing); routed-out
findings are persisted in the review-round record, not erased — the reserved
seam for future Opportunity harvest.

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
