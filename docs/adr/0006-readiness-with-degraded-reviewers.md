# Readiness with degraded reviewers: outcome-recorded, not review-succeeded

Once the review funnel (ADR-0005) makes a local review's failure *visible*, we
must decide what a failed / absent / empty / timed-out **required reviewer** does
to **Ready**. Two failure modes bound the decision:

- **Silent park** — a broken, quota-limited, or timed-out external reviewer holds
  the PR forever (the disaster the observability spine exists to kill). Lots
  outside shipit's control can cause it: an unpaid subscription, a new ToC to
  accept, an outage, a model timeout.
- **Silent erosion** — if we instead just *ignore* failures, "broken bits get
  mistaken as flakes," we move on, and before long nothing actually works but
  every PR still says "fine."

The engine is a stateless pure function from snapshot → state (CONTEXT.md), and
shipit deliberately dropped release-core's looping `pr wait` — so there is no
clock in the system to lean on.

## Decision

- A required reviewer is **settled** when its funnel reaches a **recorded terminal
  outcome** — *posted* / *empty* / *failed* / *timed-out*. **Reviewed** = every
  required reviewer **settled** + every thread from a *posted* review resolved. It
  is NOT "every required reviewer **succeeded**."
- *failed / empty / timed-out* → **settled but non-blocking**: it does not hold
  Ready, but the PR is surfaced as **degraded** (the named reviewer + its outcome),
  so the state is never *silently* "fine." Visibility is the guard against erosion;
  non-blocking is the guard against the silent park.
- Only **never-requested** and **in-flight within the wait window** actually
  **hold** the PR.
- **Wait window:** uniform across reviewer kinds, aged from each reviewer's own
  request timestamp — the check run's `started_at` for a local reviewer, the
  `review_requested` edge time for an App reviewer. **20m default, per-reviewer
  override.** In-flight past the window → *timed-out* → settled.
- **The engine stays stateless:** time enters as an **input** ("now" in the
  snapshot); the engine keeps no clock and release's looping `pr wait` is NOT
  revived.
- **Provisioning failures are treated identically to runtime flakes** —
  non-blocking + loud. A consumer mid-rollout (its review App still lacks
  `checks:write`, ADR-0005) sees that reviewer perpetually *degraded*, never
  blocked. Making "not provisioned" block would recreate the very "one broken
  thing parks every PR" disaster on every half-rolled-out repo.

### Alternatives rejected

- **Block on any non-succeeded required reviewer** — the intuitive gate; recreates
  both the silent-park and the one-broken-reviewer-blocks-everything failures.
- **Silently skip failed reviewers** — invites the erosion above; the failure
  becomes an invisible flake.
- **Revive a stateful `pr wait` poller** to own the clock — re-adds the looping
  component shipit deleted; the stateless-engine-plus-injected-now achieves the
  same wait without it.

## Consequences

- `pr status` gains a **degraded** annotation (which required reviewers failed /
  timed-out, and why); a clean-but-degraded PR reports **"Ready (degraded: …)."**
- The next-action **dispatcher routes on structured funnel state**, not on the
  engine's human-facing `next_action` prose — which absorbs the deferred PRF01
  finding (issue #24.1) into this work rather than leaving it as a separate fix.
- The engine's snapshot carries "now"; it stays pure and unit-testable (a fixed
  "now" + a recorded snapshot → a deterministic state), with no wall-clock in the
  decision path.
- "Reviewed" / "Ready" change meaning fleet-wide; CONTEXT.md's glossary is updated
  in lockstep (this is why the change is recorded here, not just in code).
