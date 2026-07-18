# A uniform backend-adapter contract for spawned Runs

> **Status: Proposed.** This ADR fixes the *shape* of the multi-backend seam and the
> invariants every backend must honour. The per-backend **launch specifics** (exact argv,
> role/instruction injection, auth-env scrub, read-only posture) were left as load-bearing
> unknowns for a WS00 spike — the same discipline ADR-0019 used for `claude` (real probe →
> recorded findings → torn down). **The WS00 spike (#181) has now run** against the real
> binaries (`codex-cli 0.139.0`, `agy` v1.0.14) and filled §Decision-per-backend below; the
> per-backend **adapter code** still lands in WS02 (`codex`) / WS03 (`antigravity`) from these
> recorded findings. The reviewer-path reconciliation is **DECIDED** (maintainer-ratified at the
> WS00 gate — REPLACE outright; see §Reviewer-path reconciliation). Until the adapters land, only
> `claude` is wired.
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

## Decision-per-backend — recorded by the WS00 spike (#181)

Probed against the real binaries (`codex-cli 0.139.0`, `agy` v1.0.14) in throwaway git repos
(write Runs confirmed by a landed commit; reviewer Runs confirmed against a 2804-line planted-bug
diff). Throwaway probe scripts torn down (all under the scratchpad, never in the repo).

**`agy` ≡ `antigravity` — CONFIRMED.** There is **one** non-Claude review/spawn binary for this
backend: `agy` (the Antigravity CLI, v1.0.14; `src/shipit/review/backends/agy.py` is literally
"the Antigravity (agy) CLI backend"). There is **no** separate `antigravity` binary. The
`--backend antigravity` surface drives the `agy` binary; treat "antigravity" and "agy" as the
same adapter under two names (the user-facing `--backend` value is `antigravity`; the binary it
shells out to is `agy`). WS03 builds **one** adapter.

### The one universal, load-bearing operational finding (both backends)

**stdin MUST be `/dev/null`** (generalizes ADR-0019 §1's claude stdin rule to *every* backend).
Both binaries **hang waiting on stdin** when launched non-interactively with the prompt passed
as an argument and an open (non-TTY, unclosed) stdin inherited from the parent:

- `codex exec "<prompt>"` with an open pipe on stdin blocks on `Reading additional input from
  stdin...` indefinitely (it appends piped stdin as a `<stdin>` block, so it waits for EOF).
- `agy --print "<prompt>"` with inherited stdin blocks and eventually emits the live
  `timed out waiting for response` / blank-output failure.

Redirecting **stdin from `/dev/null`** (or, for codex, piping the prompt on stdin and closing it
at EOF — what `src/shipit/review/backends/codex.py` already does via `input=prompt`) eliminates
the hang for both: probe write/review Runs that hung indefinitely completed in **10–35 s** once
stdin was closed. The adapter's `launch()` MUST set the child's stdin to `/dev/null` (the shared
launch path already owns this for `claude`; the seam keeps it shared). **This finding directly
explains the funnel's intermittent agy failure — see §Open decisions.**

### codex (`codex-cli` 0.139.0)

- **Invocation — write Run.** `codex exec --skip-git-repo-check
  --dangerously-bypass-approvals-and-sandbox --model <id> "<task prompt>"`, subprocess `cwd` =
  the Tree, stdin `/dev/null`. The task prompt is the first positional arg (or `-`/stdin). The
  **sandbox choice is load-bearing**: codex's `--sandbox` has three modes — `read-only`,
  `workspace-write`, `danger-full-access`. Probed directly: under `workspace-write` codex can
  edit files **but cannot commit** — it is denied `.git/index.lock` (`Operation not permitted`)
  and has **no network** (so `git push` / `gh` would also fail). A write Run that must
  **commit + push + open a draft PR via `gh`** therefore needs the unsandboxed posture:
  `--dangerously-bypass-approvals-and-sandbox` (the flag codex documents for *"environments that
  are externally sandboxed"* — our chmod'd Tree **is** that external sandbox) or equivalently
  `--sandbox danger-full-access`. Probe-confirmed: with the bypass flag, a file edit followed by
  `git add` and `git commit` landed a real commit. Result mode: foreground, final message on stdout; optional
  `--json` (JSONL events), `--output-schema <f>` (native JSON shape), `-o/--output-last-message
  <f>` (capture final message to a file). The parent reads only start + exit (PR is the channel).
- **Invocation — reviewer Run (network-capable sandbox on a read-only Tree).** `codex exec
  --skip-git-repo-check --ephemeral --sandbox workspace-write -c
  sandbox_workspace_write.network_access=true --model <id> "<reviewer task>"`, `cwd` = the
  shared read-only Tree, stdin `/dev/null`. The ADR's earlier `--sandbox read-only` reviewer
  guess was falsified by WS04a: it blocked the network a self-posting reviewer needs, so the
  chmod'd Tree (ADR-0018) is the load-bearing filesystem read-only guard and codex's sandbox is
  the least-privilege posture that still allows network. Probe-confirmed against the 2804-line
  diff: codex **lazily walked the code** (ran `git diff` / grep itself) and returned rich,
  code-located findings (absolute path + line range) for **both** planted bugs in ~26 s. (The
  capture path also adds `--output-schema` before `--model`; keep that native structured-output
  behavior.)
- **Role / instruction conveyance.** **No** native `--system-prompt`/`--agent` flag exists. The
  role's system prompt is conveyed by **prepending it to the task prompt** (the shared
  `write_task()` / `reviewer_task()` text). codex *also* honours an `AGENTS.md` project-memory
  file and `-c experimental_instructions_file=…`, but writing either into the Tree would pollute
  the PR, so **prompt-prepend is the chosen mechanism** (the funnel already proves prompt-only
  conveyance works).
- **Auth.** Authenticates via **ChatGPT OAuth** (tokens in `~/.codex/auth.json`, dir overridable
  via `CODEX_HOME`), inherited by the child. Probe: a **bogus `OPENAI_API_KEY` in the env did
  NOT break** `codex exec` on this box — codex 0.139 preferred the stored ChatGPT tokens. Still,
  the safe generalization of ADR-0019 §3 holds: the adapter **scrubs `OPENAI_API_KEY` (and
  `CODEX_API_KEY`)** from the child env so the login wins. There is no adapter-level override
  that preserves these API-billing keys; operators who deliberately want API-key auth must choose
  a separate, explicit launch path rather than inheriting it accidentally through `child_env`.
  **Never** write the key/token into the Tree; auth stays in `CODEX_HOME`.

  > **As probed (CDX01-WS03, `codex-cli` 0.139.0).** The invocation audit passed unchanged —
  > every flag the adapter emits (`exec`, `--skip-git-repo-check`,
  > `--dangerously-bypass-approvals-and-sandbox`, `--ephemeral`, `--sandbox workspace-write`,
  > `-c sandbox_workspace_write.network_access=true`, `--output-schema`, `--model`) is still
  > present in the live `codex exec --help`. The auth surface got a probed refinement:
  > `CODEX_API_KEY` is codex's documented opt-in for **API-billed** `codex exec`, and
  > `CODEX_ACCESS_TOKEN` is the **trusted-automation conduit** — a ChatGPT *subscription*
  > token, named by `codex login --with-access-token`, that codex consumes **natively from
  > the env with precedence over the stored login** (a bogus value fails loud: `invalid agent
  > identity JWT format` even on `codex login status`). The adapter therefore scrubs the two
  > API-billing keys (subscription login stays first-class, per the bullet above) and
  > deliberately **passes `CODEX_ACCESS_TOKEN` through** so headless automation with no
  > persisted `CODEX_HOME` login can still reach codex on subscription billing. The token
  > rides the child env only — never persisted, never written into managed files.
- **Read-only posture.** codex **has a real native sandbox**, but for reviewers its
  `--sandbox read-only` mode is historical/falsified because it also blocks network. The current
  reviewer contract is `--sandbox workspace-write -c
  sandbox_workspace_write.network_access=true` on the shared chmod'd read-only Tree (ADR-0018).
  The Tree remains the load-bearing write guard; codex's sandbox is defense-in-depth and bounds
  any escape to codex's workspace-write writable roots while preserving reviewer self-posting.
- **Lifecycle quirks.** Foreground/blocking; exit 0 on success. `--ephemeral` skips session
  persistence; `-C/--cd` sets a working root but OS-process `cwd` is the rooting mechanism
  (ADR-0019). **stdin `/dev/null`** mandatory (see universal finding). No background mode adopted.

### antigravity (`agy` v1.0.14)

- **Invocation — write Run.** `agy --new-project --add-dir <Tree> --model="<verbatim name>"
  --print-timeout=<dur> --dangerously-skip-permissions --print "<task prompt>"`, subprocess `cwd`
  = the Tree, stdin `/dev/null`. **Two agy-specific gotchas, both probe-confirmed:**
  1. **agy IGNORES the process `cwd` for its workspace.** A bare `agy --print` rooted at the Tree
     wrote its file into `~/.gemini/antigravity-cli/scratch/…` and reported *"you didn't have an
     active workspace set"*. The agent is rooted in the Tree only by **`--new-project --add-dir
     <Tree>`** (establishes an active project + grants write access to that dir). With those flags
     and `cwd` = Tree, a file edit followed by `git add` and `git commit` landed a real commit in the Tree.
  2. **`--dangerously-skip-permissions`** is agy's bypassPermissions equivalent (auto-approve all
     tool/shell requests); without it a non-interactive `--print` write Run stalls on permission
     prompts.
- **Invocation — reviewer Run (read-only).** Same shape **without** `--dangerously-skip-permissions`
  (a reviewer needs no write/exec approval): `agy --new-project --add-dir <Tree> --model="<name>"
  --print-timeout=<dur> --print "<reviewer task>"`, `cwd` = the read-only Tree, stdin `/dev/null`.
  Probe-confirmed: tree-rooted, told to run `git diff` itself, agy returned valid JSON findings in
  ~10 s.
- **Role / instruction conveyance.** **No** native `--system-prompt`/agent-def flag. Role + task
  are conveyed in the **`--print` instruction text** (or a prompt tempfile the instruction points
  at by absolute path — what `src/shipit/review/backends/agy.py` does today). Prompt-prepend is the
  mechanism.
- **Auth.** Authenticates via the **Antigravity OAuth login** (creds under
  `~/.gemini/antigravity-cli` + `~/.antigravity`), inherited by the child. Probe: a `GEMINI_API_KEY`
  *was* present in the env and agy still used its OAuth login. Safe generalization: the adapter
  **scrubs `GEMINI_API_KEY` / `GOOGLE_API_KEY`** so a stale key can't shadow the login. Never write
  creds into the Tree.
- **Read-only posture.** agy has **no granular tool allow-list / native read-only sandbox** for a
  reviewer (its `--sandbox` flag only "enables terminal restrictions", best-effort). So a reviewer
  Run's read-only guarantee **rides solely on the chmod'd shared read-only Tree (ADR-0018)** — the
  load-bearing guard — with "omit `--dangerously-skip-permissions`" (and optionally `--sandbox`) as
  best-effort defense-in-depth. This is exactly the asymmetry §Decision invariant 4 anticipated.
- **Lifecycle quirks.** `--print` is foreground/blocking with `--print-timeout` (default 5m; pin
  higher for big reviews). Models via `--model="<verbatim name>"`, the verbatim name taken from
  the `agy models` list; a bare
  `pro` silently resolves to Gemini Flash, which in `--print` goes **agentic** (runs shell/build
  instead of answering) — pin a capable non-agentic model (the funnel pins
  `Gemini 3.1 Pro (High)`). **stdin `/dev/null`** is mandatory (see the universal finding — this
  is the agy failure root cause). No background mode adopted.

## Reviewer-path reconciliation — DECIDED at the WS00 gate (maintainer-ratified)

**Reviewer-path reconciliation.** shipit has two ways to get a codex/agy review on a PR: the
existing **`-e review` check-runs funnel** (ADR-0005/0006; front-loads the diff into the prompt)
and the new **spawn-Tree path** (`--backend codex --role reviewer`, drops the backend into a
shared read-only Tree at the correct head and tells it to fetch the scoped diff itself —
`reviewer_task()` in `src/shipit/spawn/launch.py` already does this). `agy` ≡ `antigravity` is
**confirmed** (one adapter — see §Decision-per-backend).

> **As built (TRE05-WS04b).** The REPLACE landed: the funnel's front-loaded `codex` /
> `agy` review backends (`src/shipit/review/backends/{codex,agy}.py`, which pasted a
> pre-computed diff into the prompt and ran the CLI in the consumer's checkout) are
> **retired**. The single funnel producer is now `src/shipit/review/producer.py`
> (`run_tree_review`): it provisions the shared read-only Tree (ADR-0018) on the PR head
> via `create_readonly`, launches the agent through the SAME spawn `BackendAdapter`
> read-only posture the spawn surface uses (`build_command(..., read_only=True)` — one
> definition of "launch codex/agy as a reviewer"), and the agent **fetches the scoped
> diff itself** with `gh pr diff` (the diff is no longer in the prompt). shipit then
> **captures** the agent's structured stdout and posts it AS the bot App identity through
> the EXISTING `post.py` onto the EXISTING `review: <agent>-local` check-run — so the
> readiness engine, the App-identity posting, the `reviewers:` config, dry-run honesty,
> and codex `--output-schema` are all preserved (the migration-cost checklist below).
> codex `--output-schema` rides a new `output_schema_path` argument on the seam's
> `build_command` (codex honours it; claude/agy ignore it — agy carries the schema in
> prose). The `proc.run` stdin fix shipped earlier in the epic. Canary-validated end to
> end on a throwaway PR: both `codex-local` and `agy-local` posted `CHANGES_REQUESTED`
> reviews as their bot App, both `review: *-local` check-runs closed `completed/success`,
> and `shipit pr status` read them as done — the second agent reused the first's Tree.

### Decision: **REPLACE — outright.** The spawn-Tree path is the single reviewer producer; the funnel is retired

The maintainer **ratified REPLACE outright** at the WS00 gate. End-state: the spawn-Tree reviewer
is the one producer feeding the readiness check-run, and the funnel's front-loading is **retired
as WS04's deliverable — not kept as a fallback**. There is **no permanent "alongside" phase**: a
dual path was explicitly rejected as a crutch (problems found midway → revert to the funnel → the
spawn path never gets finished → the funnel eventually breaks → nothing works). The grounds for
replace are **architectural** — the spike falsified the robustness rationale the funnel's symptoms
first suggested (see §"The falsified hypothesis" below), so the decision rests on these:

1. **Tree fidelity at the correct head.** ADR-0018 gives a real shared read-only checkout at the
   PR's true head. The reviewer sees the *whole codebase*, not a context-free diff, and walks it
   lazily — codex tree-rooted returned **richer, code-located findings** (absolute path + line
   range, cross-referenced against unchanged code) than a front-loaded diff can support.
2. **Scoped diff without guessing the base.** The reviewer runs **`gh pr diff`** (which resolves
   the PR's real base/head — epic branch *or* `main`; do **not** assume `main`) inside the Tree,
   instead of the funnel front-loading a pre-computed diff. One reviewer prompt, no schema/diff
   temp-file plumbing.
3. **Uniformity.** One reviewer mechanism across claude/codex/antigravity over one Tree substrate,
   rather than a second, divergent review path.

### The falsified hypothesis (the load-bearing spike result behind the architectural grounds)

The maintainer's leading hypothesis was that **front-loading a large diff** causes agy's
intermittent blank-text / truncated-JSON failures (the `_TIMEOUT_MARKER = "timed out waiting for
response"` guard in `src/shipit/review/backends/base.py`), and that tree-rooting would fix it. The spike
ran the A/B and **the evidence does not support that mechanism**:

- agy front-loaded a **2804-line** diff completed in **35 s** with valid JSON **once stdin was
  `/dev/null`**; a 455-line front-load completed in 16 s. Front-load size up to 2804 lines is
  **not** what breaks agy.
- **Every** agy hang in the spike — front-loaded *and* tree-rooted, Flash *and* Pro — was the
  **inherited-stdin hang** (agy `--print` waiting on an unclosed stdin), cleared the instant stdin
  was `/dev/null`.
- **Root cause located in the funnel itself:** `src/shipit/review/backends/codex.py` runs
  `proc.run(..., input=prompt)` → stdin is piped and **closed** at EOF → codex review is reliable.
  `src/shipit/review/backends/agy.py` runs `proc.run(..., input=None)` → the child **inherits the
  parent's stdin** (`proc.run` in `src/shipit/proc.py` passes `input` straight through and never
  sets `stdin=DEVNULL`). When shipit's own stdin is an open-but-idle pipe (CI, some spawn
  contexts), agy blocks → the exact intermittent blank/timeout failure. This is a **one-line fix**
  (`stdin=subprocess.DEVNULL` in `proc.run` when `input is None`, or `input=""` for agy),
  **independent of the replace decision**. **Ratified to fold into the TRE05 convergence
  workstream** — it ships inside this epic, not as a standalone patch to `main`.

**Implication of the decision:** "replace" stands on Tree-fidelity + scoped-diff + uniformity
(grounds 1–3), **not** on being the only cure for agy reliability — that cure is the cheap stdin
fix above, which is independent of the replace work and lands in TRE05 convergence regardless of
the producer that wins.

### Migration cost — what the funnel does that the spawn path MUST preserve or consciously drop

WS04 cannot "replace" until these funnel responsibilities are accounted for:

- **Check-run integration.** The funnel posts each review as a GitHub **check-run** that the
  readiness engine reads. The spawn-Tree reviewer currently delivers *through the PR* (review
  comment/exit). WS04 MUST wire the spawn reviewer's result back to the **same readiness
  check-run** (a new producer feeding the existing gate), or readiness gating regresses. *(This is
  the real integration work; treat it as WS04's core.)*
- **Readiness gating.** `prstate/reviewers*.py` decides REQUEST / RE-REQUEST / stale-after-push per
  reviewer. The spawn path must feed the same gate so per-reviewer `rerun:` semantics still hold.
- **`reviewers:` config.** The model/timeout/`rerun` map in `.shipit.toml` / `.release-sync.yaml`
  drives the funnel today. The spawn reviewer must read the **same config** (model alias → backend
  model, per-reviewer timeout) so consumers don't re-configure.
- **Native schema enforcement (codex).** The funnel uses `--output-schema` for guaranteed JSON;
  the tree-rooted reviewer relies on prompt-instructed JSON. WS04 should keep `--output-schema` on
  the codex reviewer (cheap, and it is a real robustness win the spike confirmed).
- **Dry-run honesty.** The funnel's dry-run/no-op path (don't bill a model, report what *would*
  run) must survive into the spawn path.

### De-risking — validation, not a parallel production cycle

Confidence in the cutover comes from **real runs on the canary repo, not from running both
producers in production** (the rejected alongside path). WS04 de-risks the outright replace with:

- **(a) Unit tests on the seam inputs.** Thorough tests of the **argv + env construction** per
  backend — `build_command()` (the headless argv incl. the sandbox/permission posture and stdin
  contract) and `child_env()` (the auth-var scrub) — and the role/task prompt assembly. These are
  the cheap, high-value seam inputs; assert them exhaustively.
- **(b) Real-work validation on the canary.** Actually run the spawn codex/agy reviewers across
  **many real scenarios in the shipit repo itself** (the maintainer's canary) before the funnel is
  retired — real PRs, real diffs, real findings — and confirm parity with what the funnel produced.

Only once (a) + (b) hold does WS04 retire the funnel's front-loading. The `proc.run` stdin fix
ships in TRE05 convergence (above) independently. The migration-cost checklist above is still WS04's
required scope — the only thing the ratification drops is any permanent "run both in production"
step.

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

## Amendment (issue #989, superseded by #1033) — AGY 1.1.2 native reviewer agent attempt

The original §antigravity was recorded against **agy v1.0.14**, whose reviewer posture had
**no** native agent-def flag, so the reviewer role rode a prompt-prepended sentence
(`role_prompt`). The installed **agy is now 1.1.2**, which added a documented **`--agent <name>`**
flag. An attempt was made in #989 to use this for the reviewer posture, but it was superseded
by #1033 which reverted to prompt-prepend.

- **Reviewer agent posture (issue #1033).** The reviewer posture uses prompt-prepend to convey its role, exactly like a write Run (:func:`role_prompt`). The prior attempt to use agy's native `--agent reviewer` flag (issue #989) was reverted because it degraded the model's reliability (e.g. going agentic instead of returning JSON).
- **`--new-project --add-dir` retained.** The measured startup/project overhead is only seconds;
  the dominant cost was the planner/tool loop, not Tree/project churn. `--project` reuse and
  `--sandbox` are deliberately NOT adopted here (agy exposes no stable public create/list workflow,
  and the reviewer still needs networked diff acquisition).
- **Prompt self-check dropped.** The agy reviewer prompt's best-effort `shipit review validate`
  temp-file self-check is removed: it cost the reviewer a tool loop for a check the producer already
  guarantees deterministically (the parser + one parse retry + the service salvage). The inline
  JSON schema and the strict single-object instruction stay.
- **Dogfood review models (`.shipit.toml [reviewers]`).** shipit pins its own local reviewers to the
  faster tier: **codex → `gpt-5.6-sol`** (a verbatim id) and **agy → `flash`** (the existing honest
  alias → `Gemini 3.5 Flash (High)`). The `pro` alias is **not** remapped and write-agent defaults
  are untouched — a bare `pro` still resolves to the capable non-agentic `Gemini 3.1 Pro (High)` per
  §antigravity, so the "pin a non-agentic model" caution above still governs the write path and any
  reviewer left on the default.
  > **SUPERSEDED (issue #1006, PR #1032)**: the `agy → flash` half of this pin shipped a dead
  > required reviewer — Flash went agentic in agy's `--print` mode on the live self-fetch path and
  > narrated instead of returning JSON, failing every `agy-local` run for days while the other
  > reviewers masked it. #1032 reverted the pin to `pro` (`Gemini 3.1 Pro (High)`). The rest of
  > this bullet (codex on `gpt-5.6-sol`, the un-remapped `pro` alias, untouched write defaults)
  > still stands.

**Dogfood verification.** A no-post synthetic replay over a generated 5-file, +92/-11 range with
five seeded defects measured the current agy harness on Gemini 3.5 Flash (High) at **40.408s**
(valid first-pass JSON, all five defects) versus a slim native reviewer agent with the diff supplied
and terminal denial at **32.400s** (valid first-pass JSON, all five defects, zero shell commands) —
the slim arm **~19.8% faster**, both in ten planner/model rounds. This confirms 3.5 Flash plus a
focused native reviewer as the useful first landing and that Tree/project churn is not the
multi-minute cause. The Review Lab cross-repo stress case (fixture core-440, phos-editor/core PR
440) could **not** be run afresh here: new external-model execution on local repository content was
blocked by the platform data-export boundary, so this amendment claims **no fresh core-440 AGY
score**; historical evaluation records and fixtures naming `gpt-5.5` or model `pro` are preserved.

## Amendment (issue #1006) — parse-failure diagnosis is evidence-based; no static model blacklist

The #989 `agy → flash` pin shipped a dead required reviewer (see the superseded note above);
**#1032** landed the emergency config revert to `pro`, and **#1033/#1035** reverted the native
`--agent reviewer` posture back to prompt-prepended `--print`. What remained wrong after both was
the **diagnosis**: every parse failure — including a model that narrated prose and never answered
on a 4-file docs diff — was reported as "no parseable JSON … try a faster model or a smaller
diff", sending the operator chasing diff size when size was never the fault.

- **`parse_review_output` now diagnoses from evidence the raw output actually carries.** Only the
  backend's own explicit timeout marker proves a mid-flight cut-off, so only that mode carries the
  size/latency advice. Empty stdout is reported as a silent non-delivery (a killed child, a failed
  login). A COMPLETE JSON object with the wrong `{summary, comments}` envelope (#826) is an
  output-contract fault — the response terminated on its own. Everything else — prose, narration,
  partial or otherwise unparseable JSON — is a conservative "no review verdict" that states what
  the output was and points at the raw, without guessing a cause: a brace, or a review-shaped
  prefix, is not evidence of truncation (narration, command snippets and tool JSON all carry
  braces while delivering no verdict). The diagnosis is an implementation detail behind
  `parse_review_output`; the raw salvage (#76) and the structured `timed_out` flag are unchanged.
- **No static "unusable model" declaration.** A per-backend reviewer-model blacklist was
  considered and rejected: durable review logs show model behaviour is not a stable capability
  fact (`flash` both narrated-and-failed and succeeded with findings across runs; `pro` has also
  failed with empty output), and `Backend` is shared identity, not reviewer-role policy. AGY
  reviewer health needs runtime provenance and cross-run escalation — tracking the resolved model,
  launch posture and delivery outcome per run — which is follow-up work, not a config constant.
  This amendment claims better *diagnostics*, not that AGY reliability is fixed.
