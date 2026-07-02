# CLI/API separation — parse-to-values, typed results, render at the edge

> Spec for the CLI boundary contract (ADR-0030) and its rollout. Two epics
> execute it: **CLI01** (the seam + `PrId` + `Roster`, proven on the pr
> family) and **CLI02** (the offender-verb promotions). Glossary: CONTEXT.md
> (**PrId**, **Roster**); decisions: ADR-0030, building on ADR-0002 (verb
> convention), ADR-0021 (value objects / functional core), ADR-0024 (core
> identities), ADR-0028 (typed adapter results), ADR-0029 (agents-first
> logging).

## Problem Statement

shipit's CLI is structurally uniform (click end to end, one `cmd → run → int`
convention, no stray `sys.exit`) but semantically tangled. A verb's `run()` is
a print-infused orchestrator: it resolves identity, calls the git/gh adapters
directly, interleaves rendering with mutation, and returns a bare int — so the
only way to observe an outcome is to capture a terminal. The cost lands in
four places:

- **Testability.** Verb tests rebuild the world by monkeypatching module
  functions (identity probes, gh calls, launchers) 4–6 layers deep per happy
  path, then assert on rendered whitespace. The clean suites in the codebase
  (the PR state engine's typed-fixture tests) prove the same coverage costs a
  fraction when the seam is typed.
- **Repetition.** The same error-mapping block is copy-pasted across the pr
  verbs; "optional REPO defaults to the current checkout" exists in five
  variants (three of them a network shellout per invocation, contradicting the
  offline identity source ADR-0024 fixed); shared option stacks are defined
  twice with divergent help.
- **Primitive obsession at the top edge.** ADR-0024/PROC03 closed the bottom
  edge — Tool adapters return `Repo`/`PR`/`Sha` — but the top edge still
  speaks primitives: PR numbers travel as bare ints with their repo re-derived
  along the way, reviewer configuration is three parallel string-keyed dicts
  behind the module-global caches ADR-0021 explicitly bans.
- **No pure Python API.** Consumers (tests, future tooling, agents importing
  shipit) cannot invoke a verb's behavior without a terminal; the logic worth
  calling is trapped inside verb modules alongside its presentation.

## Solution

Finish the arc: the same typed-value discipline that PROC03 applied to the
adapter boundary, applied to the CLI boundary. Every verb becomes three
separable pieces — **click parses argv into value objects; one pure domain
function takes those values and returns a typed result (logging, never
printing); a verb-layer renderer turns the result into text or `--json`
output, with the exit code derived from the same result.**

The pure Python API is not a new namespace: **the domain packages ARE the
API**. Verb families whose logic squats in the verb layer get real domain
homes; the verb layer shrinks to click glue plus renderers. Identity arrives
typed from the top (`PrId`, ambient `WorkingDir` resolved once at the CLI
root) the way it already leaves the adapters typed at the bottom. Reviewer
configuration becomes one **Roster** value read once and passed. Errors follow
a two-tier contract (usage vs runtime) enforced by one shared shell instead of
per-verb copies.

The external command surface does not change — no command or flag renames
(external callers pin it); `--json` is added to read verbs; the agent-parsed
mutation sentinels stay.

## User Stories

1. As a test author, I want every verb's behavior reachable as a pure function
   over typed values, so that I can assert on a typed result instead of
   monkeypatching module internals and parsing captured stdout.
2. As a test author, I want rendering isolated in pure formatting functions,
   so that text-output tests assert on a string return value rather than
   terminal capture.
3. As a test author, I want the argv-to-exit-code path covered by a thin
   wiring smoke layer, so that CLI regressions are caught without duplicating
   domain coverage at the CLI layer.
4. As the maintainer, I want one shared parameter library for the repeated CLI
   concepts (PR target, repo slug, path default, `--json`, `--dry-run`, the
   tree/spawn shape options), so that the same concept parses, validates, and
   documents identically on every verb.
5. As the maintainer, I want malformed arguments rejected at parse with a
   usage error, so that validation is click's job and verb bodies contain no
   argument-checking boilerplate.
6. As the maintainer, I want one error shell mapping the known runtime
   exception set to a uniform stderr message, so that the copy-pasted
   try/except blocks and divergent error prefixes disappear.
7. As a script author (pixi task, CI step, shell pipeline), I want a two-tier
   exit contract — usage errors distinct from runtime failures — so that my
   caller can tell "I called it wrong" from "it failed".
8. As an agent (coordinator or subagent) driving shipit, I want `--json` on
   every user-facing read verb, so that I parse structured output instead of
   scraping human-formatted text.
9. As an agent parsing spawn/tree output, I want the existing sentinel + JSON
   surfaces left byte-stable, so that my existing parsing keeps working
   through the refactor.
10. As the maintainer, I want the ambient repo resolved once at the CLI root
    (offline, from the origin remote per ADR-0024) and threaded to every verb,
    so that the five divergent repo-default implementations and the per-fetch
    network shellouts disappear.
11. As a user running `pr next`, I want the readiness fetch to stop resolving
    the repo via the network twice per invocation, so that the verb is faster
    and works consistently with the offline identity.
12. As a developer calling PR services, I want the PR target to travel as a
    `PrId` — `(repo, number)` as one value — so that knowing *which* PR never
    requires a wire fetch and the repo can't be re-derived inconsistently
    along the way.
13. As a developer reading the PR state engine, I want reviewer configuration
    as one `Roster` value read once at the boundary, so that required/rerun/
    window/run-options can never disagree with each other and no module-global
    cache exists to reset in tests.
14. As the maintainer, I want the reviewer-cache autouse test fixtures to
    become unnecessary, so that tests stop coupling to process-global state.
15. As a developer importing shipit, I want each verb family's logic in its
    domain package with a typed signature, so that "what does `install`
    actually do" is an importable, callable answer rather than a verb module
    to read.
16. As a future CLI02 implementer, I want the boundary contract proven on the
    pr family first, so that promoting the offender verbs is mechanical
    adoption of an established pattern, not fresh design.
17. As the maintainer, I want the tree gc sweep's mutation loop out of the
    print function, so that destructive filesystem operations are drivable and
    observable without a terminal.
18. As the maintainer, I want gh-setup's three passes to return a typed report
    instead of printing from inside mutation loops, so that a dry-run or a
    caller can see exactly what would change.
19. As the maintainer, I want the spawn pipeline (validation → identity →
    tree → launch → post-condition audit) as a typed spec-to-result function,
    so that the fleet's most complex verb is testable stage by stage.
20. As the maintainer, I want the install reconciliation's plan/apply split
    surfaced as the API (plan pure, apply effectful), so that "what would
    install do" is a value I can inspect before any file is written.
21. As a user tailing logs, I want the log-reader engine (tail, rotation,
    malformed-line handling) callable as an iterator, so that its behavior is
    testable without a live terminal follow loop.
22. As a hook integrator, I want the hook verbs left exactly as they are —
    fail-open canon, stdin/stdout protocol — so that the safety-critical
    surface carries zero churn from this feature.
23. As a reviewer of shipit PRs, I want verbs held to "glue + render only" by
    a stated contract (ADR-0030), so that logic drifting back into the verb
    layer is a review finding, not a style debate.
24. As the maintainer, I want every promoted domain function to keep its
    durable log twin (ADR-0029) while prints move to the renderer, so that
    the JSONL record stays complete even as the terminal surface thins.

## Implementation Decisions

**The per-verb anatomy (ADR-0030).** Every user-facing verb decomposes into:
a click command using the shared parameter library; one domain function
`typed values in → frozen result out` that logs but never prints; a pure
`format` function plus a shared `emit` helper that renders the result as text
or `--json` and derives the exit code. No new behavior is added during
promotion — each verb's observable output is preserved except where this spec
says otherwise (exit codes for usage errors; added `--json` flags).

**Shared CLI machinery (CLI01).** Three small verb-layer modules: a parameter
library (custom click parameter types and reusable decorators that mint value
objects at parse — repo slugs via the canonical parser, paths with ambient
defaults, the shared shape-option stack with its exclusivity validation, the
`--json` and `--dry-run` flags defined once); an error shell (one decorator
mapping the known runtime exception set — exec, PR-state, config, domain
refusals — to a uniform `error: …` stderr line and exit 1); a render seam
(the `emit` helper plus the `format_*` convention, JSON serialized from the
result's `to_dict`).

**Two-tier exit contract.** Exit 2 = usage (argument errors, raised at parse,
owned by click); exit 1 = runtime failure (via the error shell); exit 0 =
success. This is an observable change for malformed-argument cases that
currently return 1; tests and callers are updated deliberately in the same
change that moves each validation. Hook verbs are exempt — their
fail-open/fail-closed canon is untouched.

**Identity threading.** The CLI root resolves the ambient `WorkingDir` once
(offline, origin-derived per ADR-0024) onto click's context as a frozen root
context; shared parameters use it as the default that an explicit REPO/PATH
argument overrides; verbs that need a repo but run outside one fail with one
uniform error. The per-fetch API-based repo resolutions inside the readiness
fetch path are deleted. Hooks (repo from payload cwd) and the detached review
child (explicit repo flag) keep their own entry points.

**PrId.** A frozen `(repo, number)` value — the identity half of the existing
PR noun, composed by it (the PR core exposes its own PrId). The PR-target
resolver returns a `PrId`; the readiness gather, reviewer request, ready flip,
and detached-review services take it in place of a bare int. Resolution of
"which PR" (explicit number vs the current branch's PR) stays a boundary call
inside the verb body — "no PR for this branch" is a runtime outcome with
per-verb semantics, not a parse-time usage error.

**Roster.** A frozen value holding every configured reviewer's settings
(required, rerun, wait window, run options), loaded once at a verb boundary
from repo config and passed as a value — replacing the three parallel
string-keyed dict resolvers and both module-global caches (discharging
ADR-0021 rule 4 for its named example). Reviewer *identity* stays with the
Backend/adapter registries; Roster entries are keyed by reviewer name (wire
strings). The readiness view's parallel per-setting dict fields collapse to
the roster.

**Domain packages are the API.** No `api` namespace, no facade. The pr-family
services parked under the verb layer (the reviewer-request service, the
next-action dispatch, the guarded ready-flip, the PR-target resolver) move to
their domain homes. CLI02 creates domain packages for the verb families that
lack one — install (unit model, plan/apply split: `reconcile → Plan` pure,
`apply(Plan, mode) → InstallResult` effectful), gh-setup (`setup → SetupReport`
with per-pass outcomes; prints leave the mutation loops), the log reader (an
iterator engine over the JSONL file with tail/rotation/malformed-line
handling) — and moves the spawn pipeline (spec → result, including the launch
post-condition audit) and the tree operations (gc as plan + sweep, removal
gating, fleet listing as typed records) into their existing packages.

**Output convention.** Every user-facing read verb gains `--json` (serialized
from the typed result); text rendering moves to pure format functions; the
mutation sentinels (`READY`, `SPAWNED`) are a frozen agent-facing surface and
do not change. Domain functions keep lifecycle logging (the durable twin,
ADR-0029); the renderer owns the terminal.

**Frozen surface.** No command or flag renames anywhere in this feature —
external callers (hook settings templates, pixi task blocks, role prompts,
in-code argv literals, bootstrap shims) pin the surface. Additions only.

**Epic split.** CLI01 = the seam machinery + identity threading + PrId +
Roster + the pr family promoted as the proof of the contract. CLI02 = the
four offender promotions (install, spawn, tree, gh-setup + log reader),
planned after CLI01 merges, each a mechanical adoption of the landed pattern.

## Testing Decisions

A good test here exercises external behavior through a typed seam: value in,
value out. The proven in-repo template is the PR state engine's test style —
typed views built from recorded fixtures, an injected clock, near-zero
monkeypatching — and the two already-clean verbs (the eval report's
aggregate/format/run split; verify-apps' typed liveness results).

- **Every new value object** (PrId, Roster, the root context) gets direct
  unit tests: construction-is-validation, equality, composition with the
  existing identities.
- **Every promoted domain function** gets prstate-style tests: typed inputs
  in, typed result out, boundaries injected as values — replacing the current
  4–6-monkeypatch verb tests as each verb migrates. Existing verb-glue tests
  are collapsed or rewritten in the same work stream that migrates their verb,
  never left asserting the old shape.
- **Renderers** are tested as pure functions: `format_*(result)` string
  assertions, JSON payloads asserted structurally (field-set equality, the
  existing idiom).
- **Wiring** keeps a thin smoke layer per verb: one or two full
  argv-to-exit-code round trips through the CLI entry, proving the click
  binding, the error shell, and the exit contract — not the domain logic.
- **The exit-contract change** (usage errors moving from 1 to 2) is asserted
  explicitly wherever a validation moves, so the behavior change is
  deliberate and visible in the diff.
- The reviewer-cache autouse reset fixture and the per-file hand-rolled
  fake stacks shrink or disappear as their reasons (module-global caches,
  print-fused verbs) are removed.

## Out of Scope

- **The hook verb family** — entirely untouched: no params library, no error
  shell, no promotion of their orchestration. Their fail-open/fail-closed
  canon and stdin/stdout protocol stay as they are.
- **Command/flag renames or removals** — the external surface is frozen;
  cleanup of surface inconsistencies (e.g. the three spellings of the repo
  argument across verbs) is deferred until a deliberate breaking-surface
  effort.
- **Changing the mutation-verb output** (`READY`/`SPAWNED` sentinel blocks) —
  agents parse these today.
- **New verb behavior** — this is a re-plumbing; no verb gains or loses
  functionality beyond `--json` availability and the exit-code contract.
- **A Run value object and eval-record typing** — the Run noun's fragments
  stay as they are; typing them is its own future effort.
- **The review-funnel/producer internals** beyond what Roster and PrId
  threading touch — the detached-review machinery's own cleanup (e.g. its
  slug-string round-trips) happens opportunistically where signatures already
  change, not as a goal.
- **Rust rewrite considerations** (ADR-0023) — unaffected.

## Further Notes

- ADR-0030 records the boundary contract and its rejected alternatives (an
  `api` namespace, a facade module, rc-1-for-everything, prints-in-services).
  CONTEXT.md carries the PrId and Roster glossary entries with their
  avoid-lists ("PrRef"/"PrTarget"; "policy" for reviewer config; a Reviewer
  identity competing with Backend).
- The sizing signal from the scouting pass: the verb layer is ~6,500 lines of
  which the true click glue is ~50–150 per module; the three worst offenders
  (install, spawn, tree) hold the bulk of the trapped logic. The monkeypatch
  counts in their test files (170+, 130+ call sites) are the before-metric;
  the prstate suites are the after-metric.
- CLI02's work streams are intentionally mechanical: the contract, the
  machinery, and a worked example (the pr family) all exist by the time they
  start. If CLI01 slips on scope, Roster is the piece that can split out —
  PrId and the seam are load-bearing for the pr-family proof; Roster is
  parallel.
