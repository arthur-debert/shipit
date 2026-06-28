# Role prompts are generated role-scoped reductions, delivered via binding surfaces

A **role** (`coordinator`, `implementer`, `shepherd`, `explorer`) must reliably behave as
itself for a whole session. Two observed failures rule out the obvious approaches: agents
*read* AGENTS.md but lose it as context builds (so an ambient doc is not where binding
behavior can live), and an agent that sees *every* role's instructions drifts mid-session
into acting as another role. So role behavior must arrive **reduced to the one role** and on
a surface that **sticks**.

**Decision.** Role behavior has a single source — focused **lex** fragments: a shared
dev-cycle *base* plus one *overlay* per role — composed via lex includes. From that source we
**generate** a per-role **role prompt** that is `base + that role's overlay only` (the
`coordinator`'s also carries the *map* of the roles it delegates to; it is the one broad
slice). Each role prompt is delivered on a **binding surface**: a subagent role's prompt is
its agent-def body (`.claude/agents/<role>.md`); the `coordinator`, which is the top-level
session and has no agent-def, receives its prompt as injected context plus the PreToolUse
**deny** reason. AGENTS.md is generated from the *same* source but is **non-binding
reference** — at most it carries the role *map*, never a role's detailed marching orders.

This is ADR-0004's pattern (one source → generated files; thin surfaces) applied to prompts,
and the generated files reconcile by hash like every other managed file
(`architecture.lex` §2).

## Considered options

- **Hand-author each role prompt independently.** Rejected: the dev cycle would be stated
  four-plus times and drift — the exact `lessons-learned` root cause (weak-review drift).
- **Deliver role behavior through AGENTS.md.** Rejected on evidence: ambient docs are
  read-then-lost, and the all-roles union re-exposes the drift this decision removes.
- **A new structured (non-lex) role-definition format.** Rejected: lex includes already give
  a sliceable base+overlay source, so a new format is reinvention.

## Consequences

- Per-role *reduction* requires a *sliceable* source; prose AGENTS.lex cannot be sliced, so
  the source is the focused fragments and AGENTS.md becomes one of their generated outputs,
  not the source.
- The `coordinator` is special twice over: the broad slice, and the only role with no
  agent-def — its prompt rides injected context + the deny reason, not a file body.
- Whether AGENTS.md should carry the role *map* at all is a **tunable knob**, not settled
  here: HAR02's session metrics (role-drift incidents, adherence) decide it empirically.
  Because AGENTS.md is non-binding, its role content can be added or removed with no behavior
  risk.
