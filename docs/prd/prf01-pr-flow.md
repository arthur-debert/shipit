# PRF01 — PR flow

> Epic: **PRF01** · Status: ready-for-agent · Plan: `docs/prd/FUTURE_WORK.md`
> ADRs: `docs/adr/0001-reuse-release-core-by-copy.md`, `docs/adr/0002-adapt-release-primitives-two-cli-conventions.md`
> Glossary: `CONTEXT.md` (PR-flow terms)

## Problem Statement

An agent (or human) driving a pull request has to hold the whole review protocol in
their head: which **required reviewers** have reviewed the current head, whether a push
staled a prior review, whether every thread is resolved, whether the review loop has gone
on too long, whether CI is green, and whether the merge state actually permits a flip to
ready. Getting any of these wrong means a PR flipped to ready too early, a reviewer
re-requested when it shouldn't be (burning a token/model run), or a PR parked forever
waiting on a signal that already arrived. Today shipit has **no** PR-flow tooling at all —
the logic that solves this lives only in release-core, behind release's command surface
and its `.release-sync.yaml` config, which shipit is deliberately leaving behind.

## Solution

Bring release-core's reviewer-agnostic **PR state engine** into shipit by copy (never a
wheel dependency) and expose it behind a clean, consistent `shipit pr` command group:

- `shipit pr status` — read where the PR stands: its lifecycle state and the single
  **next action**, as text or JSON.
- `shipit pr next` — *do* that one next action (request/re-request a review, or flip
  draft→ready when **Ready**), then report what happened.
- `shipit pr review request` — request (or re-request) the **required reviewers** on the
  current head, verifying the request actually attached.
- `shipit pr ready` — the guarded draft→ready flip (and `--undo`).

**What Ready means — the three pillars.** A PR is **Ready** (green) only when all the
generic, obvious work is done: (1) the code is correct — **Reviewed**: written, reviewed,
and every thread addressed; (2) the checks pass — CI green; (3) the PR is **Mergeable** —
no conflict, not behind its base, no unsatisfied branch-protection rule, keyed off the
authoritative merge-state signal (not GitHub's async-stale `mergeable` boolean). These
three are exactly the engine's pillar order, and `pr next` flips draft→ready only when all
three hold.

The engine is unchanged (the crown jewel is copied, not rewritten); the surface a user
touches is redesigned for consistency with the rest of shipit. Reviewer policy — which
reviewers hold **Ready** and whether each re-reviews every push — moves to a `[reviewers]`
table in `.shipit.toml`, alongside `[secrets]`. The default is **Copilot only,
review-once**, and Copilot works end-to-end in this epic. The **local-agent reviewers**
(`codex-local`, `agy-local`) are known to the engine (their reviews are detected, their
names resolve) but actually *running* a local review is deferred to a later step.

## User Stories

1. As an agent driving a PR, I want to ask where the PR stands, so that I know the one
   thing to do next without re-deriving the whole protocol.
2. As an agent, I want `pr status` to name the **next action** in plain language, so that I
   can act without interpreting raw GitHub state.
3. As an agent, I want `pr status --json`, so that I can branch a script on the lifecycle
   state, reviewer map, checks, and mergeability.
4. As an agent, I want a single `pr next` that performs the one next action, so that I can
   drive a PR forward one safe step at a time instead of orchestrating sub-commands.
5. As an agent, when no review has been requested yet, I want `pr next` to request the
   **required reviewers**, so that the review loop starts.
6. As an agent, when a push staled a prior review, I want `pr next` to **re-request** only
   the reviewers whose policy is head-strict (`rerun = true`), so that I don't pay for a
   re-review a review-once reviewer already gave.
7. As an agent, when the PR is **Reviewed** and CI is green and the merge state is clean, I
   want `pr next` to flip draft→ready and stop, so that the human is paged exactly once at
   the right moment.
8. As an agent, when threads are open, I want `pr next` to point me at the open threads
   rather than flip, so that I address them before readiness.
9. As an agent, when the review loop has hit the stopping rule (6 rounds, or the latest
   round is all **nitpicks**), I want an otherwise-ready PR to go **Ready** anyway, so that
   leftover nitpick threads don't park it forever.
10. As an agent, when CI is failing or the branch is behind/conflicted, I want `pr next` to
    report the real blocker, so that I fix the right thing instead of waiting.
11. As an agent, I want `pr review request` to verify the request actually attached, so
    that a silently-dropped request fails loud instead of parking the PR at
    reviews-pending.
12. As an agent, I want `pr review request` to skip reviewers already done on the current
    head, so that a bare re-run doesn't re-poke a finished reviewer.
13. As an agent, I want `pr ready` to refuse unless the engine says **Ready**, so that I
    cannot flip a PR that isn't actually done.
14. As an agent, I want `pr ready --undo`, so that I can send a PR back to draft when a
    human asks for changes.
15. As a maintainer, I want to declare which reviewers hold **Ready** in `.shipit.toml`, so
    that changing the required set is a one-line config edit, not a code change.
16. As a maintainer, I want a per-reviewer `rerun` flag (default false = review-once), so
    that re-reviewing every push is an explicit, cost-aware opt-in.
17. As a maintainer, I want the reviewer config to look like the rest of `.shipit.toml`
    (the `[secrets]` style), so that there is one config idiom to learn.
18. As a maintainer, I want a missing/empty `[reviewers]` table to fall back to the shipped
    default (Copilot, review-once), so that a repo works with zero reviewer config.
19. As a maintainer, I want an unknown reviewer name, a non-requestable reviewer in the
    required set, or a malformed entry to fail loud with a clear message, so that a typo
    never silently drops a required reviewer.
20. As a maintainer, I want to pre-declare `model` / `instructions` for a local-agent
    reviewer in `.shipit.toml`, so that the config is complete now even though it is
    consumed by the later local-agent step.
21. As a consumer-repo owner, I want `shipit pr` to work with only Copilot configured, so
    that I get the PR loop without standing up any local-agent infrastructure.
22. As an agent, when I request a `codex-local`/`agy-local` review before the local-agent
    step exists, I want a clean error telling me it isn't available yet, so that I get a
    readable message instead of a stack trace.
23. As a shipit maintainer, I want the engine copied verbatim with its tests, so that the
    behavior is identical to release's proven state machine and regressions are caught.
24. As a shipit maintainer, I want the engine's `gh` boundary kept separate from the
    verb-layer boundary, so that the stdlib-only engine runs identically in CI, Cloud, and
    local.
25. As an agent, I want `pr status`/`pr next` to read the **authoritative** merge state
    (not GitHub's async-stale `mergeable`), so that I don't flip on a stale optimistic
    verdict.
26. As an agent, I want best-effort reviewers (e.g. Gemini) to never hold **Ready**, so
    that an absent best-effort reviewer doesn't hold the PR.
27. As an agent, I want every `shipit pr` command to share the same option idiom (optional
    PR argument, `--json`, `--reviewer`), so that the group feels like one tool.
28. As a maintainer, I want a `gh`/auth failure to surface as a clean error and a non-zero
    exit, so that automation can detect and react to it.

## Implementation Decisions

### Reuse model (ADR-0001)

- The PR state engine is **copied** from release-core into shipit's package as the
  `prstate` subpackage; shipit never depends on the release-core wheel. The pure core (the
  data model, the state machine, the stopping-rule **breakers**, the thread helpers) is
  byte-for-byte; only relative imports are rewritten to absolute. The engine is stdlib-only.
- shipit keeps **two `gh` boundaries**: the existing verb-layer boundary (gh-setup /
  install) and the engine's own copied boundary (which adds GraphQL plus the PR-act calls
  the verb-layer boundary lacks). They are not merged; the small REST/pagination overlap is
  intentional duplication.

### CLI surface (ADR-0002)

- `pr` is shipit's first **nested** command group. All PR-flow verbs use shipit's single
  CLI convention (the inline command + `run(...) -> int` shape the setup verbs use). No
  passthrough wrapper from release is imported; release's command surface is **not**
  preserved — it is redesigned for consistency.
- The valuable logic currently entangled in release's CLI modules is **extracted** into
  composable, testable helpers rather than rewritten: the request-attach verification, the
  guarded draft→ready re-check, and the **next-action dispatcher** that maps a lifecycle
  state to the single act.
- The **next-action dispatcher** is its own deep module: a pure decision from a
  `TaskStatus` to the one act to take; the act's execution (request / flip) is an injected
  boundary so the decision is unit-testable without GitHub.

### `pr status` output contract

The JSON object is the engine's status, verbatim:

```text
{ pr, state, next_action, reviewers{name: lifecycle}, open_threads,
  checks, mergeable, cycles, breaker }
```

`state` is one of: `no_pr`, `reviews_pending`, `addressing`, `reviewed`, `validating`,
`ready`, `blocked`. Text output renders the same fields human-readably.

### `pr next` behavior

`pr next` resolves the PR (current branch if omitted), gathers a snapshot, evaluates it,
and performs the single next action keyed on the lifecycle state:

```text
no_pr           → report "create a draft PR" (the human's act)
reviews_pending → request / re-request the pending required reviewers, OR report "waiting"
                  when they are already requested/in-progress on the head
addressing      → show the open threads (read-only); the human resolves them
reviewed        → report "mergeability computing — re-check"
validating      → report "CI running — wait"
ready           → flip draft→ready (guarded), then stop
blocked         → report the real blocker (conflict / behind / failing CI / merge blocked)
```

It is the single-shot form of release's looping `wait` — the polling loop is dropped; the
guarded flip reuses the shared `ready` helper.

### Reviewer policy config

- A new `[reviewers]` table in `.shipit.toml`, parsed by a new pure config module that
  mirrors the `[secrets]` parser. Inline-table style:

```toml
[reviewers]
copilot     = { rerun = false }                         # default if table absent
codex-local = { rerun = false, model = "pro", instructions = "docs/review.md" }
```

- The **required reviewer** set is the table's keys; every key holds **Ready**. Per-reviewer
  options: `rerun` (bool, default false = review-once) consumed now by the engine; `model`
  and `instructions` parsed and validated now but **reserved** for the deferred local-agent
  step. Unknown options, unknown/non-requestable reviewer names, and duplicates fail loud.
- The backend of a local-agent reviewer is **derived from its name** (`codex-local` → codex,
  `agy-local` → agy); there is no separate backend field.
- An absent or empty `[reviewers]` table falls back to the shipped default
  (`{ copilot: rerun=false }`). A CLI flag (e.g. `--reviewer`) overrides config at
  invocation.
- This **replaces** release's `.release-sync.yaml` + `yq` seam with an in-process `tomllib`
  read. The process-lifetime config cache release needed (to avoid a `yq` subprocess per
  poll) is **dropped** — it does not apply to an in-process TOML read.

### Scope boundary — local-agent execution (WS07, in scope)

- The full reviewer-adapter registry is copied (copilot, coderabbit, gemini, codex-local,
  agy-local), so the engine **detects** an existing local-agent review and resolves every
  name. Copilot — an **app reviewer** — needs only a single `gh` reviewer-edit.
- **Running** a local-agent review (the release-core `review/` engine: backends, prompt,
  GitHub-App auth) is **in scope** as **WS07** (the original deferral is reversed — every
  adapter is PRF01 work, and the epic dogfoods the local reviewers on its own PRs). The
  earlier WS01 lazy-import **guard** is replaced by the real ported `review/` subpackage;
  `_LocalReviewAdapter.request()` runs the agent over the diff and posts as the bot.
- The bot **identity** is sourced from Doppler via shipit's **existing** `secretsrc` /
  `[secrets]` infra — `CODEX_REVIEW_APP_PRIVATE_KEY` / `CODEX_REVIEW_APP_ID` (and `AGY_…`)
  in `github/prd` — and signed in-memory by PyJWT, so the key never lands on disk (the
  decided divergence from release's loose-`.pem`-on-disk model). The `adr-codex-review` /
  `adr-agy-review` Apps are already minted and installed on `arthur-debert`; their PEMs were
  migrated off `~/.config` into Doppler as PRF01 **pre-work** (done). The reserved
  `[reviewers]` `model` / `instructions` fields are **consumed** here.

## Testing Decisions

A good test here asserts **external behavior** — given a recorded PR snapshot (or a parsed
config), the engine reports the right state / next action / verdict — never an
implementation detail. The engine already tests this way (pure functions over captured
JSON, the boundary monkeypatched), which matches shipit's own test conventions, so the bar
is "port what still earns its keep," not "port everything."

**Tests to bring/write (evaluated, not blanket):**

- **Port the engine suites** for the state machine, the breakers, the reviewer adapters,
  the data model, and the snapshot assembly (`fetch`), along with their captured-JSON
  fixtures. These are the highest-value tests and exercise the copied behavior directly.
- **Adapt, don't port wholesale, the local-reviewer tests.** Tests that exercise a
  local-agent adapter *running* a review (the deferred `review/` engine) cannot pass here;
  replace them with **one** test asserting the deferred-import **guard** returns a clean
  error. The local adapters' *detection* tests (reading an existing bot review) stay.
- **New tests for the two new deep modules:** the `[reviewers]` config parser (table-driven
  over the validation cases — unknown name, non-requestable, duplicate, bad `rerun` type,
  reserved fields accepted, empty→default; mirrors the secret-source tests) and the
  **next-action dispatcher** (each `TaskState` → expected act, pure, boundary injected).
- **Boundary-injected tests** for the act helpers: the request-attach verification (poll
  behavior with a faked boundary) and the guarded flip (refuses when not Ready, flips when
  Ready). These hold real logic and are worth the injected-boundary test.
- **A few CLI smoke tests** for the `pr` group: that the commands wire up and that
  `pr status --json` emits the documented field set. Keep this minimal — do not test click
  plumbing or re-test the engine through the CLI.

**Tests deliberately NOT written:**

- No `proc` tests — that module is not copied in this epic (the engine boundary shells out
  directly).
- No dedicated test for the text/JSON **render** helper — the JSON is the engine's existing
  `to_dict()` (already covered) dumped to a string; the one CLI smoke test covers the shape.
- No new tests that merely re-assert engine behavior already covered by the ported suites.

## Out of Scope

- **Minting NEW review Apps**: running the manifest mint/install flow to register a
  brand-new bot App or install it on a new owner. The `adr-codex-review` / `adr-agy-review`
  Apps already exist and are installed on `arthur-debert`; WS07 *uses* them (auth + post),
  it does not re-mint. Provisioning the `codex` / `agy` CLIs is likewise assumed (they are
  on the dev/agent PATH), not automated here.
- A looping/blocking `pr wait` (only the single-shot `pr next` is in scope).
- The `pr resolve-thread` / `pr review reply` push-back surface beyond what `pr status` /
  `pr next` need to *report* (threads are surfaced, not auto-resolved).
- Any change to the install/managed-set flow that would carry the `pr` config or skills
  into a consumer — install integration is its own concern.

## Further Notes

- CodeRabbit and Gemini adapters come along in the registry but are not in the default
  required set; CodeRabbit remains an opt-in (its App is only installed on some repos) and
  Gemini is best-effort (never holds Ready). This epic does not onboard either.
- The verification target is a real PR on a throwaway test repo with Copilot as the required
  reviewer: `pr status` reports each lifecycle state correctly, and `pr next` requests the
  review, then — once reviewed, CI green, merge state clean — flips draft→ready and stops.
- Execution topology (the six Work Streams and their dependency waves) lives on the PRF01
  epic issue, not here — per `AGENTS.lex` §2.2.3, that is execution detail.
