# Agent-lifecycle enforcement via native declarative hooks, not a framework

The agent harness must enforce role behavior at the moment of action — first and foremost
"the **coordinator** never implements." The field's answer is middleware/guardrail
*frameworks* (LangChain `AgentMiddleware`, OpenAI Agents SDK guardrails) — imperative code
libraries you wire into an agent runtime. But Claude Code uniquely exposes the *same*
interception **declaratively**: `PreToolUse` / `PostToolUse` hooks configured in
`.claude/settings.json`, fed JSON on stdin, returning a JSON decision — and a hook can `deny`
(block),
return `updatedInput` (mutate), or `additionalContext` (inform). We verified on Claude Code
2.1.195 that these hooks fire recursively in subagents, that `agent_type` is present iff the
caller is a subagent (empty ⇒ coordinator — the **role** signal), and that `deny` overrides
even `bypassPermissions`.

**Decision.** Enforcement reuses the existing **operation** / **policy** model (no new
vocabulary) and is realized by **ADR-0004's pattern applied to lifecycle hooks**: a thin,
committed hook line in `.claude/settings.json` → `shipit hook <name>` (all rich logic in the
versioned binary) → reading only data that already lives in `.shipit.toml`. We do **not**
adopt an agent framework, and we do **not** invent a declarative tool-wrap config language —
the logic is code, the data is the existing **path→toolchain map**. The coordinator-edit
**policy** is **fixed shipit behavior, not a consumer knob**: the `edit` operation is
**blocking** when `role == coordinator` AND the path is in the toolchain map (implementation
it should delegate) AND no **break-glass** marker is present.

## Considered options

- **Adopt a middleware/guardrail framework.** Rejected: imperative and heavyweight, it
  reinvents what Claude Code already ships declaratively, and the enforcement lives on the
  agent/PR-loop path (off the required-check path) where a thin native hook is the right
  weight.
- **Make the policy consumer-configurable.** Rejected: standardization *is* the value — a
  consumer no more redefines "may the coordinator implement?" than it redefines what `lint`
  means (`architecture.lex` §7). Fixed in the binary, zero new config.
- **A declarative tool-wrap DSL in `.shipit.toml`.** Rejected: a config language that
  compiles to hooks is the drift-engine subsystem `lessons-learned` warns against. Keep logic
  in the binary; the only data read is the toolchain map, already present.

## Consequences

- Enforcement rides the auto-updating agent/PR-loop surface (`architecture.lex` §2): a policy
  fix lands fleet-wide with no per-repo file churn, and stays off the required-check path.
- This **binds Claude Code as the harness substrate** — the native hook contract
  (`agent_type`, `permissionDecision`, the stdin/stdout JSON shape). A different agent runtime
  would need its own thin wiring; the binary logic ports, the hook config does not (the
  research's "script logic ports, wiring does not" finding). Pin/track the hook contract
  against the CLI version it was verified on (2.1.195).
- The committed hook LINE (the managed `.claude/settings.json` command) is part of the
  enforcement, not just plumbing: a hook that cannot actually run must never be
  indistinguishable from one that ran and allowed. ADR-0038 amends the `PreToolUse` entry's
  outer shell wrapper to fail CLOSED on a resolution failure (`pixi`/`./bin/shipit`
  unresolvable) after a #505 regression made it fail open and silent (#529); the pure Python
  guard logic this ADR describes is unaffected.
- HAR01 adds essentially no `.shipit.toml` schema; per-consumer *role additions* (a custom
  role) are a deliberately deferred, separate concern — the closed registry ships fixed.
