# OBS03 — Async local-review execution

> Epic: **OBS03** · Status: planned · Plan: `docs/legacy-prd/FUTURE_WORK.md`
> ADR: `docs/adr/0005-local-review-funnel-via-check-runs.md`
> Glossary: `CONTEXT.md` (PR-flow terms)

## Problem Statement

A local-agent review runs **synchronously on the main thread**. When an agent (or
human) asks for one, `_LocalReviewAdapter.request()`
(`src/shipit/prstate/reviewers.py`) calls `service.run_and_post()`
(`src/shipit/review/service.py`), which calls `backend.run()`
(`src/shipit/review/backends/base.py` → `agy.py`), which calls `proc.run()` — and
that call **blocks until the agent CLI returns or hits its timeout**. For agy that
timeout is `--print-timeout=600s` (ten minutes of headroom for a big diff). So
`shipit pr review request --reviewer codex-local` blocks the whole invocation, and
between the request and the result **there is zero visibility** — no signal that a
review is in flight, no way to poll, nothing to read.

Worse, this makes the OBS02 funnel breadcrumb a lie. OBS02 gives a local reviewer
a `requested / in-flight` marker (an App-authored **check run**, ADR-0005) so the
PR shows a review is on its way before it lands. But if posting is synchronous, the
`in_progress` marker and the terminal `success`/`failure` marker are written **in
the same blocking call, microseconds apart** — the in-flight state is never
observable, because nothing else runs while the agent runs. **Async is what makes
the funnel marker real.** Until execution detaches, the OBS02 breadcrumb has no gap
to fill.

## Solution

Make a local-review request **fire-and-forget**. The request synchronously creates
the OBS02 `in_progress` check run, then **detaches** the agent run and returns
immediately. The detached process runs the agent over the PR diff, posts the
structured review, and flips the check run to its terminal state
(`success` / `failure` / `empty` / `timed_out`) on completion. The caller is never
held for the length of a model run.

**The PR + check run ARE the result store** (ADR-0005). There is **no daemon, no
local job store, no shipit-side process holding state**. State is reconstructed by
reading the PR — the same way every other part of the engine works ("piggyback
GitHub, no daemon, no local state"). The detached run is the only writer; the PR is
the only store; a reader (a polling agent, or OBS04's engine) learns the outcome by
reading the check run and the posted review, never by talking to a shipit process.

This inverts the `_LocalReviewAdapter.request()` contract: today it returns `True`
only after the review is posted; after OBS03 it returns as soon as the run is
detached and the `in_progress` marker is up, and the **outcome is read later from
the PR**, not from the call's return.

## User Stories

1. As an agent, when I run `pr review request --reviewer codex-local`, I want the
   call to return immediately with the review **in-flight**, so that I am not
   blocked for the length of a model run.
2. As an agent, after requesting a local review, I want to **poll the PR** for the
   outcome (the funnel check run + the posted review), so that I learn the result
   from the PR's own state, not from a long-held call.
3. As an agent, when a detached run **crashes or times out**, I want it to still
   resolve to a **visible failed / timed-out** check run, so that a dead run is
   never indistinguishable from "still working" or "never requested".
4. As an agent, when I re-request a local review that is already in flight, I want
   the system to **reconcile against the existing check run** rather than spawn a
   duplicate run that double-posts, so that a bare re-run is safe.
5. As an operator diagnosing a failed local review, I want the detached run's output
   captured to the OBS01 **file sink**, so that I can read why it failed even though
   no terminal was attached to it.
6. As a consumer-repo owner who never uses local reviewers, I want the App-reviewer
   (Copilot) path **unchanged**, so that async execution adds nothing to a repo that
   doesn't run local agents.

## Implementation Decisions

### Detach mechanism — no daemon

The request path does the synchronous, fast work — create the `in_progress` check
run (OBS02) — then spawns a **detached child process** that survives the parent
exiting, and returns. The mechanism is an **implementation detail left to the work
stream**, bounded by a hard constraint:

> **detached child process · survives the parent exiting · no daemon · no local
> state · posts back to the PR · idempotent.**

Candidate shapes (sketched, not prescribed — the WS picks one):

- A backgrounded **`shipit` subinvocation** — the parent execs a child
  `shipit`-internal entrypoint that carries the `(repo, pr, reviewer, model,
  instructions)` it needs as arguments, double-forks / detaches from the parent's
  process group, and runs `run_and_post` + the terminal check-run transition. The
  child reconstructs everything it needs from its arguments + the PR; it holds no
  shared state with the parent.
- A platform detach primitive (`subprocess` with start-new-session / `nohup`-style
  detachment). Same contract.

What is **explicitly rejected** (per ADR-0005 and FUTURE_WORK): a long-lived daemon,
a local job queue / job-store file, or any shipit-side process that must be kept in
sync with the PR. The invariant is "**no daemon / no local state / posts back to the
PR**" — if a candidate needs a place to remember in-flight jobs other than the check
run, it is the wrong candidate.

### Idempotency — reconcile against the check run, never double-post

A second `request` for a reviewer whose check run is already `in_progress` must not
spawn a second run that posts a second review. Because the **check run is the
store**, idempotency is a **read-then-decide** against it: if a non-terminal funnel
check run for this reviewer + head already exists, the request reconciles to it
(reports in-flight) instead of detaching a duplicate. This keeps "re-request is the
same call" (the existing adapter contract) honest under async — a re-poke of an
in-flight reviewer is a no-op against the live run, not a duplicate.

### Logging — capture the detached run via OBS01

The detached child has no attached terminal, so its diagnostics must go somewhere
durable: the child wires its output (the agent invocation, the parse result, the
post + check-run transition) to the **OBS01 file sink**. This is the only way story
5 works — a crashed detached run leaves both a terminal **failed** check run on the
PR (the *what*) and a **log entry** in the file sink (the *why*). OBS03 depends on
OBS01 being in place for exactly this reason.

### Terminal posting from the detached process

The detached process owns the terminal transition: run the agent, parse the review,
**post the structured review** (`post.post_review`, AS the App bot — unchanged from
`run_and_post`), and flip the check run to `success` (review posted) / `empty` (no
findings) / `failure` (backend or post error) / `timed_out` (the agy timeout
marker). The review-generation and posting code is reused as-is; OBS03 changes
*who runs it* (a detached child) and *what it does on completion* (the check-run
transition), not *how a review is generated*.

## Failure & Timeout

A detached run must **never leave a dangling `in_progress` with no resolution path**.
Two backstops:

- **Self-resolution (primary):** the detached run wraps its work so that any
  outcome — success, empty, backend error, parse failure, the agy timeout marker —
  flips the check run to the matching terminal state before the child exits. The
  existing `BackendError` / timeout-marker handling
  (`src/shipit/review/backends/base.py`) feeds the `timed_out` vs `failure`
  distinction.
- **Wait-window backstop (OBS04):** if the child **vanishes** — killed, OOM,
  machine reboot — without writing any terminal state, the check run sits
  `in_progress` with a `started_at`. OBS04's **wait window** ages that timestamp and,
  once it lapses, treats the reviewer as **timed-out / degraded** — settled,
  non-blocking, visible. So even a process that disappears resolves to a visible
  outcome; the engine never waits forever on a ghost. (OBS04 *consuming* the window
  is out of scope here; OBS03 only relies on it as the backstop and guarantees the
  self-resolution path for every outcome it can observe.)

## Work Streams (hint)

Execution topology (Work Streams + dependency waves) lives on the OBS03 epic issue,
not here. As a sketch:

- **(WS) Detach + return-immediately path** — the request creates the OBS02
  `in_progress` check run, spawns the detached child (no daemon), and returns;
  `_LocalReviewAdapter.request()` no longer blocks on the model run.
- **(WS) Terminal posting from the detached process** — the child runs the agent,
  posts the review, and transitions the check run to its terminal state; output
  routed to the OBS01 sink.
- **(WS) Crash / timeout resolution** — self-resolution for every observable outcome,
  with idempotent reconcile against an existing in-flight check run; leans on OBS04's
  wait window only for a vanished process.

## Out of Scope

- **The engine reading the funnel / the wait window / the Ready pillars** — consuming
  the breadcrumbs + timestamps, applying the per-backend wait window, and the
  "requested + outcome-recorded + threads-resolved" readiness pillar are **OBS04**. OBS03 only
  *produces* the async outcome; it relies on OBS04's window solely as the
  vanished-process backstop.
- **The check-run primitive itself** — creating the funnel check run, its
  isomorphic-across-reviewers shape, and the `checks:write` re-grant are **OBS02**
  (and ADR-0005). OBS03 *uses* the primitive; it does not define it.
- **The App-reviewer (Copilot) path** — Copilot has a native `review_requested`
  edge and posts on its own; nothing about its flow becomes async here.
- **Install rollout / readiness holds on by default** — INS01.

## Depends on

- **OBS02** — the funnel check run is what makes the in-flight state observable;
  without it there is no `in_progress` marker for async to fill, and OBS03's whole
  point (a real, visible gap between request and result) collapses.
- **OBS01** — the file sink is where the detached run's diagnostics land
  (Implementation Decisions → Logging).
