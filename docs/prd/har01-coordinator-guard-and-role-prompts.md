# HAR01 — Coordinator guard & generated role prompts

> Epic HAR01 of the **agent harness** (rung 1 of HAR01–HAR04). Authoritative spec.
> Decisions: [ADR-0011](../adr/0011-role-scoped-generated-prompts.md),
> [ADR-0012](../adr/0012-enforcement-via-native-hooks.md). Vocabulary: `CONTEXT.md`
> (**role**, **role definition**, **role prompt**, **operation**, **policy**,
> **context predicate**, **break-glass**, **run**).

## Problem Statement

shipit ships a dev cycle (draft → shepherd → ready; the coordinator delegates, never
implements) but agents do not reliably follow it, and the failure is structural, not
incidental:

- The agent the human addresses — the **coordinator** — implements directly instead of
  delegating, *especially* on tasks that feel trivial ("it's just a typo"). Observed live in
  this very repo: on a one-line skill rename the coordinator edited files itself and cut the
  branch off a stale local `main`, straight into conflicts — the exact thing the dev cycle
  exists to prevent.
- The rules live in `AGENTS.md`, which agents *read* but lose as context builds. Documentary
  guidance is read-then-forgotten, so compliance depends on the agent remembering and
  *choosing* it — which is precisely what fails under load.
- An agent that sees *every* role's instructions drifts mid-session into behaving as a
  different role.

The rules are advisory text where they need to be mechanical. The cost is wasted context,
broken delegation, and a dev cycle that only holds when the agent happens to comply.

## Solution

Make role behavior **mechanical and role-scoped**, not documentary:

1. **Each agent receives only its own role's prompt, on a surface that binds.** A single
   sliceable source — focused **lex** role-definition fragments (a shared dev-cycle *base* +
   one *overlay* per **role**) — is composed via lex includes and **generated** into a
   reduced **role prompt** per role. A subagent role (`implementer` / `shepherd` /
   `explorer`) gets its prompt as its agent-def system-prompt body; the `coordinator` (the
   top-level session, which has no agent-def) gets the broad slice plus the role *map*, as
   injected context and as the enforcement **deny** reason. `AGENTS.md` is generated from the
   same source but is non-binding reference. One edit to the source re-flows everything, so
   the dev cycle is stated once (ADR-0011).

2. **The coordinator is physically prevented from implementing.** The `edit` **operation** is
   **blocking** when the actor's **role** is `coordinator` and the path is a code path and no
   **break-glass** marker is present — realized as a thin committed `PreToolUse` hook that
   `deny`s. We verified on Claude Code 2.1.195 that the hook fires for the coordinator's own
   tool calls, that `agent_type` distinguishes coordinator (empty) from subagent, and that
   `deny` overrides even `bypassPermissions`. The deny reason *is* the coordinator's
   role-prompt slice ("you coordinate; delegate; branch off origin/main"), so the rule
   arrives as a wall at the moment of action (ADR-0012).

3. **shipit dogfoods it** — shipit runs the harness on its own development, so the harness is
   tested, executed, and verified by the same loop that builds it.

The result: an agent cannot quietly drift role or silently implement when it should delegate;
the binding behavior no longer depends on anyone reading `AGENTS.md`.

## User Stories

1. As a maintainer, I want the addressed agent to refuse to implement and delegate instead,
   so that a "small" task doesn't quietly bypass the dev cycle.
2. As a maintainer, I want the coordinator's first file-edit on a code path to be blocked
   with a redirect, so that delegation is enforced, not hoped for.
3. As a maintainer, I want the block to override `bypassPermissions`, so that the most common
   agent run mode can't sidestep the rule.
4. As a maintainer, I want each agent to see only its own role's instructions, so that agents
   stop drifting mid-session into another role.
5. As a maintainer, I want the dev cycle defined in exactly one place, so that role prompts
   and `AGENTS.md` can never disagree.
6. As a maintainer, I want role prompts generated from that one source, so that editing the
   cycle re-flows every role's prompt with no hand-syncing.
7. As a maintainer, I want a logged break-glass escape, so that the rare legitimate
   coordinator code-edit is possible but visible.
8. As a maintainer, I want break-glass uses recorded, so that frequent use is a signal to
   tighten the policy rather than a silent bypass.
9. As a maintainer, I want zero new `.shipit.toml` config for this, so that adopting the
   harness costs nothing to configure.
10. As a maintainer, I want the policy to be shipit's fixed behavior, so that a consumer can't
    weaken "the coordinator may not implement."
11. As a coordinator agent, I want my own prompt to tell me I orchestrate and delegate, so
    that I act as a coordinator from the first turn.
12. As a coordinator agent, I want the map of the roles I delegate to, so that I know what an
    implementer vs a shepherd is for.
13. As a coordinator agent, I want to still write planning docs (PRDs, ADRs, `CONTEXT.md`),
    so that the planning leg isn't blocked by the implementation guard.
14. As an implementer agent, I want a prompt scoped to implementing + opening a draft PR +
    stopping, so that I don't wander into shepherding or coordinating.
15. As a shepherd agent, I want a prompt scoped to addressing one review round and handing
    back, so that I stay in my lane.
16. As an explorer agent, I want a read-only, search-scoped prompt, so that I return findings
    without mutating the repo.
17. As a maintainer, I want the generated role prompts committed and hash-reconciled like
    other managed files, so that consumer edits surface in a PR instead of being clobbered.
18. As a maintainer, I want the coordinator's deny message to carry the actionable next step
    (delegate; branch off origin/main), so that the block teaches, not just stops.
19. As a maintainer, I want the guard to allow the coordinator to edit non-code paths (docs,
    config, `.lex`), so that authoring and planning proceed normally.
20. As a maintainer, I want a clear definition of "code path" that works before the
    path→toolchain map exists, so that HAR01 ships without waiting on Step 5–6.
21. As a future consumer, I want the same guard + role prompts installed into my repo, so that
    my agents follow the standardized dev cycle too.
22. As a maintainer, I want the hook contract pinned to the verified CLI version, so that a
    silent Claude Code change can't weaken enforcement unnoticed.
23. As a maintainer, I want role resolution to treat an empty `agent_type` as `coordinator`,
    so that the human-facing session is always governed.
24. As a maintainer, I want the rich logic in the binary and only a thin caller in
    `settings.json`, so that the hook wiring never drifts and behavior ships via the package.

## Implementation Decisions

- **Role is a closed registry** (mirrors **Toolchain** / **Reviewer adapter**):
  `coordinator`, `implementer`, `shepherd`, `explorer`. Per-consumer custom roles are out of
  scope. The `coordinator` is the empty-`agent_type` top-level session; the others are
  generated agent-defs (ADR-0011).

- **Five modules**, pure-core / thin-boundary split (shipit's existing shape):
  1. **Role resolver** — `resolve_role(hook_input) → Role`: encapsulates the `agent_type` /
     run-meta read and the empty⇒`coordinator` rule. Pure.
  2. **Edit-enforcement decision** — `decide(role, path, is_code, break_glass) →
     Decision{allow | deny, reason}`: the ADR-0012 **policy**. Break-glass is an input here,
     not a separate module. Pure; the security-critical core.
  3. **Code-path classifier** — `is_code_path(path) → bool`: ships an HAR01 **default** (e.g.
     `src/**`, `tests/**`, and known code paths) and **converges on the path→toolchain map
     when ADR-0007 lands**. HAR01 does not block on the unbuilt map. Pure.
  4. **Role-prompt generator** — `render(role_defs) → {role → role prompt}`: composes the lex
     base + per-role overlay fragments (via lex includes) into the reduced per-role prompts
     and the `AGENTS.md` union. Wraps lexd / the existing lex→file generation.
  5. **Hook boundary** — the `shipit hook pretooluse` subcommand: parse stdin JSON → call
     `decide()` → emit the `hookSpecificOutput` decision on stdout. Thin; no logic beyond
     I/O marshalling.

- **Enforcement realization** (ADR-0012): a thin committed `PreToolUse` line in
  `.claude/settings.json` → `shipit hook` (rich logic in the versioned binary) → reads only
  data that already exists. The policy is **fixed shipit behavior, zero new consumer config**.
  The `deny` decision carries `permissionDecisionReason` = the coordinator role-prompt slice.

- **Delivery surfaces** (ADR-0011): subagent role → agent-def system-prompt body; coordinator
  → injected context + the deny reason. `AGENTS.md` carries at most the role *map*, never a
  role's marching orders; its exact content is a knob HAR02 later tunes.

- **Break-glass**: a logged marker (mechanism — env flag vs per-edit marker — is an
  implementation detail to settle in build) that lets the coordinator perform a blocked
  `edit`. Every use is recorded so its frequency is measurable (an HAR02 signal). Included at
  rung 1 (start permissive + measure, tighten later).

- **Managed-file integration**: the generated role prompts and the `.claude/settings.json`
  hook line join the slow/committed managed set, hash-reconciled by the existing install
  algorithm (consumer edits surface in the PR, never clobbered).

- **Verified mechanics** (Claude Code 2.1.195, recorded so they aren't re-derived): hooks
  fire for the coordinator's own calls and recursively in subagents; `agent_type` present iff
  subagent (empty ⇒ coordinator); `permissionDecision:"deny"` blocks even under
  `bypassPermissions`; per-run `.meta.json` carries `agentType` / `model` / `permissionMode`.
  The hook contract is pinned to this version.

## Testing Decisions

- **Good tests assert external behavior, not implementation** — a decision's allow/deny
  verdict and its reason, a generated prompt's content; never internal call shapes.
- **Modules tested: #1–#4.**
  - **#2 Edit-enforcement decision** — table-driven over `role × is_code × break_glass →
    expected{allow|deny}` (the security-critical matrix); assert the coordinator is denied on
    a code path, allowed on a doc path, and allowed under break-glass.
  - **#1 Role resolver** — fixture hook payloads (empty `agent_type`, each named role) →
    expected `Role`; the empty⇒coordinator case is the load-bearing one.
  - **#3 Code-path classifier** — code vs non-code path fixtures → expected bool, including
    docs / `.lex` / config as non-code.
  - **#4 Role-prompt generator — the *reduction property*** is the key test: a generated role
    prompt **contains its own overlay and NOT the other roles' overlays** (directly tests the
    anti-drift guarantee), and the `AGENTS.md` union contains all. Use lex-fragment fixtures.
- **#5 Hook boundary** gets a single thin integration test: feed a sample `PreToolUse`
  payload, assert the emitted `hookSpecificOutput` JSON matches the expected decision — not
  broad coverage.
- **Prior art**: the PR state engine tests (pure state-from-snapshot) and the lint tests
  (pure routing) — same pure-core / thin-boundary testing split shipit already uses.

## Out of Scope

- **HAR02** — session **eval** (objective metrics, the run records). HAR01 only *emits* the
  break-glass / deny log lines HAR02 will later read.
- **HAR03** — tool-wrap side-effects firing the PR state machine (`PostToolUse`
  auto-request-review, max-round flip).
- **HAR04** — the subjective agent-as-judge eval.
- **Per-consumer custom roles** — the registry ships fixed and closed.
- **The declarative path→toolchain build map itself** (ADR-0007 / Step 5–6) — HAR01 ships a
  default code-path classifier and converges on the map when it exists.
- Tuning whether `AGENTS.md` carries the role map — decided empirically by HAR02.

## Further Notes

- **Dogfooding is the validation**: shipit runs HAR01 on its own dev loop, so the guard and
  the role prompts are exercised by the same sessions that build the rest of the harness.
- **Disposition: start permissive, measure, tighten.** Break-glass ships at rung 1; HAR02's
  metrics (break-glass frequency, role-drift incidents) decide whether to tighten — not
  intuition.
- **Bootstrapping wrinkle**: the guard blocks the coordinator's code edits, including edits to
  the harness's own code. Authoring the role-def *fragments* (lex) and docs is allowed (not
  code paths); touching the hook's binary code uses delegation or break-glass. The escape
  hatch must therefore exist before the guard is switched on for shipit itself.
- **Portability**: `AGENTS.md` is an emerging cross-tool standard (Amp/Cursor/Antigravity read
  it), so the generated reference stays useful beyond Claude Code even though the *binding*
  surfaces are Claude-Code-specific.
