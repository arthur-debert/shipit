# Publish fires a selectable endpoint subset; the Release stays whole

`shipit release publish` walks the `[artifacts]` map and fires every declared
endpoint of every artifact in one event. Exactly two booleans on the closed
adapter registry discriminate what runs: `external` (skipped by the
`-release-rc` live-fire guard) and `stable_only` (skipped on a prerelease).
Nothing else subsets a publish.

That blocks a real need. Seeding the Artifact channel (ADR-0064) from
`lex-fmt/lex` requires the derived `conda` endpoint to fire for a channel
artifact — first `lexd-lsp` (the VSIX's staged language server), later `lexd`
itself (the lint-gate tool whose stable publication gates ADR-0066's cutover) —
while `crates` (declared on `lexd`) and `npm` (declared on `lex-wasm`) — sibling
artifacts of the same repo, fired by the same event — must not: those are
owner-gated live publishes to third-party registries, and they cannot be
unpublished. Both seeds hit the identical wall, which is why the mechanism is
general rather than scoped to one artifact. Today only two shapes exist, and neither seeds the channel safely:
a `-release-rc` skips *every* external endpoint including `conda`, and any other
tag fires `conda` *together with* `crates` and `npm` (neither is `stable_only`).
The channel is therefore unseedable without collateral, and `lex`'s own
`.shipit.toml` already says as much — "the live channel SEED is coordinator-owned
and NOT dispatched in this change".

## Decision

- **`shipit release publish` takes a repeatable `--endpoint <name>` selector.**
  When present, only the named endpoints publish; every other endpoint is
  skipped with its own recorded reason (`SKIP_SELECTOR`), the way the RC guard
  already records its skips. Absent the flag, behavior is unchanged — the full
  plan fires.
- **The selector is a plan-level filter.** It applies in `plan()`, so the
  existing plan-only/dry-run preview shows exactly what will fire *before*
  anything external happens. `plan()` stays the one place that decides "what
  actually runs", and the closed adapter registry is untouched.
- **The selector narrows distribution, never the Release.** The build/sign
  barrier still runs over the *entire* declared artifact set, and `gh-release`
  still carries every artifact's assets — the Release that lands is complete
  (ADR-0009). `--endpoint` cannot deselect `gh-release`; a run that tries is
  refused.
- **Derived endpoints keep their base.** Selecting a derived endpoint (`conda`,
  `brew`) requires its base endpoint in the plan, preserving ADR-0009's
  release-before-derived ordering. A selector that would orphan a derived
  endpoint is refused, not silently repaired.
- **Per-invocation only.** `--endpoint` is never a `.shipit.toml` field. A repo
  cannot declare a permanently-subsetted publish; a subset is an operator's
  deliberate act on one run, and the next run is whole again.
- **It composes with the existing guards by intersection.** The `-release-rc`
  live-fire guard still skips every external endpoint, so a seed uses an
  ordinary prerelease tag — not the reserved `-release-rc` suffix.

### Alternatives rejected

- **An artifact-scoped filter (`--artifact`)** — subsets the Release itself: the
  GitHub release would carry only one artifact's assets, which is precisely the
  partial release ADR-0009 exists to prevent, and it would make the seed depend
  on how a producer happens to decompose its artifacts (adding `crates` to
  `lexd-lsp` later would silently re-arm the collateral). Endpoint-scoping
  leaves the Release whole and narrows only distribution — the shape the RC
  guard already established.
- **A conda-specific exception to the live-fire guard** (let `-release-rc` seed
  the channel) — bends the guard's single, legible meaning ("a rehearsal touches
  nothing external") for one endpoint, and buys one special case where a general
  mechanism is needed; the next seedable derived endpoint re-opens the same
  argument.
- **Authorize one combined stable fire** (accept `crates` + `npm` publishing
  alongside the seed) — trades two irreversible third-party publishes for a
  mechanism the portfolio needs anyway, and leaves the channel permanently
  unseedable-without-collateral for every future reseed.
- **Seed out of band** (run `rattler-build` + upload by hand) — bypasses the very
  endpoint under test, is unrepeatable, and escapes the drift guard that keeps
  the `conda` entry mirrored across `ENDPOINTS` / `ENDPOINT_SECRETS` / the
  publish adapter registry.

## Consequences

- **Seeding becomes an ordinary, repeatable, auditable release run:** a plain
  prerelease tag plus `--endpoint gh-release --endpoint conda` publishes the
  complete Release and the `.conda`, with `crates` and `npm` recorded in the plan
  as selector-skipped. Reseeding after a channel loss is the same one command.
- **The selector is sharp, deliberately.** An operator can publish a stable
  Release whose external registries lag the tag. The plan preview and the
  recorded skip reasons make that visible, and the per-invocation-only rule keeps
  the lag from ever becoming a repo's steady state.
- Every publish gains one uniform place — `plan()` — where "what will fire" is
  both decided and shown; the RC guard, the `stable_only` rule, and the selector
  are three inputs to one intersection rather than three scattered behaviors.
- `--endpoint` names must be validated against the closed registry: an unknown or
  misspelled endpoint is an error, never a silent no-op that publishes nothing.
