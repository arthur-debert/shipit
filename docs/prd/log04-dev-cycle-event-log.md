# Dev-cycle event log: tagged events, domain-key extension, flow view

> Spec for the dev-cycle event log epic. Decision records: ADR-0032 (dev-cycle
> events — tagged records, three witness tiers, constrained emission) over
> ADR-0029 (agents-first JSONL logging). Groundwork: #349 (session key exported
> into the session env) should land first.

## Problem Statement

Shipit's durable log records what subsystems did, but not the *story* of a
session: which agents were spawned for which Work Streams, which PRs got
reviews requested and received, which rounds were addressed, when a breaker
fired, when a PR flipped ready. The operator reconstructs that story by hand
from GitHub tabs and terminal scrollback; agents are oblivious to the log
entirely (there is nothing in it shaped for them to consume, and no way for
the planning cycle to appear in it at all). Correlation stops at
`session/tree/pr/run/repo` — the log cannot answer "show me everything for
epic RVW01 Work Stream 1" or "what did this shepherd agent do" — and the one
reader offers no filters beyond repo and tail count. As orchestration is
tuned and verified, there is no cheap way to observe whether the high-level
flow actually happened as designed.

## Solution

The dev cycle narrates itself. Every milestone a shipit verb already performs
— agent spawned, review requested, review received, round detected, breaker
fired, PR ready — is emitted as a **dev-cycle event**: an ordinary INFO
record in the same per-repo file log, tagged with an `event` name from a
closed vocabulary and correlated by four new **domain keys** (`epic`, `ws`,
`agent`, `role`) alongside the existing five. Local commits enter via the
shipit-managed post-commit hook; the planning cycle (grill, ADR/PRD written,
epic and Work Streams minted) enters via skill-scripted calls to one
constrained write verb that accepts only registered event names. The reader
grows domain-key filters and a `--flow` view that renders the story —
friendly relative times, `RVW01-WS01: PR 368: review requested` lines, a
session-theme header — and a `/shipit-session-status` skill wraps it for the
operator. Agents pay zero prompt tokens for any of it: the trail falls out of
the verbs they already must run.

## User Stories

1. As an operator, I want a `--flow` view of the current session, so that I
   can see at a glance what the orchestration did without reconstructing it
   from GitHub tabs and scrollback.
2. As an operator, I want every event line tagged `RVW01-WS01:` style, so
   that parallel Work Streams read as separate threads of one story.
3. As an operator, I want friendly relative timestamps in the flow view, so
   that "1h34m ago" tells me staleness without mental ISO-8601 arithmetic.
4. As an operator, I want `/shipit-session-status`, so that one slash command
   shows me the session story without remembering reader flags.
5. As an operator, I want agent ids collected on every event but shown only
   when I ask, so that the default view stays clean and the forensic detail
   stays available.
6. As a coordinator agent, I want `shipit logs --epic <code> --events`, so
   that I can orient on an epic's progress in one bounded read after a
   compaction or handoff.
7. As a coordinator agent, I want spawn/review/breaker/ready milestones
   emitted by the verbs themselves, so that the trail exists without me
   spending a single token narrating.
8. As an implementer agent, I want my spawn to bind `epic`, `ws`, `agent`,
   and `role` into my environment, so that every shipit command I run
   correlates to my Work Stream with no action on my part.
9. As a shepherd agent, I want `review.received` and `round.detected` events
   on the PR I tend, so that "what happened while I waited" is one filtered
   read.
10. As an operator, I want `commit.created` events from the managed
    post-commit hook, so that local progress is visible in the flow before a
    push ever happens.
11. As an operator, I want the planning cycle (grill started, ADR written,
    PRD written, epic/WS minted) in the same record, so that planning
    sessions stop being invisible.
12. As an operator, I want a `session.intent` event emitted when a session's
    purpose crystallizes, so that the flow view opens with "planning session:
    reviewer symmetry" instead of a guess.
13. As a future contributor, I want the event vocabulary closed and
    registered, so that a typo cannot mint a new event type and the registry
    is where new types get debated.
14. As a future contributor, I want events to be ordinary log records, so
    that one pipeline (redaction, rotation) and one reader serve both
    diagnosis and flow.
15. As an agent debugging a stuck PR, I want `shipit logs --pr <n> -f --raw`,
    so that I can watch that PR's records live and pipe them to jq.
16. As an operator, I want `--ws` to accept `1`, `01`, or `WS01` and
    normalize, so that the CLI never punishes me for typing the display form.
17. As an operator, I want the emit verb to reject unregistered names loudly,
    so that the durable record never becomes an agent diary.
18. As an operator, I want hook emission to be fail-open, so that logging can
    never block or slow a commit.
19. As an operator running multiple sessions on one repo, I want
    `--session current` resolved from my environment, so that "this session's
    story" needs no id lookup.
20. As the operator of future tuning work, I want the flow events queryable
    per epic across sessions, so that "how many rounds did RVW01 PRs really
    take" is a jq one-liner over the per-repo file.

## Implementation Decisions

- **ADR-0032 governs; ADR-0029 is unchanged in spirit.** Events are ordinary
  INFO records in the same per-repo JSONL file — an `event` field, no custom
  type, no new level, no second file. Correlation stays domain-keys-only.
- **Domain keys grow from five to nine**: `epic` (code string), `ws` (Work
  Stream index as int — `WS01` is rendering, never data), `agent` (spawn id),
  `role` (Role registry name). `ws` joins the int-typed keys. The closed-set
  guard, present-when-bound semantics, and `SHIPIT_LOG_CTX_*` env propagation
  extend unchanged.
- **Binding sites**: the spawn seam binds all four from its own arguments
  (retiring the never-set `SHIPIT_EPIC` marker gap); PR verbs bind
  `epic`/`ws` per-operation by deriving them from the target PR's
  slash-namespaced head branch (ADR-0016); sessions never bind an epic — a
  session cannot know its purpose at start and may span epics.
- **The event registry + emit core is a deep module**: one internal helper
  the verbs call, one closed dot-namespaced vocabulary, unknown names raise.
  Starting vocabulary (additive registry): `session.started`,
  `session.intent`, `tree.created`, `agent.spawned`, `agent.done`,
  `commit.created`, `review.requested`, `review.received`,
  `review.degraded`, `round.detected`, `breaker.fired`, `pr.ready`, and the
  planning family `planning.grill.started`, `planning.adr.written`,
  `planning.prd.written`, `planning.epic.minted`, `planning.ws.minted`.
- **Three witness tiers, strongest preferred** (ADR-0032): verb-witnessed
  (the verb performing the milestone emits — the dev cycle's tier),
  hook-witnessed (the managed post-commit hook emits `commit.created`),
  skill-scripted (planning skills call the emit verb at their checkpoints —
  best-effort by design and accepted as such).
- **One constrained write verb** (`shipit log event <name> [--about]`) serves
  the hook and skill tiers: registered names only, keys from the environment
  plus branch derivation, fail-open when invoked from hooks. There is no
  freeform write path on any tier.
- **Branch-identity derivation is a deep module**: a pure parse from a branch
  name to (epic, ws) — epic umbrella branches yield epic only, non-namespaced
  branches yield nothing. Shared by PR verbs, the emit verb, and the hook
  tier.
- **The reader grows filters, not a sibling**: `--session <id|current>`,
  `--epic`, `--ws`, `--pr`, `--agent`, `--role`, `--events`, composing as AND
  and working uniformly with `--raw`, `--follow`, and the tail count.
  Filtering is client-side over the bounded rotating file; no index is built
  until a real slicing gap shows.
- **The flow renderer is a deep module**: a pure function from filtered
  records to the rendered view — `session.intent` header when present,
  inferred theme otherwise; relative times; `EPIC-WSnn:` prefixes composed
  from domain keys; agent-id display as a flag with data always present.
  `--flow` implies `--events`.
- **`/shipit-session-status`** is a managed skill distributed like the other
  shipit skills; it wraps the flow view for the operator, and the underlying
  command is documented in role prompts so agents skip the skill round-trip.
- **Groundwork ordering**: #349 (session key into the session env) lands
  first; the spawn-seam export of the new keys follows the same pattern.

## Testing Decisions

- Tests assert external behavior: record contents, decision outputs, rendered
  text as a whole — never pipeline internals or incidental whitespace.
- **Emit core / registry**: registered name → record carries `event` +
  bound keys; unknown name raises; vocabulary is additive without downstream
  edits. Prior art: the logging convention sweeps and logcontext unit tests.
- **Branch derivation**: the full matrix — `EPIC/WSnn`, epic umbrella,
  standalone-issue branches, ephemeral branches, garbage — to (epic, ws) or
  nothing. Pure-core tests, no git.
- **Flow renderer**: record streams in → rendered views out, covering the
  intent header, inferred-theme fallback, multi-epic sessions, agent-id
  display toggle, and relative-time formatting. Prior art: the reader's
  existing render tests.
- **Reader filters**: composition (AND), `--ws` normalization (`1`/`01`/
  `WS01`), `--session current` resolution, `--events` selection — against
  fixture JSONL files, the existing reader-test pattern.
- **Emit verb**: name validation, key pickup from env + branch, fail-open
  exit when the log is unwritable. Prior art: hook verbs' fail-open tests.
- **Instrumentation sites**: each subsystem's existing tests extend with an
  assertion that the milestone record carries the expected `event` — the
  `test_logging_adoption_scoped` sweep pattern; no parallel event-test suite.
- **Hook tier**: one end-to-end test that a commit in a Tree produces a
  `commit.created` record and that a broken log path does not block the
  commit.

## Out of Scope

- `pr.opened` — no witnessing seam exists for a bare `gh pr create`; RVW01's
  first-request-at-open approximates it. Revisit if a PR-open wrapper verb is
  ever promoted.
- Push events — `review.requested` follows most pushes; add later if wanted.
- Skill-invocation and shipit/pixi command adoption metrics — per-run
  behavioral telemetry belonging to the eval store's transcript extractors
  (ADR-0013), not the milestone vocabulary.
- An index, a query engine, or per-session log files — the per-repo rotating
  file plus client-side filtering is the design (ADR-0029); revisit only on a
  demonstrated gap.
- Coordinator outer-loop mechanization (#343 gaps 1–4) — a consumer of this
  epic's events, planned separately.
- Synthetic trace/span ids — rejected again (ADR-0029, ADR-0032).

## Further Notes

- The design's one prompt-side ask is verb adoption itself, and that pressure
  already exists; every event this epic adds is a side effect of a command
  the flow already requires. Planning-tier events are best-effort by design:
  a missing `planning.prd.written` means a skipped skill step, not a broken
  invariant.
- The emit verb doubles as a free adoption signal: `session.intent` records
  incidentally show which sessions used the planning skills.
- The flow view is the intended data source for the future epic-status sweep
  (#343): the events give the sweep its cheap reads, the sweep gives the
  events their consumer.
