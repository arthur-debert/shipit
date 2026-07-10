# Convergence-curve objective and one-axis experiment cells

RVW02's experiments were one-shot 15-minute runs driven by an uncommitted
script, changing several things at once and judged on a single round-1 recall
number. We decided review experiments run as **Cells**: small declarative
in-repo files (fixture version + PR subset, pipeline shape, Invocation,
instructions variant, replicates, sweeps) that a thin `lab` verb resolves onto
the sanctioned offline replay driver, foreground, on subscription-billed CLI
backends. Every cell must name its **baseline** cell and the **single axis**
on which it differs — an unfair comparison should fail at PR review of the
cell file, before tokens burn. Cell runs are **idempotent by key**
(cell, fixture PR, fixture version, variant, replicate, sweep): banked
round records are reused, never re-run, so extending a curve pays only for the
new points.

The objective is a **convergence curve, not a round-1 score**: cells may run K
full **Sweeps** over the same range (blind, or informed by prior sweeps'
findings — an explicit declared mode), and the scorer reports cumulative
recall, cumulative false positives/precision, token cost, and latency at each
sweep point. Designs are compared **at equal budget** (recall per token/minute),
so a configuration that converges by sweep 2 at half the cost shows up as the
win it is instead of being penalized for surfacing findings "late". Round-1
exhaustiveness remains the product north star (the incremental-round
architecture of ADR-0045 depends on it) but is a reported point on the curve,
never a gate that discards a cell.

Two deliberate scope exclusions. **Fix-range/breaker dynamics are
observational-only**: multi-round replay would require simulating shepherd
fixes; instead, breaker-policy questions are answered from live review-round
telemetry, and only revisited with controlled machinery if round-1/sweep cells
plateau. **The calibrator re-enters as a late cell with an entry bar** — it is
measured as a precision intervention after the union baseline curve exists, and
refuting even one ground-truth positive fails the cell (#665); there is no
standing calibrator workstream. Capabilities cells need (per-dimension
Invocation overrides, informed-sweep prompt composition) live in the lab
runner, not in product Roster configuration, until a cell earns the promotion
with data.
