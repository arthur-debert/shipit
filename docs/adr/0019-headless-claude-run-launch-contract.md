# The headless-Claude Run launch contract (TRE03 spike, #161)

`shipit spawn subagent` launches a backend agent as a **child process rooted in the
Tree**. For the `claude` backend, that child is the **`claude` CLI in headless print
mode** — `claude -p "<task>" --agent <role> --permission-mode bypassPermissions --output-format json`
run as a subprocess with **`cwd=<Tree>`**, stdin from `/dev/null`,
and **`ANTHROPIC_API_KEY` scrubbed from the child env**. The role is conveyed to the
harness via the native **`--agent <role>`** flag — not a custom marker — and the parent
learns start/finish from the **process exit + the JSON result envelope**, never by
scraping content (results land in the PR). This refines ADR-0017's "child process rooted
in the Tree" into an exact, implementable contract and is the gate for WS01 (#155).

## Context

ADR-0017 decided *that* shipit owns spawning and launches the backend as a child process
with `cwd = <Tree>`, results reported through the PR. It deliberately left the **launch
mechanism** unspecified — the one load-bearing unknown WS00 (#161) had to settle before
WS01 could wire the verb, the same shape as the #139 `WorktreeCreate` feasibility spike
(throwaway probe → recorded decision → torn down).

The spike ran a real probe (`claude` CLI **2.1.196**): a headless child launched in a
scratch Tree, then in the shipit repo itself, with the coordinator-guard
(`.claude/settings.json` PreToolUse) live. It produced two **surprising, load-bearing**
findings that a paper decision would have missed — exactly the spike's value.

## Decision

The `claude`-backend launch contract WS01 implements verbatim:

1. **Invocation.** Subprocess of the `claude` CLI in print mode —
   `claude -p "<task prompt>" --agent <role> --permission-mode bypassPermissions [--tools "<allowlist>"] --output-format json`
   — with the subprocess **`cwd` set to the
   Tree** and **stdin redirected from `/dev/null`** (a TTY-less child otherwise waits ~3 s
   for stdin and warns). No `--cwd` flag exists; rooting is the OS process cwd — which the
   probe confirmed lands writes in the Tree with **no leak to the parent checkout**, and
   sidesteps the bash-cwd-reset footgun (a subagent's bash resets to the parent repo per
   call) because the process itself is rooted, not a `cd`.

2. **Role conveyance — the load-bearing finding.** A headless `claude -p` child is a
   **fresh top-level session**, not a Task-tree subagent, so its PreToolUse payload carries
   **no `agent_type`** — which `resolve_role` maps to **`coordinator`**. The probe
   confirmed this end-to-end: a bare headless child's `Write` to a `src/` path was
   **denied** by the coordinator-guard ("you never implement"). The fix is the **native
   `--agent <role>` flag**: it both injects the role's system prompt *and* populates the
   hook payload's `agent_type`, so `resolve_role` returns the real role and the guard
   allows a spawned implementer's edits. Probe-confirmed: `--agent implementer` → the same
   `src/` `Write` **succeeded**. **No change to `resolve_role` is needed** — the launcher
   simply passes `--agent <role>`, where `<role>` is a registry role with a committed
   `.claude/agents/<role>.md` def carried by the Tree clone. (Reviewer has no def yet —
   WS03 adds `.claude/agents/reviewer.md`.)

3. **Auth — the second load-bearing finding.** The child inherits the parent's
   credentials, **but** a stale/invalid `ANTHROPIC_API_KEY` in the env **takes precedence
   over the claude.ai OAuth/keychain login** and breaks auth ("Invalid API key"). The
   probe's first run failed for exactly this reason; the same launch with `ANTHROPIC_API_KEY`
   scrubbed succeeded against the keychain login. **The launcher MUST remove
   `ANTHROPIC_API_KEY` from the child env** (so the keychain OAuth login is used), unless a
   known-valid key is being passed deliberately. This is a hard contract requirement, not a
   nicety.

4. **Permissions.** `--permission-mode bypassPermissions` for write Runs (implementer/
   shepherd); the **coordinator-guard still fires** in the child (harness loads — see 5),
   so "bypass" is bounded by the same policy the dev loop runs under. Tool access is
   narrowed per role with `--tools "<allowlist>"` (a reviewer gets read-only tools, no
   `Write`).

5. **Harness fidelity.** The child loads the Tree's project config — `.claude/settings.json`
   (hooks fire — probe-confirmed), `CLAUDE.md`, `.claude/agents/`, skills — i.e. it runs
   **under the full shipit harness**, not a bare claude. `--append-system-prompt` is
   available to layer extra Run-specific context onto the role prompt. (`--bare`/`--safe-mode`
   would strip the harness — explicitly NOT used.)

6. **Lifecycle.** `-p` is a **blocking foreground subprocess**: start = the spawn returns a
   pid; finish = process exit + a single JSON envelope on stdout
   (`session_id`, `subtype`, `is_error`, `duration_ms`, `total_cost_usd`). Because results
   arrive **through the PR**, the parent needs only the **start + exit** signal — it never
   parses the envelope's content for the deliverable. The envelope is the minimal lifecycle
   handle ADR-0017 asked for.

7. **Concurrency.** A wave of WS implementers = **N parallel `claude -p` subprocesses**;
   the launcher owns the pool/backpressure (a bounded worker count). A detached model
   (`claude --background` + `claude agents --json`) exists but is **not** adopted:
   foreground-wait is simpler and sufficient when results land in PRs.

**Fail-closed (unchanged from ADR-0017).** If Tree creation errors, the spawn fails loud —
never a silent fallback to a native worktree.

## Considered options

- **Claude Agent SDK (programmatic).** Heavier dependency surface for no gain over the CLI,
  which already exposes cwd-rooting, role via `--agent`, permission modes, and a JSON
  lifecycle envelope. Rejected for the claude backend; the CLI is the clean supported path.
- **A custom role marker (env var) read by `resolve_role`.** Would work, but `--agent`
  already populates `agent_type` natively and needs **zero** harness code change. Rejected
  as redundant.
- **`--background` / `claude agents`.** Detached lifecycle management we don't need when the
  PR is the result channel. Deferred (it is the natural escape hatch if foreground pooling
  ever bottlenecks).

## Consequences

- WS01 implements the launcher against this exact contract without re-deciding. The two
  non-obvious requirements it MUST encode: **`--agent <role>`** (else the guard denies the
  Run's own edits) and **scrub `ANTHROPIC_API_KEY`** (else auth fails).
- WS03 (reviewer Run) must add `.claude/agents/reviewer.md` so `--agent reviewer` resolves;
  `resolve_role` already maps an unknown non-empty `agent_type` to a non-coordinator worker,
  but a committed def is needed for the role prompt and clean resolution.
- WS04 (the demoted `WorktreeCreate` adapter) is unaffected by this contract — it covers the
  *in-CC* Agent-tool path, which already carries `agent_type` natively; this ADR governs the
  *out-of-CC* `claude -p` child only.
- The probe was fully torn down (no residue in the repo, settings, or filesystem), matching
  the #139 spike discipline.

This **refines ADR-0017** (the launch mechanism it left open) and **does not amend** the
role model of ADR-0011/0012 — the spike confirmed `--agent` makes the existing
`resolve_role` rule reach headless children unchanged.
