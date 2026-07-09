# Tools are shipit verbs, not consumer-named pixi tasks

The CI building blocks — `test`, `build`, `e2e`, and the `release` stages — follow
the `shipit lint` model (ADR-0004) rather than WF01's "consumer supplies a `test`
task" line: each is a shipit verb; the pixi task of the same name is a thin
one-line caller (`test = "./bin/shipit test"`, the ADR-0033 pinned-launcher
form). The tree-input tools (`test`, `build`)
walk the path→toolchain map (ADR-0007) and dispatch every entry to a producing
command — a registry default per toolchain (rust → cargo-nextest, go → go test,
python → pytest, npm → npm test, …) with a per-path override in `.shipit.toml`. `e2e` and the
release stages share the verb shape but take the artifact side as their axis:
`e2e` consumes a built artifact, the release stages walk the artifact map. We chose the verb because it gives one implementation
across laptop / hook / CI, multi-toolchain fan-out for free (a Tauri repo's
`shipit test` runs the rust and npm legs — a bare task would make the consumer
hand-chain them), uniform exit/reporting semantics, and it kills the silent
POSIX-`test` footgun. WF01's pixi-encapsulation text is superseded on this point,
as its pixienv half already was by ADR-0028.

## Consequences

- The stack's own surface stays reachable: passthrough args (`-- <args>`) forward
  verbatim to the dispatched command. On a multi-leg repo passthrough requires a
  leg selector (`shipit test rust -- --no-capture`); passthrough with several legs
  and no selector is a hard error, never a broadcast. Single-leg repos may omit
  the selector.
- A lane's `run` may name a leg (`test npm`), giving the lane planner
  per-toolchain jobs with no new concept.
- The binary carries a test/build dispatch registry — more shipit code, mirroring
  the lint Lang registry, accepted deliberately.
