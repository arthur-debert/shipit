# Dev-cycle events: tagged records, three witness tiers, constrained emission

> **Status: Proposed.** Dev-cycle event log epic; builds on ADR-0029
> (agents-first JSONL, domain-key correlation) and #349 (session key exported
> into the session env).

The dev-cycle trail — agent spawned, review requested, review in, round
detected, breaker fired, PR ready — becomes **dev-cycle events**: ordinary
INFO records in the same per-repo JSONL file, distinguished only by an
`event` field carrying a dot-namespaced name from a CLOSED, additive
vocabulary (unknown names raise, the `DOMAIN_KEYS` discipline). No custom log
type, no new level, no second file: an event IS a log record, so it rides the
one pipeline (context-merge, redaction, rotation) and the one reader. The
correlation vocabulary grows by four keys — `epic` (code string), `ws` (Work
Stream index as int; `WS01` is rendering, not data), `agent` (spawn id),
`role` — bound where the value is known: spawn args at the spawn seam
(env-propagated like the rest, retiring the never-set `SHIPIT_EPIC` marker),
and the slash-namespaced branch (ADR-0016) at the PR verbs. Sessions carry no
epic: a session cannot know its purpose at start and may span epics, so the
coordinator's records get `epic`/`ws` per-operation and a flow view derives
the session theme from the stream (or from an explicit `session.intent`
event, emitted when intent crystallizes).

Emission has **three tiers, strongest preferred**. *Verb-witnessed*: the verb
performing the milestone emits it — unforgeable, zero prompt tokens, the dev
cycle's tier. *Hook-witnessed*: a shipit-managed git hook emits (post-commit
→ `commit.created`) — automatic, skippable only by a hook bypass, which the
eval store already counts. *Skill-scripted*: a skill's instructions include the
emission step — best-effort, and accepted as such; this is how the planning
cycle (grill, ADR/PRD written, epic/WS minted) enters the record at all,
since its milestones pass through skills and `gh`, not shipit verbs. The
write path for the last two tiers is one constrained verb (`shipit log
event`) that accepts ONLY registered event names — freeform narration stays
impossible on every tier, and the vocabulary registry is where new event
types get debated.

## Considered options

- **A custom event record type / separate event file** — rejected: forks the
  pipeline and the reader; every consumer would need to merge two streams the
  domain keys already unify. The `event` field costs one `select()`.
- **No agent-invocable write path** (verb-witnessed only) — rejected: it
  leaves the planning cycle and local commits invisible, which is the status
  quo this epic exists to fix. The registered-names-only constraint is the
  guardrail that made a write verb acceptable.
- **An open `shipit log "<message>"` verb** — rejected: an agent diary in the
  durable record, unbounded vocabulary, and the correlation/flow value
  evaporates.
- **A session-level epic binding** (`shipit session theme`, or the
  `SHIPIT_EPIC` env marker) — rejected: a session cannot know its epic at
  start, may span epics, and the branches already carry the identity;
  per-operation derivation is both truer and free.
- **Synthetic event/trace ids** — still rejected (ADR-0029): domain keys are
  the correlation.

## Consequences

- The flow of any session, epic, WS, PR, or agent is one filtered read
  (`shipit logs --epic RVW01 --ws 1 --events`); `--flow` renders it with
  friendly times and `RVW01-WS01:` prefixes. `/shipit-session-status` wraps
  `--flow --session current` as an operator helper.
- Dev-cycle observability costs agents zero tokens: the verbs they already
  must run emit the trail as a side effect. Adoption of the *verbs* is the
  only prompt-side ask, and that pressure already exists.
- Planning-tier events are best-effort by design; a missing
  `planning.prd.written` means a skipped skill step, not a broken invariant.
- Skill/command adoption metrics stay OUT: they are per-run behavioral
  telemetry and belong to the eval store's transcript extractors (ADR-0013),
  not the milestone vocabulary.
