# shipit owns subagent spawning; Trees are the Run substrate

> **Refined by ADR-0019.** This ADR fixed *that* shipit launches the backend as a child
> process rooted in the Tree but left the **launch mechanism** open; ADR-0019 settles it for
> the `claude` backend (headless `claude -p --agent <role>`, `ANTHROPIC_API_KEY` scrubbed).
> See also ADR-0018 (write vs read-only Trees).

The coordinator launches every real **Run** through a shipit CLI —
`shipit spawn subagent --repo R --epic E --ws N --role ROLE [--backend claude|codex|antigravity]`
— which creates the **Tree**, launches the agent as a child process **rooted in that
Tree** (cwd = the Tree), and lets the Run report back **through the PR** (a draft PR for
a writer, a posted review for a reviewer). Claude Code's in-session `Agent` tool + the
`WorktreeCreate` hook are **demoted** to a convenience adapter for throwaway in-CC Claude
helpers; native `git worktree` / `EnterWorktree` stays denied (WS06 / ADR-0014).

## Context

TRE01/TRE02 shipped `shipit tree` as a primitive the coordinator drives by hand
(ADR-0014, ADR-0015). Issue **#139** then surfaced an enforcement gap: an
`Agent(isolation: "worktree")` call mints a native `.claude/worktrees` worktree,
bypassing the WS06 deny-guard — agent isolation was never actually routed through Trees.

A feasibility spike recorded on #139 **confirmed** that Claude Code's `WorktreeCreate`
hook is documented and stable, and that the harness adopts **any path the hook returns**
(a dissociated clone) as the subagent's cwd — no validation, no footgun. That removed the
last unknown and reframed #139 from "patch a hole" to "decide who owns spawning."

The deeper pull is architectural: **push everything regular enough down to the tool/CLI
layer, and leave the LLM only genuine-judgment work.** Determinism at the tool level buys
consistency, speed, and lower execution cost — the same reason the PR engine (`prstate`)
and the policy hooks (`harness/policy.py`) are code, not prompt. Spawning a Run — pick the
base, create the Tree, root the agent in it, wire the result back to a PR — is regular. It
belongs in code.

## Decision

shipit owns subagent spawning. The coordinator never points an Agent tool at a worktree;
it calls `shipit spawn subagent` and passes **intent as arguments** — `--repo`, `--epic`,
`--ws`, `--role`, `--backend`. Because intent arrives explicitly, shipit never has to
*infer* it, which dissolves the per-spawn handshake race (below) and frees the launcher to
start **non-Claude backends** (codex, antigravity) behind the same verb.

The verb:

1. resolves the base ref and plans the **Tree** (`tree/layout.py`, ADR-0014/0016);
2. creates the Tree (`tree/create.py`) — a **write Tree** for a writer, a **read-only
   Tree** for a reviewer (ADR-0018);
3. launches the backend agent as a **child process whose cwd is the Tree**, so there is
   no bash-cwd footgun (a subagent's bash defaults to the *parent* repo and resets per
   call — pointing the Agent tool at an external path can't fix that);
4. the Run reports back **through the PR** — a writer opens a draft PR, a reviewer posts a
   review — matching shipit's existing PR-driven model. The coordinator orchestrates with
   `shipit` + `shipit pr status`, never by scraping a child's stdout.

**Fail-closed.** If Tree creation errors, the spawn **fails loud**. There is never a
silent fallback to a native worktree.

## Considered options

- **Transparent-hook-only** (let the `WorktreeCreate` hook build the Tree). Rejected: the
  hook can learn a *session-stable epic marker*, but **not** the per-spawn WS/role — the
  coordinator cannot predict the `agent-<id>` the hook will receive before the spawn
  returns, so the hook can't build the semantic path. It is also **Claude-only**: a hook
  is a Claude Code harness contract, useless for a codex or antigravity backend.
- **Coordinator points the `Agent` tool at an external Tree path.** Rejected: there is no
  clean cwd parameter to do it, and it carries the **bash-cwd footgun** — the subagent's
  bash defaults to the parent repo and resets every call, so writes land silently in the
  wrong repo.
- **Keep #139 as a small hook patch.** Rejected: it leaves isolation Claude-only and
  leaves spawning as undeterministic LLM work. The judgment above (push the regular work
  into code) says spawning is a subsystem, not a prompt.

## Consequences

- #139 grows from "a small hook" into a **real subsystem**: backend adapters
  (claude / codex / antigravity), a Run lifecycle, and result capture via the PR. That is
  the intended scope of **Trees v2 / shipit-owned subagent spawning**.
- The in-session `Agent` tool + `WorktreeCreate` hook survive only as a **convenience
  adapter** for throwaway in-CC Claude helpers: it knows only the session-stable epic
  marker, so it builds `<epic>/agent-<id>` and is Claude-only. Anything that needs a real
  branch-pinned Run, a non-Claude backend, or a PR-reported result goes through
  `shipit spawn subagent`.
- Native `git worktree` / `EnterWorktree` stays **denied** (WS06, ADR-0014); this ADR adds
  the *positive* path the deny message points at, closing the #139 gap by construction
  (the supported route never mints a native worktree).
- Provisioning stays cheap (~1.5s via the pixi global cache; worst rust consumer trivial
  once sccache is warm), so no warm-template is needed here — ADR-0015's template remains
  the deferred escape hatch, and **sccache is now load-bearing** for per-spawn Trees, not
  merely a nicety.

This **amends ADR-0014**: enforcement is no longer deny-only — it is now *deny the native
path* **and** *provide the shipit-owned spawn path*.
