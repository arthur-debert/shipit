# The `PreToolUse` guard splits into two entries; only the edit entry fails CLOSED

> **Status: Accepted.** Fixes #1182. **Scoped amendment to ADR-0038** (the
> `PreToolUse` wrapper fails closed): ADR-0038's never-silent-allow invariant is
> retained verbatim for the **edit** entry, whose command is unchanged byte for
> byte. A **second** managed `PreToolUse` entry is added for the tool families
> ADR-0038 never covered, and that entry is **advisory on failure** — it exits 0.
> Also amends ADR-0012's tool-name registry (the codex adapter's shell tool name)
> and instantiates ADR-0014 and ADR-0047 by making their rules reachable.

The managed matcher was `Edit|Write|MultiEdit|NotebookEdit`, so on the Claude
path `Bash` and `Agent` calls never reached the guard at all. Two rules written
to enforce ADR-0014 — `git worktree add` and `EnterWorktree` — were therefore
dead code, and the mandatory-isolation rule ADR-0047's registry makes possible
had nowhere to run. Widening the matcher raises the question ADR-0038 answered
for edits: what happens when the guard cannot run?

## Decision

The managed `PreToolUse` entry becomes **two** entries, and Claude Code's own
matcher does the discrimination:

| matcher | command | on `rc != 0` |
| --- | --- | --- |
| `Edit\|Write\|MultiEdit\|NotebookEdit` | `shipit hook pretooluse` | `exit 2` — refuse (ADR-0038, verbatim) |
| `Bash\|Agent` | `shipit hook bashguard` | `exit 0` — allow, with a message on stderr |

Both run the same total decider; `bashguard` exists as a second name only
because the install marker (`SETTINGS_HOOK_MARKER`) is matched as a **substring
of the command**, so a second command containing `shipit hook pretooluse` would
be stripped by the first unit and reconcile would flap forever.

### Why the asymmetry is not a weakening of ADR-0038

ADR-0038 is right for the case it was written for (#529: a silent fail-open let
an entire epic run unguarded). The severities are not symmetric.

- Fail-closed on **edits** protects the one thing the guard exists for. Its cost
  is "you cannot edit", which is recoverable from inside the session.
- Fail-closed on **Bash** would protect ADR-0014 hygiene — a stray `git worktree
  add`, recoverable, no data loss — and its cost is that *the session cannot run
  any shell command, including the ones that repair pixi.* That is not
  recoverable from inside the session; a human must fix it from outside.
- The trigger is realistic, not hypothetical: ADR-0038's own deliberately-open
  point is that a pixi-less consumer's guard never runs and only ever refuses.
- Decisive: **`Bash` is 100% unchecked today.** Fail-open for `Bash` is never
  worse than the status quo and never touches the edit guard, while fail-closed
  for `Bash` would invent a bricking mode that has never existed.

`Agent` rides with `Bash` on the same principle: new coverage must never regress
availability.

## What the second entry enforces

1. **ADR-0014**: `git worktree add` and `EnterWorktree` are refused. Trees are
   dissociated clones, not git worktrees.
2. **Mandatory isolation (ADR-0047)**: an `Agent` spawn whose `subagent_type`
   resolves to a Role whose profile is `tree_backed` is refused when
   `isolation` is absent from `tool_input`. The rule reads the `roleprofile`
   registry, so `explorer` (`AmbientWorkingDir` — no Tree, ever) passes by
   construction and needs no exception.

The isolation rule covers shipit's five roles only. `subagent_type` is
frequently *not* a shipit role — `general-purpose`, `claude`, `Explore`,
`Plan`, `fork` — and those resolve to no profile and must be ALLOWED. `fork`
in particular can never be denied: it inherits the parent's context and cannot
be isolated.

## Rejected alternatives

**One entry with no matcher, sniffing the payload in the wrapper to pick an exit
code.** Rejected twice over. It puts tool-name policy into shell JSON matching,
which ADR-0012 forbids ("all rich logic in the versioned binary"), and a
substring match on the raw payload is **content-spoofable in both directions**:
a Bash heredoc containing the literal text `"tool_name":"Edit"` would fail
closed, and an Edit payload whose file content contains `"tool_name":"Bash"`
would fail OPEN — silently disabling the coordinator guard, which is exactly
the #529 failure ADR-0038 exists to prevent. Matcher dispatch keeps each
wrapper's exit code a **constant**.

**A shell-side pre-filter to cut latency.** Measured cost is ~0.27–0.30s warm per
invocation, and `./bin/shipit` directly is no faster than the `pixi run` wrap, so
there is no cheap win — and a pre-filter would put policy back in shell.
Accepted as-is.

**Fixing ADR-0038's pixi-less open point first.** Still open, still a larger
separate workstream; this ADR does not resolve it.

## The codex adapter

The rules were dead on the codex path too, for a **different** reason, and the
fix is in the binary rather than the wrapper.

shipit's managed codex entry (`codex-hooks-pretooluse.json`) carries **no
matcher**, so it fires on every tool call and the rules are already *reachable*
there. But `_matches_git_worktree_add` required the tool to be named `bash`, and
codex's shell tool is named **`exec_command`**, putting its command under
`tool_input.cmd` rather than `tool_input.command` (both observed on codex-cli
0.146.0, from codex's own `codex.tool_decision` / `codex.tool_result` records).
So the tool-name registry gains a `_SHELL_TOOLS` set — the same shape
`_EDIT_TOOLS` already uses to carry codex's `apply_patch` alongside Claude
Code's `Edit` — and the payload projection reads either command key.

The codex entry keeps **no matcher and `exit 2`**, unchanged. Splitting it the
same way would mean introducing a matcher on the entry that guards codex edits,
and codex only honours a project `hooks.json` whose `trusted_hash` was granted
interactively in its TUI — which cannot be minted non-interactively, so codex
matcher behaviour could not be verified here. A wrong matcher on that entry
would silently disable the codex coordinator edit guard: the #529 failure again.
Leaving it matcherless keeps it a total decider, which is strictly safer, and it
already gains the real fix above. The codex path therefore still carries the
fail-closed-on-shell exposure this ADR rejects for Claude Code; splitting it
once codex matcher semantics can be verified is the follow-up.

## Consequences

- ADR-0014's two deny rules and ADR-0047's isolation requirement are reachable
  and enforced on the Claude path for the first time.
- The edit guard's contract is untouched: same matcher, same command, same
  `exit 2`, same sha.
- Every Bash and Agent call now pays one guard invocation (~0.3s warm).
- `payload["cwd"]` is read and threaded into the deny path, so a rule that needs
  to compare the caller's checkout against a path has the field available.
- Two managed units now share one event on one file, distinguished by `key` and
  `marker`. Neither marker may contain the other, or one unit will strip the
  other's entry on every install — asserted by the test suite.
