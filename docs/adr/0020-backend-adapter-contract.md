# A uniform backend-adapter contract for spawned Runs

> **Status: Proposed (stub).** This ADR fixes the *shape* of the multi-backend seam and
> the invariants every backend must honour, but deliberately leaves the per-backend
> **launch specifics** (exact argv, role/instruction injection, auth-env scrub, read-only
> posture) as **load-bearing unknowns for a WS00 spike to settle** — the same discipline
> ADR-0019 used for the `claude` backend (real probe → recorded findings → torn down).
> Until the spike fills §Decision-per-backend, only `claude` is wired.
>
> **Extends ADR-0019** (the `claude`-only launch contract) and **refines ADR-0017** (the
> `--backend claude|codex|antigravity` surface it named but left at one backend).

`shipit spawn subagent --backend codex|antigravity` launches a **non-Claude** agent as a
child process **rooted in the Tree**, through the *same verb, Tree substrate, and PR result
channel* as the `claude` backend. The launcher stops being claude-shaped: `spawn/launch.py`'s
`build_command` / `child_env` (today hardcoded to `claude -p --agent <role>` and an
`ANTHROPIC_API_KEY` scrub) become a small **`BackendAdapter`** seam — one adapter per backend
— selected from `--backend`. The cross-backend **invariants** (cwd-rooting, PR-only result,
fail-closed, read-only-Tree enforcement) are fixed here; the **per-backend specifics** are an
adapter's private business, discovered by spike.

## Context

ADR-0017 named the surface `shipit spawn subagent ... [--backend claude|codex|antigravity]`
but shipped only `claude` (`SUPPORTED_BACKENDS = ("claude",)`). ADR-0019 then pinned the
`claude` launch contract *verbatim* — and two of its three load-bearing requirements are
**claude-specific harness couplings**, not universal truths:

- **`--agent <role>` populates `agent_type`** so the shipit `PreToolUse` coordinator-guard
  allows the Run's own edits. codex / antigravity **do not run under the shipit harness or
  its guard at all** — they are foreign agent runtimes. So "convey the role" generalizes,
  but "convey it *as `agent_type` for our guard*" does not.
- **Scrub `ANTHROPIC_API_KEY`** so the keychain OAuth login wins. Each backend has its *own*
  auth-env hazard (e.g. a stale key shadowing a logged-in session); the *principle*
  ("scrub the env vars that would break this backend's preferred login") generalizes, the
  *specific var* does not.

Only the **structural** facts are universal: a foreground subprocess with **`cwd` = the
Tree**, **stdin from `/dev/null`**, results delivered **through the PR** (parent reads
start + exit, never scrapes stdout for the deliverable), and **fail-closed** Tree creation.
That split — universal structure vs. per-backend specifics — is exactly what an adapter seam
captures, and #153's third acceptance criterion ("a uniform backend-adapter contract across
claude / codex / antigravity") asks for.

The prime use #153 names is **multi-agent PR review**: codex / antigravity reviewers posting
on the **shared read-only Trees** ADR-0018 builds (TRE03-WS03). That intersects an existing
subsystem — shipit already funnels `codex` / `agy` reviews via check-runs (ADR-0005/0006,
run today through the `-e review` pixi env). **Reconciling the spawn-based reviewer path with
the existing review funnel is the one open design fork** (see §Open decisions); this ADR does
not pre-decide it.

## Decision (the fixed shape)

1. **`BackendAdapter` seam.** Introduce a per-backend adapter (a small `Protocol` / ABC in
   `spawn/launch.py` or a new `spawn/backends/`) exposing exactly what varies:
   - `build_command(task, role, *, tools) -> list[str]` — the backend's headless argv.
   - `child_env(parent_env) -> dict[str, str]` — the backend's auth-env transform.
   - `read_only_posture` — how a **reviewer** Run is constrained beyond the chmod'd Tree
     (a tool allow-list if the backend has one; otherwise the FS chmod is the sole guard).

   The **shared, backend-agnostic** pieces stay shared and are *not* duplicated per adapter:
   `launch()` (runs `cmd`/`cwd`/`env`), `write_task()` / `reviewer_task()` (English PR-contract
   prompts), the Tree creation path, and PR resolution. `claude` becomes the first adapter
   with **zero behaviour change** (a pure refactor; the full existing suite stays green).

2. **`--backend` selects the adapter.** `SUPPORTED_BACKENDS` widens to
   `("claude", "codex", "antigravity")` as each adapter lands; an unknown backend fails loud
   at the verb boundary (no silent default to claude).

3. **Invariants every adapter MUST honour** (the contract a reviewer holds adapters to):
   - **Rooted in the Tree.** Subprocess `cwd` = the Tree, never a `cd`. (ADR-0019 §1.)
   - **PR is the only result channel.** The parent learns start + exit; it never parses the
     backend's stdout for the deliverable. *This is what makes wildly different output formats
     plug in uniformly* — the lifecycle handle is the exit code, full stop.
   - **Fail-closed.** Tree-creation error → loud nonzero exit, never a native-worktree
     fallback. (ADR-0017.)
   - **Read-only reviewers are enforced at the filesystem layer** by the chmod'd shared
     read-only Tree (ADR-0018) — that is the load-bearing guarantee and is backend-agnostic.
     A backend's native tool/sandbox restriction is *defense-in-depth on top*, best-effort.
   - **Auth hygiene.** The adapter scrubs the env vars that would shadow its backend's
     preferred login (the generalization of ADR-0019 §3), and **never** writes a secret to
     disk in the Tree.

## Decision-per-backend — STUB, to be filled by the WS00 spike

For each of `codex` and `antigravity`, the spike runs a real headless probe in a scratch Tree
(then in the shipit repo) and records, ADR-0019-style:

- **Invocation.** The exact non-interactive/headless argv: how the task prompt is passed,
  whether a `-p`-equivalent exists, the output/result mode.
- **Role / instruction conveyance.** How the role's system prompt is injected (native
  `--system-prompt` / agent-def file / config) — *without* relying on the shipit guard, which
  this backend does not run.
- **Auth.** Which env vars to scrub, and how the backend authenticates (key vs. OAuth/login).
- **Read-only posture.** Whether the backend can express a tool allow-list / sandbox for a
  reviewer Run, or whether read-only rides solely on the chmod'd Tree.
- **Lifecycle quirks.** TTY/stdin needs, exit-code semantics, any background mode (not adopted
  unless foreground pooling bottlenecks — cf. ADR-0019 §7).

> ⚠️ Do not implement `codex` / `antigravity` adapters from this stub by guessing the CLI
> flags — that is precisely the kind of paper decision ADR-0019's spike caught out. Run the
> probe, fill this section, *then* wire the adapter.

## Open decisions (resolve in WS00, before WS04)

- **Reviewer path reconciliation.** Does a spawn-based `--backend codex --role reviewer`
  **replace** the existing `-e review` check-runs funnel (ADR-0005/0006) for codex/agy, run
  **alongside** it, or feed the *same* readiness gate from a new producer? This is the highest
  -leverage fork — settle it before WS04 builds the reviewer Run, so the two review mechanisms
  don't drift. (`agy` vs `antigravity`: confirm whether they are the same backend under two
  names or two distinct adapters.)

## Considered options

- **One adapter per backend (chosen).** Smallest thing that captures the universal/specific
  split; the shared launch/prompt/Tree code is written once.
- **Backend-specific `spawn` subcommands** (`spawn codex` / `spawn antigravity`). Rejected:
  forks the verb surface and duplicates the Tree/PR plumbing ADR-0017 deliberately unified.
- **A single mega-`build_command` with per-backend `if` branches.** Rejected: the claude
  contract is already dense; branching it three ways buries each backend's contract and makes
  the read-only/auth invariants impossible to assert per backend.

## Consequences

- WS01 refactors `spawn/launch.py` to the adapter seam with `claude` as adapter #0 and the
  suite green — a behaviour-preserving move that de-risks everything after it.
- WS02/WS03 wire `codex` / `antigravity` adapters **from the spike's recorded findings**, not
  from guessed flags.
- WS04 delivers the non-Claude **reviewer** Run on a shared read-only Tree (#153 acceptance
  #2), against whichever reconciliation §Open-decisions picks.
- The read-only guarantee for foreign backends rests on the **chmod'd Tree** (ADR-0018), so a
  backend that cannot express a tool allow-list is still safe to run as a reviewer.

This **extends ADR-0019** (generalizes its claude-only contract) and **does not amend** the
Tree substrate (ADR-0014/0015/0018) or the readiness/review model (ADR-0005/0006) — the
reviewer-path reconciliation above is flagged for an explicit decision, not silently changed.
