# Lint-gate orchestration lives in the binary

The lint gate's per-language discovery, routing, and aggregation live in the
`shipit lint` **binary** — not in lefthook, and not templated into the consumer's
`pixi.toml`. This inverts release-core's shape, where lefthook is the orchestrator
carrying the per-language glob map. In shipit, lefthook and pixi stay thin one-line
callers (`pixi run lint` → `shipit lint`), and all the rich logic sits in the versioned
package.

The reason is pixi's missing seam: pixi has **no cross-manifest task inheritance**, so a
consumer cannot inherit or override a task shipit defines elsewhere. The only way to put a
rich task into a consumer is to template it into that consumer's `pixi.toml` — which makes
the manifest a managed-but-edited file, i.e. drift on the most important config file. Put
the logic in a binary instead and the consumer's `pixi.toml` carries only a stable,
never-drifting one-line task.

It is a **hard gate**: a missing tool fails non-zero, never skips. There is exactly one
gate definition, so CI's `pixi run lint` and the local pre-commit hook run the identical
binary with the identical config — "both agree" because there is one transcription of the
rules, not two. Full rationale is in `docs/dev/architecture.lex §5` (why a binary, not
templated tasks) and `§7` (the gate: one definition, hard).

## Consequences

- lefthook and `pixi.toml` stay dumb thin callers; neither carries per-language logic, so
  neither drifts.
- The orchestration is plain testable code in the package, kept out of the subprocess
  boundary so it is unit-testable (shipit's pure/boundary split).
- An unprovisioned linter fails the gate loudly rather than quietly skipping, so the gate
  cannot silently weaken.
- The gate definition cannot fork between local and CI: there is one binary, one config.
