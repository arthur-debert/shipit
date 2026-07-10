# OBS04 — Readiness engine consumes the funnel

> Epic: **OBS04** · Status: planned · Plan: `docs/legacy-prd/FUTURE_WORK.md`
> ADR: `docs/adr/0006-readiness-with-degraded-reviewers.md`
> Glossary: `CONTEXT.md`

## Problem Statement

The PR state engine decides **Reviewed** / **Ready** from native GitHub signals
only — a reviewer's `review_requested` edge and its review object. That is blind
to three things the observability spine now makes knowable, and the gap silently
parks PRs:

- **It can't see the OBS02 funnel.** The engine reads native edges, not the
  uniform funnel breadcrumbs OBS02 records on the PR. A local-agent reviewer has
  no native `review_requested` edge at all, so the engine cannot tell a review
  that **failed** or **timed-out** apart from one that was **never-requested** —
  every non-success looks identical, and the PR holds forever with no signal as
  to why.
- **It has no wait window.** The engine is a stateless pure function with no
  clock (release-core's looping `pr wait` was deliberately dropped), so there is
  nothing in the system to age a *requested-but-silent* reviewer out. A reviewer
  that never answers holds the PR indefinitely.
- **The dispatcher routes on PROSE.** `shipit pr next` picks its one act by
  substring-matching the engine's *human-facing* `next_action` text
  (`dispatch.py` `_only_waiting` searches for `"wait (already requested"` and the
  request/re-request verbs). Routing program control on a sentence meant for a
  human is brittle — a wording change silently re-routes the dispatcher. This is
  the deferred PRF01 review finding **#24.1**.

## Solution

Per **ADR-0006** (the decision this PRD implements — read it; this PRD does not
duplicate it), make the engine consume the funnel without giving it a clock.

- **Normalize to one funnel view.** The engine folds the native reviewer signals
  (Copilot's `review_requested` edge + its review object) AND the OBS02 check-run
  state into a **single funnel view per reviewer** — *requested* → *in-flight* →
  *posted*, or a terminal *failed* / *empty* / *timed-out* — read uniformly
  across App and local-agent reviewers.
- **Outcome-recorded, not review-succeeded.** A required reviewer is **settled**
  when its funnel reaches a **recorded terminal outcome** (*posted* / *empty* /
  *failed* / *timed-out*). **Reviewed** = every required reviewer **settled** +
  every thread from a *posted* review resolved. It is NOT "every required
  reviewer succeeded."
- **Failed / empty / timed-out → settled, non-blocking, degraded.** Those
  outcomes settle and do not **hold** Ready, but the PR is surfaced as
  **degraded** ("Ready (degraded: codex-local failed)") so the state is never
  *silently* "fine." Only **never-requested** and **in-flight-within-window**
  actually hold the PR.
- **Wait window.** Uniform across reviewer kinds, **20m default + per-reviewer
  override**, aged from each reviewer's own request timestamp — the check run's
  `started_at` for a local reviewer, the `review_requested` edge time for an App
  reviewer. In-flight past the window → *timed-out* → settled.
- **Engine stays stateless.** "Now" enters as an **input** to the snapshot; the
  engine keeps no clock and release's looping `pr wait` is NOT revived.
- **Provisioning failures are treated identically to runtime flakes** —
  non-blocking + loud. A consumer mid-rollout whose review App still lacks
  `checks:write` sees that reviewer perpetually **degraded**, never blocked;
  making "not provisioned" block would recreate the one-broken-thing-parks-every-PR
  disaster on every half-rolled-out repo.
- **The dispatcher routes on structured state, not prose.** `pr next` decides its
  one act from the structured funnel / `TaskStatus` data, not from `next_action`
  text — which **absorbs issue #24.1 here**, not in FLU01. This requires
  extending the copied engine's `TaskStatus` contract; per ADR-0001 the engine is
  a verbatim copy of release-core, so this is a recorded **divergence** from the
  upstream, made deliberately in shipit.

`pr status` / `pr next` surface the new state: `pr status` adds the **degraded**
annotation to its output; `pr next` waits only when a reviewer is
in-flight-within-window, and otherwise proceeds (a degraded-but-otherwise-ready
PR flips).

## User Stories

1. As an agent driving a PR, when a required reviewer has never been requested, I
   want it to **hold** the PR at reviews-pending, so that the review loop starts
   instead of the PR slipping to Ready unreviewed.
2. As an agent, when a required reviewer is **in-flight within its wait window**, I
   want it to hold the PR, so that I wait for a review that is still legitimately
   coming.
3. As an agent, when a required reviewer **posted** its review, I want it counted
   as settled (and its threads holding until resolved), so that Reviewed means the
   review actually arrived and was addressed.
4. As an agent, when a required reviewer **failed / came back empty / timed out**,
   I want it **settled and non-blocking**, so that one broken reviewer never parks
   the PR forever.
5. As an agent, when a reviewer settled with a non-success outcome, I want
   `pr status` to name it as **degraded** ("Ready (degraded: codex-local
   failed)"), so that the failure is visible and never silently mistaken for
   "fine."
6. As an agent, when a requested reviewer stays silent **past its wait window**, I
   want it to **time out → settle**, so that a perpetually-silent reviewer stops
   holding the PR.
7. As a maintainer, I want the wait window to be **20m by default with a
   per-reviewer override**, so that a slow backend gets more room without loosening
   it for everyone.
8. As an operator of a repo mid-rollout, when a review App is not yet provisioned
   (`checks:write` missing), I want that reviewer surfaced as **degraded**, never
   **blocked**, so that an in-progress rollout doesn't park every PR.
9. As an agent, I want `pr next` to **wait only when a reviewer is
   in-flight-within-window** and otherwise proceed, so that a degraded-but-ready
   PR flips instead of stalling.
10. As a shipit maintainer, I want `pr next` to route on the **structured funnel /
    `TaskStatus` data** rather than the `next_action` prose, so that a wording
    change can never silently re-route the dispatcher (this is issue #24.1,
    delivered here).
11. As a shipit maintainer, I want "now" to enter the engine as an **input**, so
    that the engine stays a pure, deterministic, clock-free function and the wait
    window is testable without a wall clock.

## Implementation Decisions

### Snapshot carries the funnel + now

- The snapshot (the readiness view over the `PR` core in `prstate.model`) carries
  the OBS02 funnel breadcrumbs
  / check-run state per reviewer **and** an injected **"now"**. The engine reads
  both; it never calls a clock itself. A fixed "now" + a recorded snapshot →
  a deterministic state.

### Funnel normalization

- Map the per-reviewer `ReviewLifecycle` (from `reviewers.py` `detect`) **plus**
  the OBS02 check-run state into one **funnel state** per reviewer — *requested* /
  *in-flight* / *posted* / *failed* / *empty* / *timed-out*. App reviewers source
  it from native signals; local-agent reviewers from the shipit-authored signal.
  The mapping lives behind the adapter interface so the engine never branches on a
  reviewer's name.

### Readiness / Reviewed redefinition + degraded

- A required reviewer is **settled** at any recorded terminal funnel outcome
  (not only *posted*). **Reviewed** = all required reviewers settled + every
  *posted*-review thread resolved. *failed / empty / timed-out* settle
  non-blocking and are collected into a **degraded** set surfaced on the status.

### Wait-window timeout

- For each reviewer, age "now" against its request timestamp (check-run
  `started_at` for local, `review_requested` time for App). In-flight past the
  window (per-reviewer override, else 20m) maps to *timed-out* → settled.

### Dispatcher on structured state (#24.1)

- Extend the engine's `TaskStatus` contract with **structured per-reviewer funnel
  data** (and the degraded set). The `pr next` dispatcher decides its one act from
  that structured state — replacing the `_only_waiting` prose substring match. The
  "wait vs (re-)request vs flip" decision reads the funnel states directly. The
  `TaskStatus` extension is the recorded ADR-0001 verbatim-copy divergence.
- Keep the engine pure/unit-testable: the decision still takes no network; the
  `Acts` boundary (execution) is unchanged.

### `pr status` / `pr next` surface

- `pr status` adds a **degraded** annotation (which required reviewers settled
  non-success, and why) to both text and JSON output; a clean-but-degraded PR
  reports **"Ready (degraded: …)."**
- `pr next` waits only when a reviewer is in-flight-within-window; otherwise it
  proceeds (flip a degraded-but-ready PR; request a never-requested reviewer).

## Testing Decisions

A good test here asserts **external behavior** from a recorded snapshot + a fixed
"now" → the engine's state / degraded set / next act — never an implementation
detail. The engine already tests this way (pure functions over captured JSON),
which is the bar.

- **Table-driven funnel × window matrix.** For each funnel state
  (*requested* / *in-flight* / *posted* / *failed* / *empty* / *timed-out*) ×
  within-window / past-window, assert the expected **holds / settled** verdict and
  whether the reviewer appears in the **degraded** set. "Now" is injected, so the
  past-window cases are deterministic.
- **Reviewed / Ready redefinition tests.** A failed/empty/timed-out required
  reviewer yields Reviewed (non-blocking) with the reviewer degraded; a
  never-requested or in-flight-within-window reviewer holds.
- **Dispatcher on structured state.** Each funnel-state combination routes to the
  expected act (wait / request / re-request / flip) via a **fake `Acts`
  boundary**, asserting which method fired — no `gh`, no prose matching.
- **Provisioning-as-flake.** A reviewer whose App lacks `checks:write` reads
  degraded, never blocked.

## Out of Scope

- **Posting the funnel breadcrumbs** — that is OBS02 (this epic *reads* them).
- **Async local execution** — that is OBS03.
- **The App permission re-grant / per-consumer rollout** — that is INS01 (and the
  `checks:write` provisioning itself).
- **Reviving a looping/blocking `pr wait`** — the stateless-engine-plus-injected-now
  achieves the wait without it.

## Depends on

- **OBS02** — the funnel breadcrumbs the engine reads.
- **OBS03** — async local execution that produces the recorded outcomes.
