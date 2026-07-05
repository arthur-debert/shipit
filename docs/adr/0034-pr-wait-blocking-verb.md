# The state machine grows one blocking verb — `pr wait`

> **Status: Accepted.** Epic RVW02 (#453); decided in the ADP00 retrospective.
> Amends the single-shot-only stance recorded in `pr next`'s module docstring
> ("there is NO polling loop here"); `pr status` and `pr next` themselves stay
> pure single-shot reads.

shipit gains exactly one verb that blocks: `shipit pr wait [PR]
--until reviews-in|ready --timeout <duration>`. It polls the same evaluator
`pr status` reads, at a FIXED tool-owned interval (default 60s, overridable in
config, never per-call), and exits the moment the awaited state arrives:
`reviews-in` fires when the latest round's reviews have all landed (the moment
an addressing agent becomes dispatchable), `ready` when the engine reports
READY. On each tick where observed state changed it emits one line and a
flow-log event (ADR-0032), so a supervisor can tail progress.

Why reverse the no-loop stance now. The loop shipit deliberately dropped did
not disappear — it moved into whichever agent or human drives the cycle, and
ADP00 measured what that costs. An agent driver's sleep economics are invisible
to the operator and skewed by prompt-cache windows (naps cluster at 270s–1800s,
never 60s, because every wake is a paid model turn); the observed result was
minutes of dead time after every review landed, roughly an hour aggregate
across one epic's merge tails, and a poll cadence that varied by session mood
rather than policy. A blocking waiter inverts all three properties: detection
latency collapses to the poll interval, the interval becomes versioned tooling
config testable in one place, and the driving agent pays zero tokens while
parked behind it (run in the background, it re-invokes the driver exactly at
the event).

The escape hatch is two layers, by design. First, `--timeout` is a HARD
deadline with required semantics: on expiry the verb exits promptly with a
distinct code and a state report ("still waiting on: copilot re-review") — a
waiter that can hang forever merely relocates the hang it was built to remove.
Second, the supervising coordinator keeps its own slow heartbeat (~20–30 min)
INDEPENDENT of any watch, owning the states nobody foresaw: a waiter that died
silently, a subagent gone idle, a state machine wedged in a shape the verb
does not model. The heartbeat is the invariant; the waiter is the
optimization.

Considered and rejected: a `--watch` flag on `pr status`/`pr next` (mixes
blocking into verbs whose value is being pure, composable reads; the waiter
must be the ONLY thing that ever blocks); keeping the loop agent-side with a
mandated 60s nap (pays a model turn per tick and re-derives cadence per
session — the exact measured failure); webhook/event push from GitHub (real
infra — App event plumbing, delivery guarantees — bought to save a 60s poll
that is free at fleet scale); a waiter with no deadline (see escape hatch).

Consequences: `pr next`'s docstring points its no-loop stance at `pr wait`;
coordinator roles drive rounds as wait → dispatch → wait; a timeout exit is an
advisory state for the supervisor, not a failure of the PR; the poll interval
joins config with a documented default (60s) and the attach-verification poll
in `prstate/request.py` remains separate (it verifies request placement, not
round arrival).
